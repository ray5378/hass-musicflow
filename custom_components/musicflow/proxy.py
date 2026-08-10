"""MusicFlow 后端代理:让 HA 前端卡片在外网/跨网段也能访问后端。

纯直连方案下,卡片在浏览器里直接连后端的 /ws 和 /rest。外网访问时会被
Private Network Access(PNA)、混合内容(HTTPS 页访问 HTTP 后端)、私有 IP
不可路由三重拦截。这里提供两条与 HA 同源的通道,由卡片 v1.6.0 在「直连失败」
时自动切换:

1. REST 代理  GET/POST/PUT/DELETE /api/musicflow/rest/{tail}
   把请求原样转发到后端 {url}/rest/{tail}(带 Bearer api_key),
   封面(getCoverArt)等二进制响应也原样回传。

2. 实时事件订阅  websocket 命令 musicflow/subscribe
   每条订阅开一条独立的后端 /ws 连接,把后端推送的消息经 HA WebSocket
   event_message 转发给对应客户端;后端连接断开时发一条
   {"type":"connection_closed"} 让卡片重新订阅。

认证:视图与订阅都要求 HA 登录(视图 requires_auth 默认开、HA WS 本身带认证),
卡片通过 fetchWithAuth / subscribeMessage 走同一套 HA 凭据;后端 API Key
只存在 HA 侧,不会下发给浏览器。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from aiohttp import web
from yarl import URL

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, WS_HEARTBEAT

_LOGGER = logging.getLogger(__name__)

# 后端长时间空闲时可能被代理/防火墙掐断,与应用层心跳保持一致(卡片直连同款 25s)
_PING_INTERVAL = 25

# 本视图挂载前缀(与 url 属性一致,剥离时用 raw path 保留百分号编码)
_PROXY_PREFIX = "/api/musicflow/rest"


def _first_backend(hass: HomeAssistant) -> dict[str, Any] | None:
    """取第一个已加载配置项的后端连接信息(url / api_key / verify_ssl)。"""
    backends = hass.data.get(DOMAIN, {}).get("_backends", {})
    for value in backends.values():
        if value:
            return value
    return None


def _build_ws_url(url: str, api_key: str) -> str:
    """后端 /ws 连接地址(带 token),与 api.MusicFlowClient.ws_url 同一套契约。"""
    scheme = "wss" if url.startswith("https") else "ws"
    host = url.split("://", 1)[-1]
    return f"{scheme}://{host}/ws?token={quote(api_key, safe='')}"


class MusicFlowProxyView(HomeAssistantView):
    """把 /api/musicflow/rest/{tail} 原样转发到后端 /rest/{tail}。"""

    url = "/api/musicflow/rest/{tail:.*}"
    name = "musicflow:rest"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__()
        self._hass = hass

    async def async_get(self, request: web.Request) -> web.Response:
        return await self._forward(request, "GET")

    async def async_post(self, request: web.Request) -> web.Response:
        return await self._forward(request, "POST")

    async def async_put(self, request: web.Request) -> web.Response:
        return await self._forward(request, "PUT")

    async def async_delete(self, request: web.Request) -> web.Response:
        return await self._forward(request, "DELETE")

    async def _forward(
        self, request: web.Request, method: str
    ) -> web.Response:
        backend = _first_backend(self._hass)
        if backend is None:
            return web.json_response(
                {"error": "MusicFlow 未配置后端连接"}, status=503
            )

        # 用 raw path 保留百分号编码:peerId 极端情况下含 `/`(udn 回退成 location
        # URL),被 decode 后再拼回会破坏路径结构;match_info 只作兜底。
        raw = request.raw_path
        if raw.startswith(_PROXY_PREFIX):
            tail = raw[len(_PROXY_PREFIX):]
        else:
            tail = f"/{request.match_info.get('tail', '')}"
        target = f"{backend['url'].rstrip('/')}/rest{tail}"
        if request.query_string:
            target = f"{target}?{request.query_string}"
        # encoded=True:tail 已是编码后的路径,不再二次编码(否则 %2F 会被拆成路径分隔符)
        url = URL(target, encoded=True)

        headers = {"Authorization": f"Bearer {backend['api_key']}"}
        data: bytes | None = None
        if method in ("POST", "PUT"):
            data = await request.read()
            content_type = request.headers.get("content-type")
            if content_type:
                headers["Content-Type"] = content_type

        session = async_get_clientsession(
            self._hass, verify_ssl=backend.get("verify_ssl", True)
        )
        try:
            async with session.request(
                method, url, headers=headers, data=data
            ) as resp:
                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type=resp.content_type,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("转发 %s %s 失败: %s", method, tail, err)
            return web.json_response({"error": f"转发失败: {err}"}, status=502)


@websocket_api.websocket_command(
    {vol.Required("type"): "musicflow/subscribe"}
)
@websocket_api.async_response
async def _ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """订阅后端实时事件(卡片代理模式的事件通道)。

    每条订阅开一条独立的后端 /ws 连接,与协调器自己那条互不干扰;
    客户端取消订阅或断开时关闭该连接。
    """
    backend = _first_backend(hass)
    if backend is None:
        connection.send_message(
            websocket_api.error_message(
                msg["id"], "no_backend", "MusicFlow 未配置后端连接"
            )
        )
        return

    session = async_get_clientsession(
        hass, verify_ssl=backend.get("verify_ssl", True)
    )
    try:
        ws = await session.ws_connect(
            _build_ws_url(backend["url"], backend["api_key"]),
            heartbeat=WS_HEARTBEAT,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        _LOGGER.debug("订阅后端 WS 失败: %s", err)
        connection.send_message(
            websocket_api.error_message(
                msg["id"], "ws_error", f"连接后端失败: {err}"
            )
        )
        return

    def _cleanup() -> None:
        """客户端取消订阅/断开连接时,关闭这条独立的后端 WS。"""
        if not ws.closed:
            hass.async_create_task(ws.close())

    connection.subscriptions[msg["id"]] = _cleanup
    connection.send_message(websocket_api.result_message(msg["id"], {}))
    hass.async_create_task(_forward_ws(hass, ws, connection, msg["id"]))


async def _forward_ws(
    hass: HomeAssistant,
    ws: aiohttp.ClientWebSocketResponse,
    connection: websocket_api.ActiveConnection,
    sub_id: int,
) -> None:
    """把后端 WS 消息转发成 HA WS 的 event_message。

    后端连接断开(后端重启/网络中断)时,发一条 {"type":"connection_closed"}
    让卡片重新订阅;本任务随即结束,订阅清理回调负责收尾。
    """
    ping_task = hass.async_create_task(_ping_loop(ws))
    try:
        async for message in ws:
            if message.type is aiohttp.WSMsgType.TEXT:
                try:
                    payload = message.json()
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    try:
                        connection.send_message(
                            websocket_api.event_message(sub_id, payload)
                        )
                    except (RuntimeError, ConnectionError):
                        break
            elif message.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break
    finally:
        ping_task.cancel()
        if not ws.closed:
            await ws.close()
        # 通知卡片事件通道已断(订阅本身还挂着,等卡片重新订阅后由 cleanup 收掉)
        try:
            connection.send_message(
                websocket_api.event_message(
                    sub_id, {"type": "connection_closed"}
                )
            )
        except (RuntimeError, ConnectionError):
            pass


async def _ping_loop(ws: aiohttp.ClientWebSocketResponse) -> None:
    """应用层心跳:后端回 pong,防止空闲连接被代理/防火墙掐掉。"""
    while not ws.closed:
        await asyncio.sleep(_PING_INTERVAL)
        try:
            await ws.send_json({"type": "ping"})
        except (aiohttp.ClientError, RuntimeError):
            break
