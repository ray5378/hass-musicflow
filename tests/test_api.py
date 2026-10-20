"""api.py 单测：认证 / 请求构造 / 错误映射 / Subsonic 解包 / WS（简体中文）。

全部走内存里的假 aiohttp 会话，**不发真实网络请求**。
锁死对象：MusicFlowClient（REST 路径前缀、Bearer 头、超时、401→重登录、
错误 → MusicFlowError/AuthError 的映射）。
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from custom_components.musicflow.api import (
    MusicFlowAuthError,
    MusicFlowClient,
    MusicFlowError,
)

BASE = "http://mf.local:46400"


# ==================== 假 aiohttp 设施 ====================
class FakeResponse:
    """够用的假响应：支持 `async with`、json()/text()/read()、status/content_type。"""

    def __init__(
        self,
        *,
        status: int = 200,
        payload=None,
        text: str = "",
        content_type: str = "application/json",
        body: bytes = b"",
    ) -> None:
        self.status = status
        self._payload = payload
        self._text = text
        self._content_type = content_type
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._body

    @property
    def content_type(self) -> str:
        return self._content_type


class FakeWSMessage:
    def __init__(self, payload=None, mtype=aiohttp.WSMsgType.TEXT) -> None:
        self.type = mtype
        self._payload = payload

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeWebSocket:
    def __init__(self, messages=(), closed: bool = False) -> None:
        self._messages = list(messages)
        self.closed = closed
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self):
        self.closed = True
        self.close_calls += 1


class FakeSession:
    """假会话：request/post/get 交给 responder；ws_connect 返回 FakeWebSocket。"""

    def __init__(self, responder=None, ws_factory=None) -> None:
        self.responder = responder or (lambda method, url, kwargs: FakeResponse(payload={}))
        self.ws_factory = ws_factory or (lambda url, kwargs: FakeWebSocket(messages=[FakeWSMessage(payload={}, mtype=aiohttp.WSMsgType.CLOSED)]))
        self.requests: list[tuple[str, str, dict]] = []

    def _do(self, method: str, url, **kwargs):
        self.requests.append((method, str(url), kwargs))
        return self.responder(method, str(url), kwargs)

    def request(self, method, url, **kwargs):
        return self._do(method, url, **kwargs)

    def post(self, url, **kwargs):
        return self._do("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    async def ws_connect(self, url, **kwargs):
        self.requests.append(("WS", str(url), kwargs))
        return self.ws_factory(str(url), kwargs)


def build(responder=None, ws_factory=None, url: str = BASE + "/"):
    """构造 (client, session)；url 故意带尾斜杠以覆盖 rstrip 分支。"""
    session = FakeSession(responder=responder, ws_factory=ws_factory)
    client = MusicFlowClient(session, url, "u", "p")
    return client, session


def json_response(payload, status: int = 200) -> FakeResponse:
    return FakeResponse(status=status, payload=payload, content_type="application/json")


def subsonic_response(inner: dict, status: int = 200) -> FakeResponse:
    return json_response({"subsonic-response": inner}, status=status)


# ==================== 认证：用户名+密码 → JWT ====================
async def test_login_success_returns_and_stores_token():
    client, session = build(lambda m, u, k: json_response({"token": "jwt-1"}))
    assert client.token is None
    assert await client.login() == "jwt-1"
    assert client.token == "jwt-1"
    method, url, kwargs = session.requests[0]
    assert method == "POST"
    assert url == f"{BASE}/rest/api/v1/auth/login"
    assert kwargs["json"] == {"username": "u", "password": "p"}
    assert kwargs["timeout"].total == 15  # REQUEST_TIMEOUT


async def test_login_401_becomes_auth_error():
    client, _ = build(lambda m, u, k: FakeResponse(status=401, text="bad"))
    with pytest.raises(MusicFlowAuthError, match="用户名或密码错误"):
        await client.login()


async def test_login_other_error_status_becomes_musicflow_error():
    client, _ = build(lambda m, u, k: FakeResponse(status=500, text="boom"))
    with pytest.raises(MusicFlowError, match="登录失败 500") as err:
        await client.login()
    assert "boom" in str(err.value)
    assert not isinstance(err.value, MusicFlowAuthError)


async def test_login_missing_token_is_auth_error():
    for payload in ({"user": 1}, None, "not-a-dict"):
        client, _ = build(lambda m, u, k, p=payload: json_response(p))
        with pytest.raises(MusicFlowAuthError, match="缺少 token"):
            await client.login()


async def test_login_timeout_becomes_musicflow_error():
    def boom(m, u, k):
        raise asyncio.TimeoutError

    client, _ = build(boom)
    with pytest.raises(MusicFlowError, match="登录超时"):
        await client.login()


async def test_login_client_error_becomes_musicflow_error():
    def boom(m, u, k):
        raise aiohttp.ClientConnectionError("refused")

    client, _ = build(boom)
    with pytest.raises(MusicFlowError, match="登录网络错误"):
        await client.login()


async def test_ensure_token_only_logs_in_once():
    calls: list[str] = []

    def responder(m, u, k):
        calls.append(u)
        return json_response({"token": "jwt-2"})

    client, session = build(responder)
    # 已有 token 时直接返回，不产生新请求
    client._token = "cached"
    assert await client._ensure_token() == "cached"
    assert session.requests == []
    client._token = None
    assert await client._ensure_token() == "jwt-2"
    assert calls == [f"{BASE}/rest/api/v1/auth/login"]


async def test_properties_and_helpers():
    client, _ = build()
    assert client.base_url == BASE  # 尾斜杠被去掉
    assert client._headers() == {"Authorization": "Bearer "}
    client._token = "t0"
    assert client._headers() == {"Authorization": "Bearer t0"}
    # peerId 里的 `/` 必须整体转义成单个路径段
    assert MusicFlowClient._encode("dlna:a/b c") == "dlna%3Aa%2Fb%20c"
    assert MusicFlowClient._encode("plain") == "plain"


@pytest.mark.parametrize(
    ("url", "expected_scheme"),
    [(BASE, "ws"), ("https://mf.local", "wss")],
)
async def test_ws_url_scheme_and_token(url, expected_scheme):
    client, _ = build(url=url)
    client._token = "a b/c"
    assert client.ws_url == f"{expected_scheme}://mf.local{':46400' if url == BASE else ''}/ws?token=a%20b%2Fc"


async def test_ws_url_without_token():
    client, _ = build()
    assert client.ws_url == f"ws://mf.local:46400/ws?token="


# ==================== 通用请求：方法/路径/头/错误 ====================
async def test_api_helpers_prefix_and_verb():
    seen = []

    def responder(method, url, kwargs):
        seen.append((method, url, kwargs))
        return json_response({"ok": True})

    client, _ = build(responder)
    client._token = "jwt"
    assert await client._api_get("/peers", {"a": 1}) == {"ok": True}
    assert await client._api_post("/play", {"b": 2}) == {"ok": True}
    # DELETE 的响应同样是 JSON 时返回解析后的字典（不是 None）
    assert await client._api_delete("/peers/x/queue") == {"ok": True}
    assert [s[0] for s in seen] == ["GET", "POST", "DELETE"]
    assert seen[0][1] == f"{BASE}/rest/api/v1/peers"
    assert seen[0][2]["params"] == {"a": 1}
    assert seen[1][2]["json"] == {"b": 2}
    # POST 未给 body 时下发空字典
    assert seen[1][2]["headers"] == {"Authorization": "Bearer jwt"}
    assert seen[2][1] == f"{BASE}/rest/api/v1/peers/x/queue"


async def test_request_401_relogins_once_then_succeeds():
    state = {"n": 0}

    def responder(method, url, kwargs):
        if url.endswith("/auth/login"):
            return json_response({"token": "fresh"})
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(status=401, text="expired")
        return json_response({"ok": 1})

    client, session = build(responder)
    client._token = "stale"
    assert await client._api_get("/users/me") == {"ok": 1}
    assert state["n"] == 2
    urls = [r[1] for r in session.requests]
    assert urls[0].endswith("/users/me")
    assert urls[1].endswith("/auth/login")
    assert urls[2].endswith("/users/me")
    assert client.token == "fresh"


async def test_request_403_relogins_once_then_succeeds():
    """403 与 401 走同一分支：清 token → 重新登录 → 重试一次即成功。"""
    state = {"n": 0}

    def responder(method, url, kwargs):
        if url.endswith("/auth/login"):
            return json_response({"token": "fresh"})
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(status=403, text="forbidden")
        return json_response({"ok": 1})

    client, session = build(responder)
    client._token = "stale"
    assert await client._api_get("/users/me") == {"ok": 1}
    assert state["n"] == 2
    urls = [r[1] for r in session.requests]
    assert urls[0].endswith("/users/me")
    assert urls[1].endswith("/auth/login")
    assert urls[2].endswith("/users/me")
    assert client.token == "fresh"


@pytest.mark.parametrize("status", [401, 403])
async def test_request_persistent_auth_failure_retries_once_then_raises(status):
    """服务端**持续**返回 401/403 时只重试一次，随后如实抛 MusicFlowAuthError。

    回归守卫：`_request` 的「只重试一次」标记必须由递归调用自己携带（`_retry_auth`
    参数），不能用实例状态——实例状态会被 `login()` 复位，导致重试无限递归直到
    RecursionError，`raise MusicFlowAuthError` 永远不可达。
    """
    state = {"n": 0}

    def responder(method, url, kwargs):
        if url.endswith("/auth/login"):
            return json_response({"token": "fresh"})
        state["n"] += 1
        return FakeResponse(status=status, text="always rejected")

    client, session = build(responder)
    client._token = "stale"
    with pytest.raises(MusicFlowAuthError, match=rf"认证失败\({status}\)"):
        await client._api_get("/users/me")
    # 业务请求恰好两次（首次 + 重试一次），绝无第三次
    assert state["n"] == 2
    # 只重新登录一次：重试期间的 token 不会再被 login() 复位成允许重试
    assert len([r for r in session.requests if r[1].endswith("/auth/login")]) == 1


async def test_request_other_error_status():
    client, _ = build(lambda m, u, k: FakeResponse(status=404, text="nope"))
    client._token = "jwt"
    with pytest.raises(MusicFlowError, match="失败 404") as err:
        await client._api_get("/nope")
    assert "nope" in str(err.value)


async def test_request_non_json_content_type_returns_none():
    client, _ = build(
        lambda m, u, k: FakeResponse(status=200, content_type="audio/mpeg", body=b"\x00")
    )
    client._token = "jwt"
    assert await client._api_get("/whatever") is None


async def test_request_timeout_and_client_error():
    client, _ = build(lambda m, u, k: (_ for _ in ()).throw(asyncio.TimeoutError()))
    client._token = "jwt"
    with pytest.raises(MusicFlowError, match="超时"):
        await client._api_get("/x")

    client, _ = build(lambda m, u, k: (_ for _ in ()).throw(aiohttp.ClientOSError("x")))
    client._token = "jwt"
    with pytest.raises(MusicFlowError, match="网络错误"):
        await client._api_get("/x")


async def test_request_json_decode_error_propagates_as_is():
    # 非 MusicFlowError 不在此处吞掉（json 解析异常原样抛出）
    client, _ = build(lambda m, u, k: json_response(ValueError("bad json")))
    client._token = "jwt"
    with pytest.raises(ValueError):
        await client._api_get("/x")


# ==================== Subsonic 解包 ====================
async def test_subsonic_ok_and_params():
    seen = {}

    def responder(method, url, kwargs):
        seen.update({"method": method, "url": url, "params": kwargs.get("params")})
        return subsonic_response({"status": "ok", "song": {"id": "s1"}})

    client, _ = build(responder)
    client._token = "jwt"
    out = await client._subsonic("getSong", {"id": "s1"})
    assert out["song"] == {"id": "s1"}
    assert seen["url"] == f"{BASE}/rest/getSong"
    assert seen["params"] == {"f": "json", "id": "s1"}


async def test_subsonic_non_dict_payload_returns_empty():
    client, _ = build(lambda m, u, k: json_response([1, 2, 3]))
    client._token = "jwt"
    assert await client._subsonic("getArtists") == {}


async def test_subsonic_auth_error_codes():
    for code in (40, 41):
        client, _ = build(
            lambda m, u, k, c=code: subsonic_response(
                {"status": "failed", "error": {"code": c, "message": "bad creds"}}
            )
        )
        client._token = "jwt"
        with pytest.raises(MusicFlowAuthError, match="bad creds"):
            await client._subsonic("getArtists")


async def test_subsonic_generic_error():
    client, _ = build(
        lambda m, u, k: subsonic_response(
            {"status": "failed", "error": {"code": 0, "message": "boom"}}
        )
    )
    client._token = "jwt"
    with pytest.raises(MusicFlowError, match="getArtists: boom"):
        await client._subsonic("getArtists")


async def test_subsonic_missing_error_message_uses_defaults():
    client, _ = build(lambda m, u, k: subsonic_response({"status": "failed"}))
    client._token = "jwt"
    with pytest.raises(MusicFlowError, match="未知错误"):
        await client._subsonic("getArtists")


async def test_async_verify():
    client, _ = build(lambda m, u, k: json_response({"username": "u"}))
    client._token = "jwt"
    assert await client.async_verify() == {"username": "u"}


# ==================== Peer 拉取 ====================
async def test_get_peers_variants():
    client, _ = build(lambda m, u, k: json_response({"peers": [{"peerId": "dlna:1"}]}))
    client._token = "jwt"
    assert await client.async_get_peers() == [{"peerId": "dlna:1"}]

    for payload in ({"peers": "nope"}, [1, 2], None):
        client, _ = build(lambda m, u, k, p=payload: json_response(p))
        client._token = "jwt"
        assert await client.async_get_peers() == []


async def test_get_peer_status_and_queue():
    client, session = build(lambda m, u, k: json_response({"state": "PLAYING"}))
    client._token = "jwt"
    assert await client.async_get_peer_status("group:g/1") == {"state": "PLAYING"}
    assert session.requests[0][1] == f"{BASE}/rest/api/v1/peers/group%3Ag%2F1/status"

    client, session = build(lambda m, u, k: json_response([1]))
    client._token = "jwt"
    assert await client.async_get_peer_status("dlna:x") == {}

    client, session = build(lambda m, u, k: json_response({"items": []}))
    client._token = "jwt"
    assert await client.async_get_peer_queue("dlna:x") == {"items": []}
    assert session.requests[0][1].endswith("/queue")  # size=0 不拼分页
    assert await client.async_get_peer_queue("dlna:x", offset=200, size=200) == {"items": []}
    assert session.requests[1][1].endswith("/queue?offset=200&size=200")
    assert session.requests[1][2]["params"] is None


# ==================== 传输控制 ====================
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("async_play", "/play"),
        ("async_pause", "/pause"),
        ("async_stop", "/stop"),
        ("async_next", "/next"),
        ("async_previous", "/prev"),
    ],
)
async def test_transport_control_paths(method, suffix):
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await getattr(client, method)("dlna:dev")
    verb, url, kwargs = session.requests[0]
    assert verb == "POST"
    assert url == f"{BASE}/rest/api/v1/peers/dlna%3Adev{suffix}"
    assert kwargs["json"] == {}


async def test_seek_aligns_to_whole_seconds():
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_seek("dlna:dev", 31.178)
    assert session.requests[0][2]["json"] == {"seconds": 31.0}
    await client.async_seek("dlna:dev", -4)
    assert session.requests[1][2]["json"] == {"seconds": 0.0}


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(150, 100), (-5, 0), (42.6, 43), (0, 0)],
)
async def test_set_volume_clamps_and_rounds(volume, expected):
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_set_volume("dlna:dev", volume)
    assert session.requests[0][2]["json"] == {"volume": expected}
    assert session.requests[0][1].endswith("/peers/dlna%3Adev/volume")


async def test_set_mute_and_play_mode():
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_set_mute("dlna:dev", 1)
    assert session.requests[0][2]["json"] == {"muted": True}
    await client.async_set_play_mode("dlna:dev", "shuffle")
    assert session.requests[1][2]["json"] == {"mode": "shuffle"}
    assert session.requests[1][1].endswith("/play-mode")


async def test_clear_queue_uses_delete():
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_clear_queue("group:g1")
    assert session.requests[0][0] == "DELETE"
    assert session.requests[0][1] == f"{BASE}/rest/api/v1/peers/group%3Ag1/queue"


async def test_queue_play_clamps_start_index():
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_queue_play("dlna:dev", [{"id": "s1"}], -3)
    assert session.requests[0][2]["json"] == {"items": [{"id": "s1"}], "startIndex": 0}
    await client.async_queue_play("dlna:dev", [], 5)
    assert session.requests[1][2]["json"] == {"items": [], "startIndex": 5}


async def test_announce_with_and_without_volume():
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    await client.async_announce("dlna:dev", "http://tts/1.mp3")
    assert session.requests[0][2]["json"] == {"url": "http://tts/1.mp3"}
    await client.async_announce("dlna:dev", "http://tts/2.mp3", volume=180)
    assert session.requests[1][2]["json"] == {"url": "http://tts/2.mp3", "volume": 100}


async def test_get_groups_variants():
    client, _ = build(lambda m, u, k: json_response({"groups": [{"id": "g1"}]}))
    client._token = "jwt"
    assert await client.async_get_groups() == [{"id": "g1"}]
    for payload in ({"groups": "x"}, None, 5):
        client, _ = build(lambda m, u, k, p=payload: json_response(p))
        client._token = "jwt"
        assert await client.async_get_groups() == []


# ==================== 内容播放（/v1/play）====================
async def test_play_content_song_id_takes_precedence():
    client, session = build(lambda m, u, k: json_response({"ok": 1}))
    client._token = "jwt"
    out = await client.async_play_content(
        "dlna:dev", "album", "a1", song_id="s9", start_index=7, play_mode="all", enqueue=True
    )
    assert out == {"ok": 1}
    body = session.requests[0][2]["json"]
    assert body == {
        "peerId": "dlna:dev",
        "type": "album",
        "id": "a1",
        "enqueue": True,
        "songId": "s9",
        "playMode": "all",
    }
    assert "startIndex" not in body


async def test_play_content_start_index_fallback_and_non_dict():
    client, session = build(lambda m, u, k: json_response([1]))
    client._token = "jwt"
    assert await client.async_play_content("dlna:dev", "song", "s1", start_index=3) == {}
    body = session.requests[0][2]["json"]
    assert body["startIndex"] == 3
    assert "songId" not in body
    assert "playMode" not in body


# ==================== OpenSubsonic 浏览包装 ====================
async def test_subsonic_wrappers_payloads():
    seen: list[tuple] = []

    def responder(method, url, kwargs):
        seen.append((url, kwargs.get("params")))
        return subsonic_response({"status": "ok"})

    client, _ = build(responder)
    client._token = "jwt"
    await client.async_get_artists()
    await client.async_get_artist("ar1")
    await client.async_get_album_list("alphabeticalByArtist", 42)
    await client.async_get_album("al1")
    await client.async_get_song("s1")
    await client.async_get_playlists()
    await client.async_get_playlist("pl1")
    await client.async_get_genres()
    await client.async_get_songs_by_genre("Rock", 7)
    await client.async_search("q")
    assert [s[0] for s in seen] == [
        f"{BASE}/rest/getArtists",
        f"{BASE}/rest/getArtist",
        f"{BASE}/rest/getAlbumList",
        f"{BASE}/rest/getAlbum",
        f"{BASE}/rest/getSong",
        f"{BASE}/rest/getPlaylists",
        f"{BASE}/rest/getPlaylist",
        f"{BASE}/rest/getGenres",
        f"{BASE}/rest/getSongsByGenre",
        f"{BASE}/rest/search3",
    ]
    assert seen[1][1] == {"f": "json", "id": "ar1"}
    assert seen[2][1] == {"f": "json", "type": "alphabeticalByArtist", "size": 42}
    assert seen[8][1] == {"f": "json", "genre": "Rock", "count": 7}
    assert seen[9][1]["query"] == "q"


async def test_get_song_returns_dict_or_empty():
    client, _ = build(lambda m, u, k: subsonic_response({"status": "ok", "song": {"id": "s1"}}))
    client._token = "jwt"
    assert await client.async_get_song("s1") == {"id": "s1"}
    client, _ = build(lambda m, u, k: subsonic_response({"status": "ok", "song": "x"}))
    client._token = "jwt"
    assert await client.async_get_song("s1") == {}


# ==================== 收藏 / 歌词 / 歌单编辑 ====================
async def test_star_unstar_and_starred():
    seen: list[tuple] = []

    def responder(method, url, kwargs):
        seen.append((url, kwargs.get("params")))
        if url.endswith("getStarred"):
            return subsonic_response(
                {"status": "ok", "starred": {"song": [{"id": 1}, {"id": None}, {"id": 2}]}}
            )
        return subsonic_response({"status": "ok"})

    client, _ = build(responder)
    client._token = "jwt"
    await client.async_star(["a", "b"])
    await client.async_unstar(["c"])
    assert await client.async_get_starred() == {"1", "2"}
    assert seen[0][1] == {"f": "json", "id": "a,b"}
    assert seen[1][1] == {"f": "json", "id": "c"}
    assert seen[2][1] == {"f": "json"}


async def test_get_starred_without_starred_key():
    client, _ = build(lambda m, u, k: subsonic_response({"status": "ok"}))
    client._token = "jwt"
    assert await client.async_get_starred() == set()


async def test_get_lyrics_parses_structured_lines():
    payload = subsonic_response(
        {
            "status": "ok",
            "lyricsList": {
                "structuredLyrics": [
                    {
                        "line": [
                            {"start": 1000, "value": "第一行"},
                            {"start": None, "value": "第二行"},
                            {"value": ""},  # 空值行丢弃
                            "not-a-dict",  # 非字典丢弃
                        ]
                    }
                ]
            },
        }
    )
    client, _ = build(lambda m, u, k: payload)
    client._token = "jwt"
    assert await client.async_get_lyrics("s1") == [
        {"start": 1000, "value": "第一行"},
        {"start": 0, "value": "第二行"},
    ]


async def test_get_lyrics_empty():
    client, _ = build(lambda m, u, k: subsonic_response({"status": "ok"}))
    client._token = "jwt"
    assert await client.async_get_lyrics("s1") == []


async def test_add_to_playlist():
    seen = {}

    def responder(method, url, kwargs):
        seen.update(kwargs.get("params") or {})
        return subsonic_response({"status": "ok"})

    client, _ = build(responder)
    client._token = "jwt"
    await client.async_add_to_playlist("pl1", ["s1", "s2"])
    assert seen == {"f": "json", "playlistId": "pl1", "songIdToAdd": "s1,s2"}


# ==================== 直链 / 封面 ====================
def test_cover_url_and_stream_url():
    client, _ = build()
    assert client.cover_url(None) is None
    assert client.cover_url("") is None
    assert (
        client.cover_url("a b/c")
        == f"{BASE}/rest/getCoverArt?id=a%20b%2Fc&size=400"
    )
    assert (
        client.cover_url("x", size=64) == f"{BASE}/rest/getCoverArt?id=x&size=64"
    )
    client._token = "tok en"
    assert client.stream_url("s 1") == f"{BASE}/rest/stream?id=s%201&token=tok%20en"


async def test_fetch_image_success_and_failure():
    def ok(m, u, k):
        return FakeResponse(status=200, body=b"img-bytes", content_type="image/webp")

    client, session = build(ok)
    client._token = "jwt"
    assert await client.async_fetch_image(f"{BASE}/rest/getCoverArt?id=1") == (
        b"img-bytes",
        "image/webp",
    )
    assert session.requests[0][2]["headers"] == {"Authorization": "Bearer jwt"}

    for status in (404, 500):
        client, _ = build(lambda m, u, k, s=status: FakeResponse(status=s))
        client._token = "jwt"
        assert await client.async_fetch_image("http://x/y") == (None, None)

    for exc in (asyncio.TimeoutError(), aiohttp.ClientConnectionError("down")):
        client, _ = build(lambda m, u, k, e=exc: (_ for _ in ()).throw(e))
        client._token = "jwt"
        assert await client.async_fetch_image("http://x/y") == (None, None)


# ==================== WS 派发 ====================
def test_add_listener_and_dispatch():
    client, _ = build()
    got: list[dict] = []
    client.add_listener(got.append)
    client._dispatch({"type": "peer_snapshot"})
    assert got == [{"type": "peer_snapshot"}]


def test_dispatch_isolates_listener_exceptions():
    client, _ = build()
    got: list[dict] = []

    def boom(_payload):
        raise RuntimeError("listener failed")

    client.add_listener(boom)
    client.add_listener(got.append)
    client._dispatch({"type": "group_changed"})  # 不应被前一个 listener 打断
    assert got == [{"type": "group_changed"}]


async def test_ws_connect_and_listen_dispatch():
    messages = [
        FakeWSMessage(payload={"type": "peer_snapshot"}, mtype=aiohttp.WSMsgType.TEXT),
        FakeWSMessage(payload="not-a-dict", mtype=aiohttp.WSMsgType.TEXT),
        FakeWSMessage(payload=ValueError("bad json"), mtype=aiohttp.WSMsgType.TEXT),
        FakeWSMessage(payload={"type": "ignored"}, mtype=aiohttp.WSMsgType.BINARY),
        FakeWSMessage(payload={"type": "never"}, mtype=aiohttp.WSMsgType.CLOSED),
        FakeWSMessage(payload={"type": "never"}, mtype=aiohttp.WSMsgType.TEXT),
    ]
    ws = FakeWebSocket(messages=messages)
    client, session = build(
        lambda m, u, k: json_response({"token": "jwt"}), ws_factory=lambda u, k: ws
    )
    got: list[dict] = []
    client.add_listener(got.append)
    await client.async_ws_connect()
    assert client._ws is ws
    # 连接前会先 _ensure_token()（此处无 token → 先发一条登录请求），
    # WS 握手在记录里单独以 "WS" 标记，需筛出来断言。
    ws_calls = [r for r in session.requests if r[0] == "WS"]
    assert len(ws_calls) == 1
    assert ws_calls[0][1] == "ws://mf.local:46400/ws?token=jwt"
    assert ws_calls[0][2]["heartbeat"] == 30
    await client.async_ws_listen()
    # 只派发 TEXT + dict；CLOSED 即中断，后续消息不处理
    assert got == [{"type": "peer_snapshot"}]


async def test_ws_listen_without_connection_returns():
    client, _ = build()
    await client.async_ws_listen()  # 未连接 → 直接返回，不抛异常


async def test_ws_listen_breaks_on_error_type():
    ws = FakeWebSocket(
        messages=[FakeWSMessage(payload={"type": "x"}, mtype=aiohttp.WSMsgType.ERROR)]
    )
    client, _ = build(ws_factory=lambda u, k: ws)
    client._token = "jwt"
    await client.async_ws_connect()
    await client.async_ws_listen()


async def test_ws_close_is_idempotent():
    ws = FakeWebSocket()
    client, _ = build(ws_factory=lambda u, k: ws)
    client._token = "jwt"
    assert await client.async_ws_close() is None  # 未连接时分支
    await client.async_ws_connect()
    await client.async_ws_close()
    assert ws.close_calls == 1
    assert client._ws is None
    await client.async_ws_close()
    assert ws.close_calls == 1


async def test_request_builds_encoded_url():
    """请求 URL 用 encoded=True 构造：路径里的 %2F 不会被二次编码拆开。"""
    client, session = build(lambda m, u, k: json_response({}))
    client._token = "jwt"
    # path 必须是带前导 `/` 的完整路径（_request 直接拼在 base_url 之后）
    await client._request("GET", f"/{client._encode('dlna:a/b')}")
    assert session.requests[0][1].endswith("dlna%3Aa%2Fb")
