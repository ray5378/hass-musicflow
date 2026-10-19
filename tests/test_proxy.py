"""proxy.py 单测（简体中文）。

锁死对象：外网/跨网段场景下的两条代理通道 ——
REST 转发视图（/api/musicflow/rest/{tail} → 后端 /rest/{tail}，含封面等二进制响应）
与后端 WS 事件订阅（musicflow/subscribe）。认证头、?token= 兜底、缓存头透传、
错误码映射都在这里锁死。
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from custom_components.musicflow import proxy as proxy_mod
from custom_components.musicflow.const import DOMAIN
from custom_components.musicflow.proxy import (
    MusicFlowProxyView,
    _backend_token,
    _build_ws_url,
    _first_backend,
    _forward_ws,
    _ping_loop,
    _ws_subscribe,
)


# ==================== 假设施 ====================
class FakeResponse:
    def __init__(self, status=200, body=b"data", content_type="image/png", headers=None) -> None:
        self.status = status
        self._body = body
        self.content_type = content_type
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body


class _RequestCtx:
    def __init__(self, resp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeSession:
    def __init__(self, resp=None, ws=None, ws_error=None) -> None:
        self.resp = resp or FakeResponse()
        self.calls: list[dict] = []
        self.ws = ws
        self.ws_error = ws_error
        self.ws_calls: list[str] = []

    def request(self, method, url, headers=None, data=None):
        self.calls.append(
            {"method": method, "url": str(url), "headers": headers, "data": data}
        )
        return _RequestCtx(self.resp)

    async def ws_connect(self, url, heartbeat=None):
        self.ws_calls.append(str(url))
        if self.ws_error is not None:
            raise self.ws_error
        return self.ws


class CIHeaders(dict):
    """大小写不敏感的头部映射（与 aiohttp 的 CIMultiDict 语义一致）。"""

    def __init__(self, data=None) -> None:
        super().__init__({str(k).lower(): v for k, v in (data or {}).items()})

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())


class FakeRequest:
    def __init__(self, raw_path, query_string="", headers=None, body=b"", match_info=None) -> None:
        self.raw_path = raw_path
        self.query_string = query_string
        self.headers = CIHeaders(headers)
        self._body = body
        self.match_info = match_info or {}

    async def read(self) -> bytes:
        return self._body


class FakeWSMessage:
    def __init__(self, payload=None, mtype=aiohttp.WSMsgType.TEXT) -> None:
        self.type = mtype
        self._payload = payload

    def json(self):
        return self._payload


class FakeWS:
    """异步可迭代的假后端 WebSocket。"""

    def __init__(self, messages=(), closed=False) -> None:
        self._messages = list(messages)
        self.closed = closed
        self.close_calls = 0
        self.sent: list = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    async def send_json(self, payload) -> None:
        self.sent.append(payload)
        self.closed = True  # 让 _ping_loop 跑一轮就退出


class FakeConnection:
    def __init__(self) -> None:
        self.results: list[tuple] = []
        self.messages: list[dict] = []
        self.subscriptions: dict[int, object] = {}

    def send_result(self, msg_id, result) -> None:
        self.results.append((msg_id, result))

    def send_message(self, message) -> None:
        self.messages.append(message)


def _backend(client=None, url="http://mf.local:46400") -> dict:
    return {"url": url, "client": client, "verify_ssl": True}


@pytest.fixture
def proxy_hass(hass, monkeypatch):
    """把假 aiohttp 会话注入 proxy 模块，并提供 backend/client 便捷构造。"""
    session = FakeSession()
    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda hass, verify_ssl=True: session
    )

    class Client:
        token = "jwt-1"

    hass.data.setdefault(DOMAIN, {})["_backends"] = {"e1": _backend(Client())}
    return hass, session, Client()


# ==================== 纯函数 ====================
def test_first_backend_and_token():
    class FakeHass:
        data: dict = {}

    assert _first_backend(FakeHass()) is None
    FakeHass.data = {DOMAIN: {"_backends": {}}}
    assert _first_backend(FakeHass()) is None
    FakeHass.data = {DOMAIN: {"_backends": {"e": None}}}
    assert _first_backend(FakeHass()) is None
    backend = {"url": "u", "client": object()}
    FakeHass.data = {DOMAIN: {"_backends": {"e1": None, "e2": backend}}}
    assert _first_backend(FakeHass()) is backend


def test_backend_token():
    assert _backend_token({"client": None}) is None
    assert _backend_token({}) is None
    assert _backend_token({"client": object()}) is None

    class Client:
        token = "abc"

    assert _backend_token({"client": Client()}) == "abc"


def test_build_ws_url():
    assert _build_ws_url("http://mf.local:46400", "jwt") == (
        "ws://mf.local:46400/ws?token=jwt"
    )
    assert _build_ws_url("https://mf.local:46400", None) == (
        "wss://mf.local:46400/ws?token="
    )
    # token 需整体转义（含 `/` 等）
    assert "a%2Fb" in _build_ws_url("http://h", "a/b")


def test_constants():
    assert proxy_mod._PROXY_PREFIX == "/api/musicflow/rest"
    assert proxy_mod._PING_INTERVAL == 25
    assert MusicFlowProxyView.url == "/api/musicflow/rest/{tail:.*}"
    assert MusicFlowProxyView.name == "musicflow:rest"


# ==================== REST 转发 ====================
async def test_forward_no_backend_returns_503(hass, monkeypatch):
    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda h, verify_ssl=True: FakeSession()
    )
    view = MusicFlowProxyView(hass)
    resp = await view._forward(FakeRequest("/api/musicflow/rest/x"), "GET")
    assert resp.status == 503
    assert "error" in json.loads(resp.body)


async def test_forward_get_builds_url_and_passes_headers(proxy_hass):
    hass, session, _client = proxy_hass
    session.resp = FakeResponse(
        status=200,
        body=b"\x89PNG",
        content_type="image/png",
        headers={"Cache-Control": "max-age=60", "ETag": "w/1", "X-Other": "drop"},
    )
    view = MusicFlowProxyView(hass)
    request = FakeRequest(
        "/api/musicflow/rest/getCoverArt?id=5&size=400",
        query_string="id=5&size=400",
        headers={"Accept": "image/webp", "If-None-Match": "w/1"},
    )
    resp = await view._forward(request, "GET")

    call = session.calls[-1]
    # 只拼一次 query，并兜底带上 ?token=
    assert call["url"] == (
        "http://mf.local:46400/rest/getCoverArt?id=5&size=400&token=jwt-1"
    )
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == "Bearer jwt-1"
    assert call["headers"]["Accept"] == "image/webp"
    assert call["headers"]["If-None-Match"] == "w/1"
    # 状态、字节流与缓存头原样回传
    assert resp.status == 200
    assert resp.body == b"\x89PNG"
    assert resp.content_type == "image/png"
    assert resp.headers["Cache-Control"] == "max-age=60"
    assert "X-Other" not in resp.headers


async def test_forward_appends_query_when_target_has_none(proxy_hass):
    hass, session, _client = proxy_hass
    view = MusicFlowProxyView(hass)
    request = FakeRequest("/api/musicflow/rest/ping", query_string="a=1")
    await view._forward(request, "GET")
    assert session.calls[-1]["url"] == "http://mf.local:46400/rest/ping?a=1&token=jwt-1"


async def test_forward_fallback_tail_from_match_info(proxy_hass):
    hass, session, _client = proxy_hass
    view = MusicFlowProxyView(hass)
    request = FakeRequest("/other/path", match_info={"tail": "a/b"})
    await view._forward(request, "GET")
    assert session.calls[-1]["url"] == "http://mf.local:46400/rest/a/b?token=jwt-1"


async def test_forward_post_reads_body_and_content_type(proxy_hass):
    hass, session, _client = proxy_hass
    view = MusicFlowProxyView(hass)
    request = FakeRequest(
        "/api/musicflow/rest/api/v1/play",
        headers={"Content-Type": "application/json"},
        body=b'{"x":1}',
    )
    await view._forward(request, "POST")
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["data"] == b'{"x":1}'
    assert call["headers"]["Content-Type"] == "application/json"


async def test_forward_maps_client_error_to_502(hass, monkeypatch):
    class BoomSession(FakeSession):
        def request(self, method, url, headers=None, data=None):
            raise aiohttp.ClientError("boom")

    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda h, verify_ssl=True: BoomSession()
    )
    hass.data.setdefault(DOMAIN, {})["_backends"] = {"e1": _backend(object())}
    view = MusicFlowProxyView(hass)
    resp = await view._forward(FakeRequest("/api/musicflow/rest/x"), "GET")
    assert resp.status == 502


async def test_view_handler_methods(proxy_hass):
    hass, session, _client = proxy_hass
    view = MusicFlowProxyView(hass)
    for method, handler in (
        ("GET", view.get),
        ("POST", view.post),
        ("PUT", view.put),
        ("DELETE", view.delete),
    ):
        await handler(FakeRequest("/api/musicflow/rest/x"))
        assert session.calls[-1]["method"] == method


# ==================== WS 订阅 ====================
async def test_ws_subscribe_no_backend(hass, monkeypatch):
    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda h, verify_ssl=True: FakeSession()
    )
    conn = FakeConnection()
    await _ws_subscribe.__wrapped__(
        hass, conn, {"id": 7}
    )
    assert conn.results == []
    assert conn.messages[0]["success"] is False
    assert conn.messages[0]["error"]["code"] == "no_backend"


async def test_ws_subscribe_success(hass, monkeypatch):
    session = FakeSession(ws=FakeWS([]))
    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda h, verify_ssl=True: session
    )
    class Client:
        token = "jwt-1"

    hass.data.setdefault(DOMAIN, {})["_backends"] = {"e1": _backend(Client())}
    conn = FakeConnection()
    await _ws_subscribe.__wrapped__(hass, conn, {"id": 8})
    assert conn.messages[0]["id"] == 8
    assert conn.messages[0]["success"] is True
    assert 8 in conn.subscriptions
    assert session.ws_calls[0] == "ws://mf.local:46400/ws?token=jwt-1"
    await hass.async_block_till_done()
    # 转发任务结束后 WS 已关闭
    assert session.ws.closed is True


async def test_ws_subscribe_ws_error(hass, monkeypatch):
    session = FakeSession(ws_error=aiohttp.ClientError("no ws"))
    monkeypatch.setattr(
        proxy_mod, "async_get_clientsession", lambda h, verify_ssl=True: session
    )
    class Client:
        token = "t"

    hass.data.setdefault(DOMAIN, {})["_backends"] = {"e1": _backend(Client())}
    conn = FakeConnection()
    await _ws_subscribe.__wrapped__(hass, conn, {"id": 9})
    assert conn.messages[0]["error"]["code"] == "ws_error"


# ==================== WS 转发 / 心跳 ====================
async def test_forward_ws_forwards_messages_and_closes(hass):
    ws = FakeWS(
        [
            FakeWSMessage({"type": "peer_snapshot", "peers": []}),
            FakeWSMessage(payload="not-dict"),  # 非 dict payload 被丢弃
            FakeWSMessage(aiohttp.WSMsgType.CLOSED),  # 关闭消息 → 退出
        ]
    )
    conn = FakeConnection()
    await _forward_ws(hass, ws, conn, 42)
    event_messages = [m for m in conn.messages if m["type"] == "event"]
    assert event_messages[0]["event"] == {"type": "peer_snapshot", "peers": []}
    # 结束前补齐一条 connection_closed，让卡片重新订阅
    assert event_messages[-1]["event"] == {"type": "connection_closed"}
    assert ws.closed is True


async def test_forward_ws_stops_when_connection_send_fails(hass):
    ws = FakeWS(
        [
            FakeWSMessage({"type": "a"}),
            FakeWSMessage({"type": "b"}),
        ]
    )

    class BoomConnection(FakeConnection):
        def send_message(self, message):
            raise RuntimeError("closed")

    conn = BoomConnection()
    await _forward_ws(hass, ws, conn, 1)  # 不应抛出
    assert ws.closed is True


async def test_ping_loop_sends_and_exits(hass, monkeypatch):
    monkeypatch.setattr(proxy_mod, "_PING_INTERVAL", 0)
    ws = FakeWS()
    await _ping_loop(ws)
    assert ws.sent == [{"type": "ping"}]


async def test_ping_loop_breaks_on_error(hass, monkeypatch):
    monkeypatch.setattr(proxy_mod, "_PING_INTERVAL", 0)

    class BoomWS(FakeWS):
        async def send_json(self, payload):
            raise aiohttp.ClientError("closed")

    ws = BoomWS()
    await _ping_loop(ws)  # 不应抛出
