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

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    BrowseMedia,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MusicFlowError
from .browse_media import build_browse_media, build_search_results, parse_media_id
from .const import (
    ATTR_CONTENT_ID,
    ATTR_CONTENT_TYPE,
    ATTR_ENQUEUE,
    ATTR_PLAY_MODE,
    ATTR_START_INDEX,
    AUTO_GROUP_SUFFIX,
    DOMAIN,
    MEDIA_URI_PREFIX,
    PEER_KIND_DLNA,
    PEER_KIND_GROUP,
    PLAY_MODES,
    PLAYABLE_TYPES,
    SERVICE_CLEAR_QUEUE,
    SERVICE_PLAY_CONTENT,
    SERVICE_SET_PLAY_MODE,
    TRANSFER_SEEK_DELAY,
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
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
)

# SEARCH_MEDIA 是 HA 2025.5 才加进 MediaPlayerEntityFeature 的成员,而 hacs.json
# 声明的最低核心版本是 2024.12 —— 在老核心上直接引用会 AttributeError,所以按
# 存在性叠加。没有这个 flag 时搜索框不出现,但 async_search_media 仍然无害。
_SEARCH_MEDIA_FEATURE = getattr(MediaPlayerEntityFeature, "SEARCH_MEDIA", None)
if _SEARCH_MEDIA_FEATURE is not None:
    SUPPORTED_FEATURES |= _SEARCH_MEDIA_FEATURE


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
    # 封面由 HA 服务端代拉后再喂给前端(见 async_get_media_image)。
    # 直链方案只在浏览器和 MusicFlow 同网段时才成立,从公网/Nabu Casa 访问 HA 时
    # 客户端够不着内网地址,封面就会空 —— 走代理可以两种场景通吃。
    _attr_media_image_remotely_accessible = False

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
        # DLNA 渲染器没有真正的电源开关,turn_off 用"软关机"表达:
        # 停止播放 + 状态显示 OFF,设备再次出声时自动解除(见 _handle_coordinator_update)
        self._soft_off = False

    # ==================== 基础信息 ====================
    @property
    def _peer(self) -> PeerState | None:
        return self.coordinator.get_peer(self.peer_id)

    @property
    def _status(self) -> dict[str, Any]:
        peer = self._peer
        return peer.status if peer else {}

    @property
    def _group_id(self) -> str | None:
        """本实体在 HA 分组里归属的 MusicFlow 组 id(没入组则为 None)。"""
        peer = self._peer
        if peer is None:
            return None
        if peer.kind == PEER_KIND_GROUP:
            return peer.raw_id
        return self.coordinator.primary_group_of_device(peer.raw_id)

    @property
    def _control_peer_id(self) -> str:
        """传输控制实际下发到哪个 peer。

        设备一旦入组,音频就由组队列驱动(组并发向每个成员 cast 同一首歌)。此时
        对着单台成员发 pause/next 只会让它和组播脱节,所以统一转发给组 —— 与
        Sonos / HA 的分组语义一致:控制任一成员即控制整组。想单独控制请先退组。
        """
        peer = self._peer
        if peer is None or peer.kind == PEER_KIND_GROUP:
            return self.peer_id
        group_id = self.coordinator.primary_group_of_device(peer.raw_id)
        return f"{PEER_KIND_GROUP}:{group_id}" if group_id else self.peer_id

    @property
    def _control_peer(self) -> PeerState | None:
        return self.coordinator.get_peer(self._control_peer_id)

    @property
    def _queue(self) -> dict[str, Any]:
        peer = self._peer
        if peer is None:
            return {}
        if peer.queue.get("items"):
            return peer.queue
        # 入组后队列挂在组 peer 上,成员设备自己的队列是空的
        control = self._control_peer
        if control is not None and control is not peer:
            return control.queue
        return peer.queue

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
    @callback
    def _handle_coordinator_update(self) -> None:
        # 设备又出声了(比如有人从 MusicFlow Web 端点了播),软关机自动失效,
        # 否则 HA 会一直显示 OFF 而喇叭在响。
        if self._soft_off:
            raw = str(self._status.get("state") or "").upper()
            if raw in ("PLAYING", "TRANSITIONING"):
                self._soft_off = False
        super()._handle_coordinator_update()

    @property
    def state(self) -> MediaPlayerState:
        peer = self._peer
        if peer is None or not peer.available:
            return MediaPlayerState.OFF
        if self._soft_off:
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
    def is_volume_muted(self) -> bool | None:
        """静音状态。后端把它当成独立于音量的 RenderingControl 状态量。"""
        muted = self._status.get("muted")
        return bool(muted) if isinstance(muted, bool) else None

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
        if peer is None:
            return None
        item = peer.current_item
        if item:
            return item
        # 入组的成员设备自己没有队列,曲目信息取组队列的当前项
        control = self._control_peer
        if control is not None and control is not peer:
            return control.current_item
        return None

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
    def media_album_artist(self) -> str | None:
        """专辑艺术家。合辑里它和曲目艺术家不同,缺省时退化成曲目艺术家。"""
        item = self._current_item
        if not item:
            return None
        return item.get("albumArtist") or item.get("artist")

    @property
    def media_track(self) -> int | None:
        item = self._current_item
        track = item.get("track") if item else None
        if isinstance(track, (int, float)) and track > 0:
            return int(track)
        return None

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

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """由 HA 服务端代拉封面。

        `media_image_remotely_accessible = False` 时 HA 会调这里拿字节流,再通过
        `/api/media_player_proxy/<entity_id>` 发给前端 —— 公网访问 HA 也能看到封面。
        默认实现用的是 HA 自己的会话,这里换成带 Bearer 的客户端,顺带兼容以后
        封面端点收紧鉴权的情况。
        """
        url = self.media_image_url
        if not url:
            return None, None
        return await self.coordinator.client.async_fetch_image(url)

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
            # 入组后传输控制会转发到组,把实际落点暴露出来便于排查
            "control_peer_id": self._control_peer_id,
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
        self._soft_off = False
        await self._call(self.coordinator.client.async_play(self._control_peer_id))

    async def async_media_pause(self) -> None:
        await self._call(self.coordinator.client.async_pause(self._control_peer_id))

    async def async_media_stop(self) -> None:
        await self._call(self.coordinator.client.async_stop(self._control_peer_id))

    async def async_media_next_track(self) -> None:
        await self._call(self.coordinator.client.async_next(self._control_peer_id))

    async def async_media_previous_track(self) -> None:
        await self._call(self.coordinator.client.async_previous(self._control_peer_id))

    async def async_media_seek(self, position: float) -> None:
        await self._call(self.coordinator.client.async_seek(self._control_peer_id, position))

    async def async_set_volume_level(self, volume: float) -> None:
        # 音量按实体本身走:组实体调全组,成员实体单独调自己那只喇叭
        await self._call(
            self.coordinator.client.async_set_volume(self.peer_id, round(volume * 100))
        )

    async def async_volume_up(self) -> None:
        current = self.volume_level
        await self.async_set_volume_level(min(1.0, (current if current is not None else 0.0) + 0.05))

    async def async_volume_down(self) -> None:
        current = self.volume_level
        await self.async_set_volume_level(max(0.0, (current if current is not None else 0.0) - 0.05))

    async def async_mute_volume(self, mute: bool) -> None:
        """静音 / 取消静音。

        走 RenderingControl 的 SetMute,不动 Volume —— 取消静音后设备自己恢复原
        音量,所以这里既不需要记旧音量,也不会和用户在设备上的物理调节打架。
        """
        await self._call(self.coordinator.client.async_set_mute(self.peer_id, mute))

    async def async_turn_off(self) -> None:
        """关闭:停止播放并清空播放列表。

        等价于服务器上"清空该播放器的队列"——停掉当前曲目后,把队列整个
        清空(用户点关闭就是想让这台设备彻底安静、队列不再保留)。先 stop
        再 clear:stop 会先冻结队列自动推进(tracker),避免 STOPPED 事件
        被误判成"这首放完了"而去播下一首。DLNA 渲染器没有标准电源开关,
        因此没有真实断电,再次播放时队列是干净的。
        """
        self._soft_off = True
        self.async_write_ha_state()
        await self._call(self.coordinator.client.async_stop(self._control_peer_id))
        await self._call(self.coordinator.client.async_clear_queue(self._control_peer_id))

    async def async_turn_on(self) -> None:
        self._soft_off = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_clear_playlist(self) -> None:
        await self._call(self.coordinator.client.async_clear_queue(self._control_peer_id))

    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self.async_apply_play_mode("shuffle" if shuffle else "order")

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        mode = {RepeatMode.ONE: "one", RepeatMode.ALL: "all"}.get(repeat, "order")
        await self.async_apply_play_mode(mode)

    async def async_apply_play_mode(self, play_mode: str) -> None:
        """设置队列播放模式(order / one / all / shuffle)。"""
        await self._call(
            self.coordinator.client.async_set_play_mode(self._control_peer_id, play_mode)
        )

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
        if announce:
            # TTS 播报。走到这里时 HA 已经把 media-source://tts/... 解析成了一个
            # 它自己托管的绝对 URL,直接丢给后端 announce 编排:保存现场 → 播报 →
            # 播完自动回到原曲原进度。
            extra = kwargs.get("extra") or {}
            volume = extra.get("volume") if isinstance(extra, dict) else None
            if volume is None:
                volume = kwargs.get("volume")
            await self._call(
                self.coordinator.client.async_announce(
                    self._control_peer_id,
                    media_id,
                    volume=int(volume) if isinstance(volume, (int, float)) else None,
                )
            )
            return

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
        self._soft_off = False
        await self._call(
            self.coordinator.client.async_play_content(
                self._control_peer_id,
                content_type,
                content_id,
                start_index=start_index,
                play_mode=play_mode,
                enqueue=enqueue,
            )
        )

    # ==================== 播放转移(SELECT_SOURCE)====================
    @property
    def source(self) -> str | None:
        """当前音频落点。转移播放后这里会指向新的播放器。"""
        peer = self._peer
        return peer.name if peer else None

    @property
    def source_list(self) -> list[str]:
        """可作为转移目标的播放器。选中另一个即把当前队列和进度整体搬过去。"""
        names: list[str] = []
        for peer in self.coordinator.controllable_peers():
            if peer.available and peer.name not in names:
                names.append(peer.name)
        return names

    async def async_select_source(self, source: str) -> None:
        peer = self._peer
        if peer is not None and source == peer.name:
            return
        target = next(
            (
                p
                for p in self.coordinator.controllable_peers()
                if p.available and p.name == source
            ),
            None,
        )
        if target is None:
            raise HomeAssistantError(f"找不到可用的播放器「{source}」")
        snapshot = self._playback_snapshot()
        if snapshot is None:
            raise HomeAssistantError("当前没有可转移的播放队列")
        await self._transfer_playback(
            target.peer_id, snapshot, source_peer_id=self._control_peer_id, stop_source=True
        )
        await self.coordinator.async_request_refresh()

    def _playback_snapshot(self) -> dict[str, Any] | None:
        """把"现在在放什么、放到哪了"打个包,用于转移到别的播放器。"""
        queue = self._queue
        items = queue.get("items") or []
        if not items:
            return None
        index = queue.get("currentIndex")
        return {
            "items": items,
            "index": index if isinstance(index, int) and index >= 0 else 0,
            "position": self.media_position or 0,
            "playing": self.state == MediaPlayerState.PLAYING,
        }

    async def _transfer_playback(
        self,
        target_peer_id: str,
        snapshot: dict[str, Any],
        *,
        source_peer_id: str | None = None,
        stop_source: bool = False,
    ) -> None:
        """把一份播放快照灌到目标 peer 并对齐进度。

        QueueItem 是后端自产自销的结构,原样搬过去即可 —— 集成侧不需要知道这批
        歌当初是从哪个歌单/专辑点出来的。
        """
        client = self.coordinator.client
        try:
            if source_peer_id and source_peer_id != target_peer_id:
                # 先把源队列摘掉:否则源 peer 的 PlaybackTracker 还盯着这台设备,
                # 目标端一 cast 新 URI 就会被它当成"切歌"而抢控制权。
                await client.async_clear_queue(source_peer_id)
                if stop_source:
                    await client.async_stop(source_peer_id)
            await client.async_queue_play(
                target_peer_id, snapshot["items"], snapshot["index"]
            )
            position = snapshot.get("position") or 0
            if position > 1:
                # 设备要先起播才吃得下 seek(同后端 announce 恢复现场的处理)
                await asyncio.sleep(TRANSFER_SEEK_DELAY)
                await client.async_seek(target_peer_id, position)
            if not snapshot.get("playing"):
                await client.async_pause(target_peer_id)
        except MusicFlowError as err:
            raise HomeAssistantError(f"转移播放失败: {err}") from err

    # ==================== 分组(GROUPING)====================
    def _entity_id_for_peer(self, peer_id: str) -> str | None:
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            MEDIA_PLAYER_DOMAIN, DOMAIN, f"{self._entry.entry_id}_{peer_id}"
        )

    def _peer_id_for_entity(self, entity_id: str) -> str | None:
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN:
            return None
        if entry.config_entry_id != self._entry.entry_id:
            return None
        prefix = f"{self._entry.entry_id}_"
        if not entry.unique_id.startswith(prefix):
            return None
        return entry.unique_id[len(prefix) :]

    def _device_ids_from_entities(self, entity_ids: list[str]) -> list[str]:
        """把 HA 实体列表翻译成 MusicFlow 的裸 deviceId(组会被展开成成员)。"""
        devices: list[str] = []
        for entity_id in entity_ids:
            peer_id = self._peer_id_for_entity(entity_id)
            if peer_id is None:
                raise HomeAssistantError(
                    f"{entity_id} 不属于这台 MusicFlow 服务器,无法加入同一个组"
                )
            kind, _, raw = peer_id.partition(":")
            if kind == PEER_KIND_DLNA:
                candidates = [raw]
            elif kind == PEER_KIND_GROUP:
                candidates = self.coordinator.group_member_ids(raw)
            else:
                raise HomeAssistantError(f"{entity_id} 不能作为分组成员")
            devices.extend(d for d in candidates if d not in devices)
        return devices

    @property
    def group_members(self) -> list[str] | None:
        """同一个 MusicFlow 组里的所有播放器实体。

        HA 约定首位是 leader。MusicFlow 的组本身就是一个播放器实体(它才是真正
        持队列、驱动全组的那个),所以把组实体排在最前,后面跟成员设备。
        """
        if self._peer is None:
            return None
        group_id = self._group_id
        if not group_id:
            return []
        members = [
            entity_id
            for device_id in self.coordinator.group_member_ids(group_id)
            if (entity_id := self._entity_id_for_peer(f"{PEER_KIND_DLNA}:{device_id}"))
        ]
        if not members:
            return []
        group_entity = self._entity_id_for_peer(f"{PEER_KIND_GROUP}:{group_id}")
        if group_entity:
            return [group_entity, *(e for e in members if e != group_entity)]
        return members

    async def async_join_players(self, group_members: list[str]) -> None:
        """把其他播放器并进本播放器所在的组(没有组就现建一个)。"""
        peer = self._peer
        if peer is None:
            raise HomeAssistantError("播放器不可用")
        targets = self._device_ids_from_entities(group_members)
        client = self.coordinator.client
        group_id = self._group_id

        if group_id:
            merged = self.coordinator.group_member_ids(group_id)
            if peer.kind == PEER_KIND_DLNA and peer.raw_id not in merged:
                merged.append(peer.raw_id)
            merged.extend(d for d in targets if d not in merged)
            # 后端 PUT /v1/groups/:id 会给新加入的成员 cast 当前曲并对齐进度
            await self._call(client.async_set_group_members(group_id, merged))
            return

        members = [peer.raw_id] + [d for d in targets if d != peer.raw_id]
        if len(members) < 2:
            raise HomeAssistantError("分组至少需要两台设备")
        # 建组会新增一个 `group:<id>` 播放器实体,原来在本机放的内容要跟着搬过去,
        # 否则建完组会出现"组是空的、歌还在单机上放"的割裂状态。
        snapshot = self._playback_snapshot()
        try:
            group = await client.async_create_group(
                f"{peer.name}{AUTO_GROUP_SUFFIX}", members
            )
        except MusicFlowError as err:
            raise HomeAssistantError(f"创建播放器组失败: {err}") from err
        group_id = group.get("id")
        if group_id and snapshot:
            await self._transfer_playback(
                f"{PEER_KIND_GROUP}:{group_id}",
                snapshot,
                source_peer_id=self.peer_id,
                # 源设备本身也是新组的成员,停它会把刚 cast 过去的组播一起掐掉
                stop_source=False,
            )
        await self.coordinator.async_request_refresh()

    async def async_unjoin_player(self) -> None:
        """退组。对组实体本身调用则解散整个组。"""
        peer = self._peer
        if peer is None:
            raise HomeAssistantError("播放器不可用")
        client = self.coordinator.client

        if peer.kind == PEER_KIND_GROUP:
            await self._call(client.async_delete_group(peer.raw_id))
            return

        group_id = self.coordinator.primary_group_of_device(peer.raw_id)
        if not group_id:
            return
        remaining = [
            d for d in self.coordinator.group_member_ids(group_id) if d != peer.raw_id
        ]
        try:
            if remaining:
                await client.async_set_group_members(group_id, remaining)
            else:
                await client.async_delete_group(group_id)
            # 离队的设备还在放着组里最后 cast 过去的那首,得让它停下来
            await client.async_stop(self.peer_id)
        except MusicFlowError as err:
            raise HomeAssistantError(f"退出播放器组失败: {err}") from err
        await self.coordinator.async_request_refresh()

    # ==================== 媒体浏览 ====================
    def _thumbnail(
        self, content_type: str, content_id: str, cover_art: str | None
    ) -> str | None:
        """浏览列表里的封面统一走 HA 的 media_player_proxy。

        直链只在浏览器和 MusicFlow 同网段时才拉得到;代理之后由 HA 服务端回源,
        公网访问也能出图(HA 还会顺带缓存)。
        """
        if not cover_art:
            return None
        return self.get_browse_image_url(
            content_type, content_id, media_image_id=str(cover_art)
        )

    async def async_get_browse_image(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """media_player_proxy 的回源实现:按 coverArt id 去 MusicFlow 取图。"""
        url = self.coordinator.client.cover_url(media_image_id)
        if not url:
            return None, None
        return await self.coordinator.client.async_fetch_image(url)

    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        try:
            return await build_browse_media(
                self.coordinator.client, media_content_id, thumb=self._thumbnail
            )
        except MusicFlowError as err:
            raise HomeAssistantError(f"浏览曲库失败: {err}") from err

    async def async_search_media(
        self, query: SearchMediaQuery, *args, **kwargs
    ) -> SearchMedia:
        """HA 媒体浏览器搜索栏的落点:歌单/专辑/艺术家/歌曲统一搜索。

        2025.5+ 协议:HA 以 `query=SearchMediaQuery(...)` 关键字调用,要求返回
        `SearchMedia(result=[...BrowseMedia])`。旧的 str 形式仅作防御保留。
        """
        search_query = query.search_query if not isinstance(query, str) else query
        try:
            return await build_search_results(
                self.coordinator.client, search_query, 50, thumb=self._thumbnail
            )
        except MusicFlowError as err:
            raise HomeAssistantError(f"搜索失败: {err}") from err
