"""HTTP API 安全调用工具.

安全策略（企业级最小集）：
- SSRF 防护：解析主机名 → DNS 解析 → 校验 IP 不在内网/保留地址段
- 方法白名单：仅允许 GET/POST/PUT/PATCH/DELETE
- 敏感头过滤：移除 Authorization/Cookie/Proxy-* 等可能泄露凭证的头
- 流式大小上限：边下载边截断，避免大响应打爆内存
- 超时分层：连接超时 + 读取超时分离
"""
import ipaddress
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from pydantic import ConfigDict, Field

from react_ai.tools.base import BaseTool, ToolContext, ToolInputModel


class HttpRequestInput(ToolInputModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(description="目标 URL（仅 http/https，禁止内网）")
    method: Optional[str] = Field(default=None, description="HTTP 方法 (GET/POST/PUT/PATCH/DELETE)")
    headers: Optional[Dict[str, Any]] = Field(default=None, description="自定义请求头（敏感头将被自动移除）")
    json_data: Optional[Dict[str, Any]] = Field(
        default=None, alias="json", description="请求 JSON 载荷"
    )


class HttpTool(BaseTool):
    name = "http_request"
    description = "发起受控 HTTP 请求（禁止访问内网/云元数据地址）。"
    side_effects = "none"
    tags = ["network", "http"]
    input_model = HttpRequestInput

    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

    # 响应体最大字节数（流式截断）
    MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB

    # 禁止透传的敏感请求头（防止模型外带凭证）
    FORBIDDEN_HEADERS = {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
    }

    def execute(self, arguments: dict, ctx: ToolContext) -> Any:
        url = arguments.get("url", "")
        method = (arguments.get("method") or "GET").upper()
        headers = arguments.get("headers", {}) or {}
        json_data = arguments.get("json", None)

        # 1. 方法白名单
        if method not in self.ALLOWED_METHODS:
            raise PermissionError(
                f"HTTP 方法 [{method}] 不被允许，允许的方法: {', '.join(sorted(self.ALLOWED_METHODS))}"
            )

        # 2. URL 解析与协议校验
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PermissionError(f"仅允许 http/https 协议，当前: {parsed.scheme or 'none'}")
        if not parsed.hostname:
            raise PermissionError("URL 缺少主机名")

        # 3. SSRF 防护：解析主机名到 IP，逐一校验是否为内网/保留地址
        resolved_ips = self._resolve_host(parsed.hostname)
        for ip in resolved_ips:
            if self._is_blocked_ip(ip):
                raise PermissionError(
                    f"URL 主机 [{parsed.hostname}] 解析到被禁止的内网/保留地址 [{ip}]，已拦截（SSRF 防护）"
                )

        # 4. 敏感头过滤
        safe_headers = {
            k: v for k, v in headers.items() if k.lower() not in self.FORBIDDEN_HEADERS
        }

        # 5. 流式请求 + 大小上限，避免大响应打爆内存
        content_chunks: List[bytes] = []
        total = 0
        truncated = False
        with httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            follow_redirects=False,  # 禁止自动跟随重定向（防止重定向绕过 SSRF 校验）
        ) as client:
            with client.stream(method=method, url=url, headers=safe_headers, json=json_data) as resp:
                status_code = resp.status_code
                for chunk in resp.iter_bytes(chunk_size=8192):
                    if total + len(chunk) > self.MAX_RESPONSE_BYTES:
                        truncated = True
                        break
                    content_chunks.append(chunk)
                    total += len(chunk)

        text = b"".join(content_chunks).decode("utf-8", errors="replace")
        if truncated:
            text += "\n...[response truncated due to size limit]"

        return {
            "status_code": status_code,
            "text": text,
            "truncated": truncated,
            "size_bytes": total,
        }

    @staticmethod
    def _resolve_host(hostname: str) -> List[str]:
        """DNS 解析主机名为 IP 列表；解析失败返回空列表。"""
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return []
        ips: List[str] = []
        for family, _type, _proto, _canonname, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in ips:
                ips.append(ip)
        return ips

    @staticmethod
    def _is_blocked_ip(ip_str: str) -> bool:
        """判定 IP 是否属于被禁止的内网/保留/环回/链路本地/云元数据地址。"""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True  # 无法解析的 IP 一律拒绝

        # 环回、私有、链路本地、保留、未指定、文档保留地址全部封禁
        blocked_networks: Tuple[ipaddress._BaseNetwork, ...] = (
            ipaddress.ip_network("127.0.0.0/8"),        # 环回
            ipaddress.ip_network("10.0.0.0/8"),          # 私有 A 类
            ipaddress.ip_network("172.16.0.0/12"),       # 私有 B 类
            ipaddress.ip_network("192.168.0.0/16"),      # 私有 C 类
            ipaddress.ip_network("169.254.0.0/16"),      # 链路本地（含云元数据 169.254.169.254）
            ipaddress.ip_network("0.0.0.0/8"),           # "本网络"
            ipaddress.ip_network("100.64.0.0/10"),       # CGNAT 共享地址
            ipaddress.ip_network("192.0.0.0/24"),        # IETF 协议分配
            ipaddress.ip_network("192.0.2.0/24"),        # 文档 TEST-NET-1
            ipaddress.ip_network("198.51.100.0/24"),     # 文档 TEST-NET-2
            ipaddress.ip_network("203.0.113.0/24"),      # 文档 TEST-NET-3
            ipaddress.ip_network("224.0.0.0/4"),         # 组播
            ipaddress.ip_network("240.0.0.0/4"),         # 保留
            ipaddress.ip_network("::1/128"),             # IPv6 环回
            ipaddress.ip_network("fc00::/7"),            # IPv6 唯一本地地址
            ipaddress.ip_network("fe80::/10"),           # IPv6 链路本地
            ipaddress.ip_network("::/128"),              # IPv6 未指定
        )
        return any(ip in net for net in blocked_networks)
