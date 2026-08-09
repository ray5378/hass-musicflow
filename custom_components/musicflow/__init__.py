"""MusicFlow 集成入口。

创建 API 客户端 + 协调器,注册服务器"网关"设备(各播放器设备以 via_device
挂在它下面),然后加载 media_player 平台。WebSocket 在后台任务里自管重连。
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, async_get as async_get_device_registry

from .api import MusicFlowClient
from .const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, DOMAIN
from .coordinator import MusicFlowCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER, Platform.MEDIA_SOURCE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """建立 client + coordinator,首刷成功后加载平台。"""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)
    )
    client = MusicFlowClient(session, entry.data[CONF_URL], entry.data[CONF_API_KEY])

    coordinator = MusicFlowCoordinator(hass, entry, client)
    # 首刷失败(网络不通/认证过期)会由 HA 自动重试或拉起重新认证
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # 服务器本体作为网关设备,播放器设备通过 via_device 归拢到它下面
    async_get_device_registry(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="MusicFlow",
        name=entry.title,
        model="MusicFlow 服务器",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=entry.data[CONF_URL],
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # WS 放在平台加载之后启动:此时实体已存在,推送来了能立刻反映到状态上
    await coordinator.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载:关 WS、清 hass.data。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MusicFlowCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
