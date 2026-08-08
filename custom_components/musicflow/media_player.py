"""MusicFlow media_player 平台。

每个"可控 peer"对应一个实体:
  - `dlna:<deviceId>` → 一台 DLNA 渲染器
  - `group:<groupId>` → 一个播放器组

`local:<userId>`(Web 客户端本地播放)不建实体 —— 它的音频跑在浏览器 Howl 里,
后端只存队列元数据,HA 发命令过去不会有声音。

所有控制都走 `/rest/api/v1/peers/:peerId/*`,内容点播走 `/rest/api/v1/play`,
队列构造/mime 推断/封面回退全在后端,集成侧不重复实现。
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MusicFlowError
from .browse_media import build_browse_media, parse_media_id
from .const import (
    ATTR_CONTENT_ID,
    ATTR_CONTENT_TYPE,
    ATTR_ENQUEUE,
    ATTR_PLAY_MODE,
    ATTR_START_INDEX,
    DOMAIN,
    MEDIA_URI_PREFIX,
    PEER_KIND_GROUP,
    PLAY_MODES,
    PLAYABLE_TYPES,
    SERVICE_CLEAR_QUEUE,
    SERVICE_PLAY_CONTENT,
    SERVICE_SET_PLAY_MODE,
)
from .coordinator import MusicFlowCoordinator, PeerState

_LOGGER = logging.getLogger(__name__)

# 后端 DeviceStatus.state(AVTransport CurrentTransportState)→ HA 状态
STATE_MAP: dict[str, MediaPlayerState] = {
    "PLAYING": MediaPlayerState.PLAYING,
    "PAUSED_PLAYBACK": MediaPlayerState.PAUSED,
    "PAUSED_RECORDING": MediaPlayerState.PAUSED,
    "TRANSITIONING": MediaPlayerState.BUFFERING,
    "STOPPED": MediaPlayerState.IDLE,
    "NO_MEDIA_PRESENT": MediaPlayerState.IDLE,
    # 后端 PlaybackState 枚举(组状态可能直接给这个)
    "PAUSED": MediaPlayerState.PAUSED,
    "BUFFERING": MediaPlayerState.BUFFERING,
    "IDLE": MediaPlayerState.IDLE,
}

# playMode ↔ HA repeat/shuffle
PLAY_MODE_TO_REPEAT = {
    "order": RepeatMode.OFF,
    "shuffle": RepeatMode.OFF,
    "one": RepeatMode.ONE,
    "all": RepeatMode.ALL,
}

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """建立实体,并在后端发现新设备/新组时动态补建。"""
    coordinator: MusicFlowCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _async_add_new_peers() -> None:
        new_entities = [
            MusicFlowMediaPlayer(coordinator, entry, peer.peer_id)
            for peer in coordinator.controllable_peers()
            if peer.peer_id not in known
        ]
        if not new_entities:
            return
        known.update(e.peer_id for e in new_entities)
        async_add_entities(new_entities)

    _async_add_new_peers()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_peers))

    # ---- 自定义服务(暴露后端特有能力:按内容类型点播 / 播放模式 / 清空队列)----
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_PLAY_CONTENT,
        {
            vol.Required(ATTR_CONTENT_TYPE): vol.In(PLAYABLE_TYPES),
            vol.Required(ATTR_CONTENT_ID): cv.string,
            vol.Optional(ATTR_START_INDEX, default=0): vol.Coerce(int),
            vol.Optional(ATTR_PLAY_MODE): vol.In(PLAY_MODES),
            vol.Optional(ATTR_ENQUEUE, default=False): cv.boolean,
        },
        "async_play_content",
    )
    platform.async_register_entity_service(
        SERVICE_SET_PLAY_MODE,
        {vol.Required(ATTR_PLAY_MODE): vol.In(PLAY_MODES)},
        "async_apply_play_mode",
    )
    platform.async_register_entity_service(
        SERVICE_CLEAR_QUEUE, {}, "async_clear_playlist"
    )


class MusicFlowMediaPlayer(CoordinatorEntity[MusicFlowCoordinator], MediaPlayerEntity):
    """一个 MusicFlow peer 的播放器实体。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(
        self,
        coordinator: MusicFlowCoordinator,
        entry: ConfigEntry,
        peer_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.peer_id = peer_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{peer_id}"

    # ==================== 基础信息 ====================
    @property
    def _peer(self) -> PeerState | None:
        return self.coordinator.get_peer(self.peer_id)

    @property
    def _status(self) -> dict[str, Any]:
        peer = self._peer
        return peer.status if peer else {}

    @property
    def _queue(self) -> dict[str, Any]:
        peer = self._peer
        return peer.queue if peer else {}

    @property
    def device_info(self) -> DeviceInfo:
        peer = self._peer
        is_group = peer is not None and peer.kind == PEER_KIND_GROUP
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}:{self.peer_id}")},
            name=peer.name if peer else self.peer_id,
            manufacturer="MusicFlow",
            model="播放器组" if is_group else "DLNA 渲染器",
            via_device=(DOMAIN, self._entry.entry_id),
        )

    @property
    def available(self) -> bool:
        peer = self._peer
        return bool(peer and peer.available) and self.coordinator.last_update_success

    # ==================== 播放状态 ====================
    @property
    def state(self) -> MediaPlayerState:
        peer = self._peer
        if peer is None or not peer.available:
            return MediaPlayerState.OFF
        raw = str(self._status.get("state") or "").upper()
        mapped = STATE_MAP.get(raw)
        if mapped is not None:
            return mapped
        # 没拿到传输状态时,用队列是否活跃兜个底
        if self._queue.get("isActive"):
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        volume = self._status.get("volume")
        if isinstance(volume, (int, float)):
            return max(0.0, min(1.0, volume / 100))
        return None

    @property
    def media_position(self) -> int | None:
        position = self._status.get("position")
        return int(position) if isinstance(position, (int, float)) else None

    @property
    def media_position_updated_at(self):
        peer = self._peer
        return peer.status_updated_at if peer else None

    @property
    def media_duration(self) -> int | None:
        duration = self._status.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            return int(duration)
        item = self._current_item
        item_duration = item.get("duration") if item else None
        return int(item_duration) if isinstance(item_duration, (int, float)) and item_duration > 0 else None

    @property
    def _current_item(self) -> dict[str, Any] | None:
        peer = self._peer
        return peer.current_item if peer else None

    @property
    def media_title(self) -> str | None:
        item = self._current_item
        return item.get("title") if item else None

    @property
    def media_artist(self) -> str | None:
        item = self._current_item
        return item.get("artist") if item else None

    @property
    def media_album_name(self) -> str | None:
        item = self._current_item
        return item.get("album") if item else None

    @property
    def media_content_id(self) -> str | None:
        item = self._current_item
        song_id = item.get("songId") if item else None
        return f"{MEDIA_URI_PREFIX}song/{song_id}" if song_id else None

    @property
    def media_image_url(self) -> str | None:
        item = self._current_item
        if not item:
            return None
        return self.coordinator.client.cover_url(item.get("coverArt"))

    @property
    def shuffle(self) -> bool:
        return self._queue.get("playMode") == "shuffle"

    @property
    def repeat(self) -> RepeatMode:
        return PLAY_MODE_TO_REPEAT.get(self._queue.get("playMode", "order"), RepeatMode.OFF)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        peer = self._peer
        queue = self._queue
        items = queue.get("items") or []
        return {
            "peer_id": self.peer_id,
            "peer_kind": peer.kind if peer else None,
            "play_mode": queue.get("playMode"),
            "queue_size": len(items),
            "queue_position": queue.get("currentIndex"),
            "websocket_connected": self.coordinator.ws_connected,
        }

    # ==================== 传输控制 ====================
    async def _call(self, coro) -> None:
        """统一包一层:后端错误翻译成 HA 能展示的异常,并请求一次状态对齐。"""
        try:
            await coro
        except MusicFlowError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        await self._call(self.coordinator.client.async_play(self.peer_id))

    async def async_media_pause(self) -> None:
        await self._call(self.coordinator.client.async_pause(self.peer_id))

    async def async_media_stop(self) -> None:
        await self._call(self.coordinator.client.async_stop(self.peer_id))

    async def async_media_next_track(self) -> None:
        await self._call(self.coordinator.client.async_next(self.peer_id))

    async def async_media_previous_track(self) -> None:
        await self._call(self.coordinator.client.async_previous(self.peer_id))

    async def async_media_seek(self, position: float) -> None:
        await self._call(self.coordinator.client.async_seek(self.peer_id, position))

    async def async_set_volume_level(self, volume: float) -> None:
        await self._call(
            self.coordinator.client.async_set_volume(self.peer_id, round(volume * 100))
        )

    async def async_volume_up(self) -> None:
        current = self.volume_level
        await self.async_set_volume_level(min(1.0, (current if current is not None else 0.0) + 0.05))

    async def async_volume_down(self) -> None:
        current = self.volume_level
        await self.async_set_volume_level(max(0.0, (current if current is not None else 0.0) - 0.05))

    async def async_clear_playlist(self) -> None:
        await self._call(self.coordinator.client.async_clear_queue(self.peer_id))

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.async_apply_play_mode("shuffle" if shuffle else "order")

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        mode = {RepeatMode.ONE: "one", RepeatMode.ALL: "all"}.get(repeat, "order")
        await self.async_apply_play_mode(mode)

    async def async_apply_play_mode(self, play_mode: str) -> None:
        """设置队列播放模式(order / one / all / shuffle)。"""
        await self._call(self.coordinator.client.async_set_play_mode(self.peer_id, play_mode))

    # ==================== 内容播放 ====================
    async def async_play_media(
        self,
        media_type: str,
        media_id: str,
        enqueue: MediaPlayerEnqueue | None = None,
        announce: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """处理来自媒体浏览器 / play_media 服务的点播请求。"""
        content_type, content_id = self._resolve_content(media_type, media_id)
        await self.async_play_content(
            content_type=content_type,
            content_id=content_id,
            enqueue=enqueue in (MediaPlayerEnqueue.ADD, MediaPlayerEnqueue.NEXT),
        )

    def _resolve_content(self, media_type: str, media_id: str) -> tuple[str, str]:
        """把 HA 传来的 (type, id) 归一成后端 `/v1/play` 认识的 (type, id)。"""
        if media_id.startswith(MEDIA_URI_PREFIX):
            kind, value = parse_media_id(media_id)
            # 浏览器里"播放某个目录"时,kind 就是内容类型
            if kind in PLAYABLE_TYPES and value:
                return kind, value
            if kind == "track" and value:
                return "song", value
            raise HomeAssistantError(f"不支持播放 {media_id}")

        # 直接调用 media_player.play_media 时:media_content_type 决定类型
        normalized = {"track": "song", "music": "song"}.get(media_type, media_type)
        if normalized in PLAYABLE_TYPES:
            return normalized, media_id
        raise HomeAssistantError(
            f"不支持的媒体类型 {media_type},可用: {', '.join(PLAYABLE_TYPES)}"
        )

    async def async_play_content(
        self,
        content_type: str,
        content_id: str,
        start_index: int = 0,
        play_mode: str | None = None,
        enqueue: bool = False,
    ) -> None:
        """`musicflow.play_content` 服务实现,也是 play_media 的落点。"""
        await self._call(
            self.coordinator.client.async_play_content(
                self.peer_id,
                content_type,
                content_id,
                start_index=start_index,
                play_mode=play_mode,
                enqueue=enqueue,
            )
        )

    # ==================== 媒体浏览 ====================
    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        try:
            return await build_browse_media(self.coordinator.client, media_content_id)
        except MusicFlowError as err:
            raise HomeAssistantError(f"浏览曲库失败: {err}") from err
