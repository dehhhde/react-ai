"""提示词模板管理器：版本化注册表、灰度分流、持久化与目录热更新.

对应差距分析：
- 2.5 版本化注册 + 按 trace_id 稳定灰度分流 + 回滚 + JSON 持久化
- 2.10 外部目录加载与 mtime 热更新
- 2.12 渲染缓存：原始模板与实体共用 env.render_template 编译缓存内核
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import jinja2

from react_ai.prompts.base import BasePrompt, DynamicPrompt, PromptResult
from react_ai.prompts.env import cache_snapshot, render_template
from react_ai.prompts.templates.react import REACT_SYSTEM_PROMPT, ReActPrompt

logger = logging.getLogger(__name__)


class PromptManager:
    """提示词管理器：原生模板字符串与 BasePrompt 实体双轨，统一版本化与渲染内核."""

    def __init__(self):
        self._lock = threading.RLock()
        # 原始模板：name -> version -> source
        self._templates: dict[str, dict[str, str]] = {
            "react_system": {"1.0.0": REACT_SYSTEM_PROMPT},
        }
        # BasePrompt 实体：name -> version -> prompt
        self._prompts: dict[str, dict[str, BasePrompt]] = {}
        # 当前生效版本（实体与原始模板共用名称空间）
        self._active_version: dict[str, str] = {
            "react_system": "1.0.0",
        }
        # 灰度配置：name -> {version: 百分比权重}
        self._canary: dict[str, dict[str, float]] = {}
        # 外部目录热更新
        self._watch_dir: Path | None = None
        self._file_mtimes: dict[str, float] = {}

        self.register_prompt(ReActPrompt())

    # ------------------------------------------------------------------
    # 注册与查询
    # ------------------------------------------------------------------

    def register(self, name: str, template_str: str, version: str = "1.0.0", activate: bool = True) -> None:
        """注册原始模板字符串（版本化）."""
        with self._lock:
            self._templates.setdefault(name, {})[version] = template_str
            if activate or name not in self._active_version:
                self._active_version[name] = version

    def register_prompt(self, prompt: BasePrompt, version: str | None = None, activate: bool = True) -> None:
        """注册 BasePrompt 实体；version 缺省取 prompt.version."""
        ver = version or prompt.version
        if ver != prompt.version:
            prompt.version = ver
        with self._lock:
            self._prompts.setdefault(prompt.name, {})[ver] = prompt
            if activate or prompt.name not in self._active_version:
                self._active_version[prompt.name] = ver

    def get_prompt(self, name: str, version: str | None = None) -> BasePrompt | None:
        """获取已注册的 BasePrompt 实体（缺省取当前生效版本）."""
        with self._lock:
            bucket = self._prompts.get(name)
            if not bucket:
                return None
            if version:
                return bucket.get(version)
            active = self._active_version.get(name)
            if active and active in bucket:
                return bucket[active]
            return next(iter(bucket.values()), None)

    def list_versions(self, name: str) -> list[str]:
        """列出某提示词的全部已注册版本."""
        with self._lock:
            return sorted(self._prompts.get(name, {})) or sorted(self._templates.get(name, {}))

    # ------------------------------------------------------------------
    # 版本切换与灰度（2.5）
    # ------------------------------------------------------------------

    def set_active_version(self, name: str, version: str) -> None:
        """切换生效版本（用于发布与回滚）."""
        with self._lock:
            exists = version in self._prompts.get(name, {}) or version in self._templates.get(name, {})
            if not exists:
                raise KeyError(f"提示词 [{name}] 未注册版本 [{version}]")
            self._active_version[name] = version
            logger.info("prompt_active_version name=%s version=%s", name, version)

    def set_canary(self, name: str, weights: dict[str, float]) -> None:
        """配置灰度分流：{version: 百分比}，权重总和不超过 100，其余走生效版本."""
        with self._lock:
            total = sum(weights.values())
            if total > 100:
                raise ValueError(f"灰度权重总和 {total} 超过 100")
            registered = set(self._prompts.get(name, {})) | set(self._templates.get(name, {}))
            unknown = set(weights) - registered
            if unknown:
                raise KeyError(f"灰度包含未注册版本: {sorted(unknown)}")
            self._canary[name] = dict(weights)
            logger.info("prompt_canary_set name=%s weights=%s", name, weights)

    def clear_canary(self, name: str) -> None:
        """结束灰度，全部流量回到生效版本."""
        with self._lock:
            self._canary.pop(name, None)

    def resolve_version(self, name: str, trace_id: str | None = None) -> str | None:
        """按 trace_id 稳定分流：sha256(trace_id)%100 落桶，同一请求恒定命中同版本."""
        with self._lock:
            active = self._active_version.get(name)
            canary = self._canary.get(name)
        if not canary or not trace_id:
            return active
        bucket = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest(), 16) % 100
        cumulative = 0.0
        for ver, weight in canary.items():
            cumulative += weight
            if bucket < cumulative:
                return ver
        return active

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, name: str, variables: dict[str, Any], version: str | None = None, trace_id: str | None = None) -> str:
        """基于统一 Jinja2 内核渲染原始模板字符串（与实体同缓存路径，2.2/2.12）."""
        self.reload_if_changed()
        with self._lock:
            bucket = self._templates.get(name)
            if not bucket:
                raise KeyError(f"未找到提示词模板: {name}")
            target = version or self.resolve_version(name, trace_id)
            source = bucket.get(target or "") or bucket.get(self._active_version.get(name, ""))
            if source is None:
                source = next(iter(bucket.values()))
        try:
            return render_template(source, variables)
        except jinja2.UndefinedError as exc:
            raise ValueError(f"模板 [{name}] 变量未定义: {exc.message}") from exc

    def render_prompt(
        self,
        name: str,
        variables: dict[str, Any],
        trace_id: str | None = None,
        version: str | None = None,
        locale: str | None = None,
        guardrail_chain: Any = None,
    ) -> PromptResult:
        """渲染 BasePrompt 实体：未显式指定版本时按灰度规则解析生效版本."""
        self.reload_if_changed()
        with self._lock:
            bucket = self._prompts.get(name)
            if not bucket:
                raise KeyError(f"未找到提示词实体: {name}")
            target = version or self.resolve_version(name, trace_id)
            prompt = bucket.get(target or "") or bucket.get(self._active_version.get(name, ""))
            if prompt is None:
                prompt = next(iter(bucket.values()))
        return prompt.render(
            variables=variables,
            trace_id=trace_id,
            guardrail_chain=guardrail_chain,
            locale=locale,
        )

    # ------------------------------------------------------------------
    # 持久化（2.5）
    # ------------------------------------------------------------------

    def save_to_file(self, path: str | Path) -> None:
        """全量导出注册表为 JSON（实体 spec + 原始模板 + 版本/灰度配置）."""
        with self._lock:
            data = {
                "entities": [
                    p.to_spec()
                    for bucket in self._prompts.values()
                    for p in bucket.values()
                ],
                "raw_templates": {n: dict(b) for n, b in self._templates.items()},
                "active_version": dict(self._active_version),
                "canary": {n: dict(w) for n, w in self._canary.items()},
            }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("prompt_registry_saved path=%s", target)

    def load_from_file(self, path: str | Path) -> None:
        """从 JSON 恢复注册表（同名同版本覆盖，随后恢复生效版本与灰度）."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for spec in data.get("entities", []):
            self.register_prompt(DynamicPrompt(spec), activate=False)
        for name, versions in (data.get("raw_templates") or {}).items():
            for ver, source in versions.items():
                self.register(name, source, version=ver, activate=False)
        with self._lock:
            for name, ver in (data.get("active_version") or {}).items():
                registered = ver in self._prompts.get(name, {}) or ver in self._templates.get(name, {})
                if registered:
                    self._active_version[name] = ver
            self._canary.update({n: dict(w) for n, w in (data.get("canary") or {}).items()})
        logger.info("prompt_registry_loaded path=%s entities=%d", path, len(data.get("entities", [])))

    # ------------------------------------------------------------------
    # 外部目录加载与热更新（2.10）
    # ------------------------------------------------------------------

    def load_from_directory(self, directory: str | Path) -> int:
        """从外部目录加载提示词：*.jinja/*.j2 为原始模板，*.yaml/*.json 为 Prompt spec.

        记录文件 mtime，后续渲染前自动检测变更并重载（热更新）。
        返回加载的文件数。
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise FileNotFoundError(f"提示词目录不存在: {directory}")
        count = 0
        for file in sorted(directory.iterdir()):
            if file.is_file() and file.suffix.lower() in (".jinja", ".j2", ".yaml", ".yml", ".json"):
                self._load_external_file(file)
                self._file_mtimes[str(file)] = os.path.getmtime(file)
                count += 1
        self._watch_dir = directory
        logger.info("prompt_directory_loaded dir=%s files=%d", directory, count)
        return count

    def _load_external_file(self, file: Path) -> None:
        suffix = file.suffix.lower()
        if suffix in (".jinja", ".j2"):
            name = file.stem
            self.register(name, file.read_text(encoding="utf-8"))
            return
        import yaml

        raw = file.read_text(encoding="utf-8")
        data = json.loads(raw) if suffix == ".json" else yaml.safe_load(raw)
        specs = data if isinstance(data, list) else [data]
        for spec in specs:
            self.register_prompt(DynamicPrompt(spec))

    def reload_if_changed(self) -> list[str]:
        """检测被监听目录内文件 mtime 变化并重载，返回重载的文件名列表."""
        if self._watch_dir is None:
            return []
        reloaded: list[str] = []
        for path_str, mtime in list(self._file_mtimes.items()):
            try:
                current = os.path.getmtime(path_str)
            except OSError:
                continue
            if current != mtime:
                self._load_external_file(Path(path_str))
                self._file_mtimes[path_str] = current
                reloaded.append(Path(path_str).name)
                logger.info("prompt_hot_reloaded file=%s", path_str)
        return reloaded

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def cache_stats(self) -> dict[str, Any]:
        """渲染编译缓存命中率与规模（2.12）."""
        return cache_snapshot()


prompt_manager = PromptManager()
