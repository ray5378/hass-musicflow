"""MusicFlow 集成入口。

创建 API 客户端 + 协调器,注册服务器"网关"设备(各播放器设备以 via_device
挂在它下面),然后加载 media_player 平台。WebSocket 在后台任务里自管重连。
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, async_get as async_get_device_registry

from .api import MusicFlowClient, MusicFlowError
from .const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, DOMAIN
from .coordinator import MusicFlowCoordinator
from .proxy import MusicFlowProxyView, _ws_subscribe

_LOGGER = logging.getLogger(__name__)

# 只列"实体平台"。media_source 不是实体平台(Platform 枚举里根本没有这个成员),
# HA 通过 async_process_integration_platforms 自动发现本包的 media_source.py
# 并调用其中的 async_get_media_source(),不能也不需要走 async_forward_entry_setups。
PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """注册 WebSocket 命令(自定义卡片用)与 REST 代理视图,只执行一次。"""
    websocket_api.async_register_command(hass, _ws_lyrics)
    websocket_api.async_register_command(hass, _ws_playlists)
    websocket_api.async_register_command(hass, _ws_backend_config)
    # 卡片代理模式(外网访问)的事件通道:订阅后端 WS 并转发
    websocket_api.async_register_command(hass, _ws_subscribe)
    # 卡片代理模式的 REST 通道:转发 /rest/*(含封面等二进制响应)
    hass.http.register_view(MusicFlowProxyView(hass))
    return True


def _coordinator_for_entity(
    hass: HomeAssistant, entity_id: str
) -> MusicFlowCoordinator | None:
    """按实体 id 找到它所属配置项的 coordinator。"""
    registry = er.async_get(hass)
    entity = registry.async_get(entity_id)
    entry_id = entity.config_entry_id if entity else None
    return hass.data.get(DOMAIN, {}).get(entry_id or "")


@websocket_api.websocket_command(
    {
        vol.Required("type"): "musicflow/lyrics",
        vol.Required("entity_id"): str,
        vol.Required("song_id"): str,
    }
)
@websocket_api.async_response
async def _ws_lyrics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """返回结构化歌词行 [{start: 毫秒, value: str}],供卡片滚动高亮。

    卡片在检测到 song_id 变化时调一次即可;歌词按时间轴滚动在卡片本地做。
    """
    coordinator = _coordinator_for_entity(hass, msg["entity_id"])
    if coordinator is None:
        raise HomeAssistantError("MusicFlow 服务器未加载")
    try:
        lines = await coordinator.client.async_get_lyrics(msg["song_id"])
    except MusicFlowError as err:
        raise HomeAssistantError(str(err)) from err
    connection.send_result(msg["id"], {"lines": lines})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "musicflow/playlists",
        vol.Required("entity_id"): str,
    }
)
@websocket_api.async_response
async def _ws_playlists(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """返回当前用户可添加的歌单列表 [{id, name}],供卡片「添加到歌单」下拉。"""
    coordinator = _coordinator_for_entity(hass, msg["entity_id"])
    if coordinator is None:
        raise HomeAssistantError("MusicFlow 服务器未加载")
    try:
        resp = await coordinator.client.async_get_playlists()
    except MusicFlowError as err:
        raise HomeAssistantError(str(err)) from err
    playlists = [
        {"id": str(p.get("id")), "name": p.get("name") or "未命名歌单"}
        for p in resp.get("playlists", {}).get("playlist") or []
        if p.get("id") is not None
    ]
    connection.send_result(msg["id"], {"playlists": playlists})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "musicflow/backend_config",
    }
)
@websocket_api.async_response
async def _ws_backend_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """返回后端连接信息(url + api_key),供 MusicFlow 前端卡片直连 /ws + REST。

    每个已加载的配置项贡献一个后端;卡片默认用第一个(单服务器场景)。
    """
    backends = hass.data.get(DOMAIN, {}).get("_backends", {})
    configs = [v for v in backends.values() if v]
    connection.send_result(msg["id"], {"backends": configs})


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

    # 供 MusicFlow 前端卡片直连后端:暴露 url + api_key(卡片经 HA WS 取走后
    # 直连后端 /ws + REST,实现与 Web/App 平等的实时双向同步);
    # proxySupported 表示本集成提供 REST 代理 + 事件订阅,卡片外网访问失败时
    # 可自动切换经 HA 中转(API Key 不下发浏览器,只存在 HA 侧)。
    backends = hass.data[DOMAIN].setdefault("_backends", {})
    backends[entry.entry_id] = {
        "url": entry.data[CONF_URL],
        "api_key": entry.data[CONF_API_KEY],
        "proxySupported": True,
        "verify_ssl": entry.data.get(CONF_VERIFY_SSL, True),
    }

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
        hass.data[DOMAIN].get("_backends", {}).pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
