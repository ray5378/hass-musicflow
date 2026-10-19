"""__init__.py 单测（简体中文）。

锁死对象：集成加载全链路 —— async_setup（WS 命令 + 代理视图注册）、
async_migrate_entry（v1→v2 迁移）、async_setup_entry（缺凭据→重新认证、
client/coordinator/backends/网关设备/平台转发）、async_unload_entry，
以及三个自定义 WS 命令的取值与错误映射。
"""

from __future__ import annotations

import pytest
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.musicflow import (
    DOMAIN,
    _ws_backend_config,
    _ws_lyrics,
    _ws_playlists,
    async_migrate_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
import custom_components.musicflow as init_mod
from custom_components.musicflow.api import MusicFlowError
from custom_components.musicflow.const import (
    CONF_URL,
    CONF_USERNAME,
)


class FakeConnection:
    """最小可用的 ActiveConnection 替身。"""

    def __init__(self) -> None:
        self.results: list[tuple] = []
        self.messages: list[dict] = []
        self.subscriptions: dict[int, object] = {}

    def send_result(self, msg_id, result) -> None:
        self.results.append((msg_id, result))

    def send_message(self, message) -> None:
        self.messages.append(message)


# ==================== async_setup ====================
async def test_async_setup_registers_ws_commands_and_view(hass):
    assert await async_setup_component(hass, "http", {}) is True
    assert await async_setup(hass, {}) is True
    handlers = hass.data.get(websocket_api.DOMAIN, {})
    for command in (
        "musicflow/lyrics",
        "musicflow/playlists",
        "musicflow/backend_config",
        "musicflow/subscribe",
    ):
        assert command in handlers
    # 代理视图已注册到 HA 路由（aiohttp 会把 `{tail:.*}` 规范化为 `{tail}`，
    # 且 ResourceRoute 不保留视图自定义的 name，只能按 canonical 前缀判定）
    assert any(
        str(getattr(resource, "canonical", "")).startswith("/api/musicflow/rest")
        for resource in hass.http.app.router.resources()
    )


# ==================== async_migrate_entry ====================
async def test_async_migrate_entry_v1_to_v2(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data={"api_key": "old-token", CONF_URL: "http://x"}
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert "api_key" not in entry.data
    assert entry.data[CONF_URL] == "http://x"


async def test_async_migrate_entry_v2_is_noop(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_URL: "http://x"})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


# ==================== async_setup_entry ====================
async def test_setup_entry_missing_credentials_raises_auth_failed(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={CONF_URL: "http://mf.local:46400"}
    )
    entry.add_to_hass(hass)
    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_and_unload_entry(hass, fake_client, mock_entry, monkeypatch):
    monkeypatch.setattr(init_mod, "MusicFlowClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(
        init_mod, "async_get_clientsession", lambda hass, verify_ssl=True: object()
    )
    fake_client.responses["async_get_peers"] = []
    entry = mock_entry
    entry.add_to_hass(hass)

    # 走 HA 的配置项状态机（会加 setup_lock 并把 state 推进到 LOADED）。
    # 直接调 async_setup_entry 会因 entry.state 仍为 NOT_LOADED 而在
    # async_forward_entry_setups 触发 OperationNotAllowed。
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.client is fake_client

    backend = hass.data[DOMAIN]["_backends"][entry.entry_id]
    assert backend["url"] == entry.data[CONF_URL]
    assert backend["username"] == entry.data[CONF_USERNAME]
    assert backend["client"] is fake_client
    assert backend["proxySupported"] is True

    # 网关设备（服务器本体）已注册
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    assert device is not None
    assert device.entry_type == dr.DeviceEntryType.SERVICE

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert entry.entry_id not in hass.data.get(DOMAIN, {}).get("_backends", {})


# ==================== WS 命令 ====================
async def test_ws_lyrics_ok(hass, coordinator, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: coordinator)
    coordinator.client.responses["async_get_lyrics"] = [{"start": 0, "value": "词"}]
    conn = FakeConnection()
    await _ws_lyrics.__wrapped__(hass, conn, {"id": 1, "entity_id": "media_player.x", "song_id": "s1"})
    assert conn.results == [(1, {"lines": [{"start": 0, "value": "词"}]})]


async def test_ws_lyrics_no_coordinator(hass, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: None)
    conn = FakeConnection()
    with pytest.raises(HomeAssistantError):
        await _ws_lyrics.__wrapped__(hass, conn, {"id": 1, "entity_id": "x", "song_id": "s1"})


async def test_ws_lyrics_musicflow_error(hass, coordinator, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: coordinator)
    coordinator.client.raises["async_get_lyrics"] = MusicFlowError("boom")
    conn = FakeConnection()
    with pytest.raises(HomeAssistantError):
        await _ws_lyrics.__wrapped__(hass, conn, {"id": 1, "entity_id": "x", "song_id": "s1"})


async def test_ws_playlists_ok(hass, coordinator, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: coordinator)
    coordinator.client.responses["async_get_playlists"] = {
        "playlists": {"playlist": [{"id": "p1", "name": "夜"}, {"name": "无 id"}]}
    }
    conn = FakeConnection()
    await _ws_playlists.__wrapped__(hass, conn, {"id": 2, "entity_id": "x"})
    assert conn.results == [(2, {"playlists": [{"id": "p1", "name": "夜"}]})]


async def test_ws_playlists_unnamed_and_error(hass, coordinator, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: coordinator)
    coordinator.client.responses["async_get_playlists"] = {
        "playlists": {"playlist": [{"id": "p2"}]}
    }
    conn = FakeConnection()
    await _ws_playlists.__wrapped__(hass, conn, {"id": 3, "entity_id": "x"})
    assert conn.results == [(3, {"playlists": [{"id": "p2", "name": "未命名歌单"}]})]


async def test_ws_playlists_no_coordinator(hass, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: None)
    conn = FakeConnection()
    with pytest.raises(HomeAssistantError):
        await _ws_playlists.__wrapped__(hass, conn, {"id": 1, "entity_id": "x"})


async def test_ws_playlists_musicflow_error(hass, coordinator, monkeypatch):
    monkeypatch.setattr(init_mod, "_coordinator_for_entity", lambda h, e: coordinator)
    coordinator.client.raises["async_get_playlists"] = MusicFlowError("boom")
    conn = FakeConnection()
    with pytest.raises(HomeAssistantError):
        await _ws_playlists.__wrapped__(hass, conn, {"id": 1, "entity_id": "x"})


async def test_ws_backend_config(hass):
    hass.data.setdefault(DOMAIN, {})["_backends"] = {
        "e1": {
            "url": "http://mf.local:46400",
            "username": "alice",
            "password": "secret",
            "proxySupported": True,
            "verify_ssl": True,
            "client": object(),
        },
        "e2": None,  # 空后端被过滤掉
    }
    conn = FakeConnection()
    await _ws_backend_config.__wrapped__(hass, conn, {"id": 5})
    (msg_id, result), = conn.results
    assert msg_id == 5
    assert result["backends"] == [
        {
            "url": "http://mf.local:46400",
            "username": "alice",
            "password": "secret",
            "proxySupported": True,
            "verify_ssl": True,
        }
    ]
    assert "client" not in result["backends"][0]  # 不下发不可序列化对象


async def test_ws_backend_config_empty(hass):
    conn = FakeConnection()
    await _ws_backend_config.__wrapped__(hass, conn, {"id": 6})
    assert conn.results == [(6, {"backends": []})]
