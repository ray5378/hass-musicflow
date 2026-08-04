"""MusicFlow 集成入口。

负责创建 API 客户端与协调器,启动 WebSocket 监听,
并加载 media_player 平台。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MusicFlowClient
from .const import CONF_API_KEY, CONF_URL, DOMAIN
from .coordinator import MusicFlowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["media_player"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """设置集成:创建 client + coordinator,启动 WS 监听。"""
    session = async_get_clientsession(hass)
    client = MusicFlowClient(session, entry.data[CONF_URL], entry.data[CONF_API_KEY])

    coordinator = MusicFlowCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    try:
        await coordinator.start_listening()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("MusicFlow WebSocket 暂时无法连接,将自动重连: %s", err)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 配置项变更时重载
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载:断开 WS,清理数据。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MusicFlowCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """配置变更时重载集成。"""
    await hass.config_entries.async_reload(entry.entry_id)
