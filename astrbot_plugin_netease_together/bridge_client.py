"""A short-lived, authenticated MCP client; never registers the upstream tools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class MusicError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code)


READ_TOOLS = {
    "netease_together_status",
    "netease_together_now",
    "netease_together_lyrics",
    "netease_search_songs",
    "netease_my_playlists",
    "netease_playlist_tracks",
}
CONTROL_TOOLS = {
    "netease_together_join",
    "netease_together_pause",
    "netease_together_resume",
    "netease_together_next",
    "netease_together_goto",
    "netease_together_enqueue_next",
    "netease_together_leave",
}


def safe_data(value, depth: int = 0):
    """Keep tool data bounded and omit credentials, account IDs, and raw errors."""
    if depth > 12:
        return "[nested data omitted]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).lower().replace("_", "")
            if normalized in {"credentialsready", "accountconfigured"}:
                result[str(key)] = bool(item)
            elif any(word in normalized for word in (
                "cookie", "token", "secret", "password", "authorization", "credential",
                "accountid", "inviteruid", "inviterid", "lastchatmessage", "modelcontext",
            )):
                continue
            elif normalized in {"error", "lasterror", "errormessage", "stack"}:
                result["hasError"] = bool(item)
            else:
                result[str(key)] = safe_data(item, depth + 1)
        return result
    if isinstance(value, list):
        return [safe_data(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:65000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported value]"


def _client_factory(headers=None, timeout=None, auth=None):
    # Do not inherit a global HTTP proxy or forward the Bearer token on redirects.
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


class CoveClient:
    def __init__(self, config):
        self.config = config

    def _connection(self):
        url = str(self.config.get("bridge_url", "http://cove:8787/mcp/music")).strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise MusicError("invalid_bridge_url", "音乐桥接地址格式无效。") from None
        host = parsed.hostname or ""
        local = host in {"cove", "localhost"}
        try:
            local = local or ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if (parsed.scheme not in {"http", "https"} or not local
                or parsed.path != "/mcp/music" or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise MusicError("invalid_bridge_url", "音乐桥接地址必须是内网 /mcp/music 地址。")
        path = Path(str(self.config.get("bridge_token_file", "")))
        if not path.is_absolute():
            raise MusicError("missing_bridge_token", "尚未配置音乐桥接令牌文件。")
        try:
            if path.stat().st_size > 4096:
                raise ValueError("oversized token file")
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            raise MusicError("missing_bridge_token", "音乐桥接令牌文件尚未准备好。") from None
        if len(token) < 32 or any(char.isspace() for char in token):
            raise MusicError("invalid_bridge_token", "音乐桥接令牌格式无效。")
        try:
            timeout = max(10, min(60, int(self.config.get("request_timeout_seconds", 25))))
        except (ValueError, TypeError):
            timeout = 25
        return url, token, timeout

    async def call(self, tool: str, arguments: dict):
        if tool not in READ_TOOLS | CONTROL_TOOLS:
            raise MusicError("tool_not_allowed", "这项网易云操作没有开放。")
        url, token, timeout = self._connection()
        try:
            # The server is stateless. One initialize+call per request avoids a
            # permanent listener and never asks the LLM to maintain a heartbeat.
            async with asyncio.timeout(timeout + 5):
                async with streamablehttp_client(
                    url,
                    headers={"Authorization": "Bearer " + token},
                    timeout=timeout,
                    sse_read_timeout=timeout,
                    terminate_on_close=False,
                    httpx_client_factory=_client_factory,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=timeout),
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
            if result.isError:
                # Upstream exception text is intentionally not returned: it can
                # contain provider response details or account data.
                raise MusicError("upstream_rejected", "网易云未确认这项请求，请查看账号或房间状态；修改操作不会自动重试。")
            value = getattr(result, "structuredContent", None)
            if value is None:
                texts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
                if len(texts) != 1 or len(texts[0]) > 200000:
                    raise MusicError("invalid_bridge_response", "音乐服务返回了无法识别的结果。")
                value = json.loads(texts[0])
            if not isinstance(value, dict):
                raise MusicError("invalid_bridge_response", "音乐服务返回了无法识别的结果。")
            return safe_data(value)
        except MusicError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not retry controls after a timeout: the write may have reached
            # NetEase even when the response did not reach us.
            raise MusicError("bridge_unavailable", "音乐服务尚未就绪或请求结果未知；请先查询状态，修改操作不会自动重试。") from None
