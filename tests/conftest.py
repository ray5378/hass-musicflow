"""pytest 公共夹具（简体中文）。

职责：
1. 把仓库根加入 sys.path，让 `custom_components.musicflow` 可被导入
   （pytest 默认只把 tests/ 加进 sys.path）。
2. 让 HA 官方测试框架的 `enable_custom_integrations` 对所有用例自动生效。
3. 提供「假客户端 / 假协调器」等公共夹具，避免每个用例重复造轮子。

注意：pytest_homeassistant_custom_component 已在 pytest.ini 的 addopts 里用
`-p` 注册（不在本文件写 pytest_plugins —— 子目录 conftest 里写 pytest_plugins
会被新版 pytest 拒绝）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from custom_components.musicflow.const import DOMAIN  # noqa: E402

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ImportError:  # pragma: no cover - 插件缺失时给出明确提示
    MockConfigEntry = None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """让 homeassistant 测试框架允许加载 custom_components/ 下的集成。"""
    yield


class FakeMusicFlowClient:
    """可编程的假 MusicFlow 客户端。

    - `responses`：方法名 → 返回值，未配置时用各方法自身的默认值；
    - `raises`：方法名 → 异常，用于模拟网络/认证故障；
    - `calls`：记录 (方法名, 位置参数, 关键字参数)，断言下发的 URL/参数。
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        raises: dict[str, BaseException] | None = None,
    ) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.raises: dict[str, BaseException] = dict(raises or {})
        self.calls: list[tuple[str, tuple, dict]] = []
        self.listeners: list = []
        self._token: str | None = "fake-jwt"
        self.base_url = "http://mf.local:46400"
        self.closed = False

    # ==================== 记录 / 派发 ====================
    def _invoke(self, name: str, args: tuple = (), kwargs: dict | None = None, default: Any = None):
        self.calls.append((name, args, kwargs or {}))
        if name in self.raises:
            raise self.raises[name]
        if name in self.responses:
            return self.responses[name]
        return default

    def called(self, name: str) -> list[tuple[str, tuple, dict]]:
        """取出某方法的全部调用记录。"""
        return [c for c in self.calls if c[0] == name]

    def emit(self, payload: dict) -> None:
        """模拟后端 WS 推送，派发给协调器注册的监听器。"""
        for listener in list(self.listeners):
            listener(payload)

    # ==================== 元信息 ====================
    @property
    def token(self) -> str | None:
        return self._token

    @property
    def ws_url(self) -> str:
        return f"ws://mf.local:46400/ws?token={self._token or ''}"

    def add_listener(self, callback) -> None:
        self.listeners.append(callback)

    def cover_url(self, cover_art: str | None, size: int = 400) -> str | None:
        """与 api.MusicFlowClient.cover_url 同形：无 coverArt 返回 None。"""
        self.calls.append(("cover_url", (cover_art, size), {}))
        if not cover_art:
            return None
        return f"http://mf.local:46400/rest/getCoverArt?id={cover_art}&size={size}"

    def stream_url(self, song_id: str) -> str:
        return f"http://mf.local:46400/rest/stream?id={song_id}&token={self._token or ''}"

    # ==================== 数据拉取 ====================
    async def async_get_peers(self):
        return self._invoke("async_get_peers", default=[])

    async def async_get_peer_status(self, peer_id: str):
        return self._invoke("async_get_peer_status", (peer_id,), default={})

    async def async_get_peer_queue(self, peer_id: str, *, offset: int = 0, size: int = 0):
        return self._invoke("async_get_peer_queue", (peer_id,), {"offset": offset, "size": size}, default={})

    async def async_get_groups(self):
        return self._invoke("async_get_groups", default=[])

    async def async_get_starred(self):
        return self._invoke("async_get_starred", default=set())

    async def async_get_lyrics(self, song_id: str):
        return self._invoke("async_get_lyrics", (song_id,), default=[])

    async def async_get_playlists(self):
        return self._invoke("async_get_playlists", default={})

    async def async_get_playlist(self, playlist_id: str):
        return self._invoke("async_get_playlist", (playlist_id,), default={})

    async def async_get_album_list(self, list_type: str = "newest", size: int = 300):
        return self._invoke("async_get_album_list", (list_type, size), default={})

    async def async_get_album(self, album_id: str):
        return self._invoke("async_get_album", (album_id,), default={})

    async def async_get_artists(self):
        return self._invoke("async_get_artists", default={})

    async def async_get_artist(self, artist_id: str):
        return self._invoke("async_get_artist", (artist_id,), default={})

    async def async_get_genres(self):
        return self._invoke("async_get_genres", default={})

    async def async_get_songs_by_genre(self, genre: str, count: int = 300):
        return self._invoke("async_get_songs_by_genre", (genre, count), default={})

    async def async_get_song(self, song_id: str):
        return self._invoke("async_get_song", (song_id,), default={})

    async def async_search(self, query: str, count: int = 60):
        return self._invoke("async_search", (query, count), default={})

    # ==================== 控制 ====================
    async def async_play(self, peer_id: str):
        return self._invoke("async_play", (peer_id,))

    async def async_pause(self, peer_id: str):
        return self._invoke("async_pause", (peer_id,))

    async def async_stop(self, peer_id: str):
        return self._invoke("async_stop", (peer_id,))

    async def async_next(self, peer_id: str):
        return self._invoke("async_next", (peer_id,))

    async def async_previous(self, peer_id: str):
        return self._invoke("async_previous", (peer_id,))

    async def async_seek(self, peer_id: str, seconds: float):
        return self._invoke("async_seek", (peer_id, seconds))

    async def async_set_volume(self, peer_id: str, volume: int):
        return self._invoke("async_set_volume", (peer_id, volume))

    async def async_set_mute(self, peer_id: str, muted: bool):
        return self._invoke("async_set_mute", (peer_id, muted))

    async def async_set_play_mode(self, peer_id: str, mode: str):
        return self._invoke("async_set_play_mode", (peer_id, mode))

    async def async_clear_queue(self, peer_id: str):
        return self._invoke("async_clear_queue", (peer_id,))

    async def async_queue_play(self, peer_id: str, items: list, start_index: int = 0):
        return self._invoke("async_queue_play", (peer_id, items, start_index))

    async def async_announce(self, peer_id: str, url: str, *, volume: int | None = None):
        return self._invoke("async_announce", (peer_id, url), {"volume": volume})

    async def async_play_content(self, peer_id: str, content_type: str, content_id: str, **kwargs):
        return self._invoke(
            "async_play_content", (peer_id, content_type, content_id), kwargs, default={}
        )

    async def async_star(self, song_ids: list):
        return self._invoke("async_star", (song_ids,))

    async def async_unstar(self, song_ids: list):
        return self._invoke("async_unstar", (song_ids,))

    async def async_add_to_playlist(self, playlist_id: str, song_ids: list):
        return self._invoke("async_add_to_playlist", (playlist_id, song_ids))

    async def async_fetch_image(self, url: str):
        return self._invoke("async_fetch_image", (url,), default=(b"img", "image/png"))

    # ==================== WebSocket ====================
    async def async_ws_connect(self):
        return self._invoke("async_ws_connect", default=None)

    async def async_ws_listen(self):
        return self._invoke("async_ws_listen")

    async def async_ws_close(self):
        self.closed = True
        return self._invoke("async_ws_close")


@pytest.fixture
def fake_client() -> FakeMusicFlowClient:
    """默认的假客户端（可用 .responses / .raises 现场改造）。"""
    return FakeMusicFlowClient()


@pytest.fixture
def make_client():
    """工厂夹具：按需构造多个假客户端。"""
    return FakeMusicFlowClient


def make_mock_entry(**kwargs):
    """构造一个可用的 MockConfigEntry（默认 MusicFlow 的合法凭据）。"""
    data = {
        "url": "http://mf.local:46400",
        "username": "user",
        "password": "pass",
        "verify_ssl": True,
    }
    data.update(kwargs.pop("data", {}) or {})
    kwargs.setdefault("entry_id", "test_entry_id")
    kwargs.setdefault("domain", DOMAIN)
    kwargs.setdefault("version", 2)
    return MockConfigEntry(data=data, **kwargs)


def make_coordinator(hass, client, entry=None):
    """构造一个真实的 MusicFlowCoordinator（供实体 / 协调器用例复用）。"""
    from custom_components.musicflow.coordinator import MusicFlowCoordinator

    return MusicFlowCoordinator(hass, entry or make_mock_entry(), client)


@pytest.fixture
def mock_entry():
    """一个凭据合法、版本为 2 的 MockConfigEntry。"""
    return make_mock_entry()


@pytest.fixture
async def coordinator(hass, mock_entry, fake_client):
    """真实协调器 + 假客户端（hass 由官方测试框架提供）。

    用例结束后必须 shutdown：DataUpdateCoordinator 自带轮询定时器与
    「请求刷新」防抖器，不清理会被 pytest-homeassistant-custom-component
    的 teardown 判定为 Lingering timer 而报错。
    """
    from custom_components.musicflow.coordinator import MusicFlowCoordinator

    coordinator = MusicFlowCoordinator(hass, mock_entry, fake_client)
    yield coordinator
    await coordinator.async_shutdown()

