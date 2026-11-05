# AGENTS.md — react-ai 项目协作指南

面向 AI 协作助手（以及人类协作者）的项目约定、运行环境与测试策略说明。

## 1. 项目概览

- **语言与运行时**：Python 3.14，项目根目录 `d:\code\react-ai`
- **Python 解释器路径**（本机唯一可用入口，`python` 命令不在 PATH 中）：

  ```
  C:\Users\admin\AppData\Local\Programs\Python\Python314\python.exe
  ```

- **包结构**：源码位于 `src/react_ai/`，测试位于 `tests/unit/`，评测集位于 `eval/`，示例位于 `examples/`，规范文档位于 `wiki/`
- **依赖管理**：`pyproject.toml` + `uv.lock`

## 2. 常用命令

```powershell
# 定义解释器别名，便于复用
$py = 'C:\Users\admin\AppData\Local\Programs\Python\Python314\python.exe'

# 运行全量单元测试
& $py -m pytest tests/unit -q

# 运行指定测试文件
& $py -m pytest tests/unit/test_prompts.py tests/unit/test_engine.py -q

# 运行评测回归（结构断言 + 基线 diff）
& $py eval/run_prompt_eval.py
& $py eval/run_prompt_eval.py --update-baseline
```

## 3. 测试策略与本机环境约束

### 3.1 本机环境缺失项

- **本机没有 PostgreSQL**：`psycopg` 连接会触发 `ConnectionTimeout` 或长时间挂起。
- **本机未安装 `fakeredis`**：`tests/unit/test_memory.py` 收集阶段会因 `ModuleNotFoundError` 失败。

### 3.2 受影响的测试文件

| 测试文件 | 依赖 | 本机行为 |
|---|---|---|
| `tests/unit/test_aftersales_tools.py` | PostgreSQL（psycopg） | 每个用例连接超时（约 160s），整批挂起 30 分钟 |
| `tests/unit/test_memory.py` | fakeredis | 收集阶段 `ModuleNotFoundError` |

### 3.3 测试执行建议（重要）

> **本机没有 PG，跑测试文件的时候对接数据库则使用假数据。**

- **默认排除**：本地跑全量单测时，应显式排除依赖真实 PG 的用例：

  ```powershell
  & $py -m pytest tests/unit -q --ignore=tests/unit/test_aftersales_tools.py --ignore=tests/unit/test_memory.py
  ```

- **对接数据库的测试必须使用假数据**：凡是需要 PG / Redis 的测试用例，应当使用 `unittest.mock.patch`、`fakeredis`、`sqlite` 内存库或项目内已有的 `MockLLM` / 内存 `CheckpointStore` 等假数据实现替换真实连接，**禁止**依赖本机或远程数据库实例。
- **新增测试的约定**：
  - 涉及外部数据库的测试，必须在 `setUp` 中用 `patch` 或内存替代实现注入假数据；
  - 不得在测试中直接发起 `psycopg.connect` / `redis.Redis` 等真实连接；
  - 计时敏感的断言（如并行耗时对比）应放宽阈值或使用确定性断言，避免抖动。

### 3.4 基线测试集

项目最小可验证基线（必过）：

```powershell
& $py -m pytest tests/unit/test_prompts.py tests/unit/test_engine.py tests/unit/test_prompts_enterprise.py -q
```

预期：**25 passed**。

## 4. 关键模块与差距分析

- **Prompts 企业级改造**：`wiki/Prompts-Gap-Analysis.md` 的 12 项差距（2.1-2.12）已全部落地，覆盖文件：
  - `src/react_ai/prompts/base.py` / `manager.py` / `env.py` / `schema.py` / `audit.py`
  - `src/react_ai/core/engine.py`（2.1 引擎接入）
  - `examples/aftersales_langgraph.py`（2.1 示例迁移）
  - `tests/unit/test_prompts_enterprise.py`（19 项验收）
  - `eval/prompts/react_standard.json` + `eval/run_prompt_eval.py`（22 样本回归）

## 5. 协作约定

- **语言**：代码注释、提交信息、与助手的交互一律使用中文。
- **风格**：保持简洁，不做超出需求的改动；新增测试必须本地可跑（不依赖外部 DB）。
- **文件引用**：在对话中引用源码时使用 `file:///` 链接 + 行号区间，便于跳转。
