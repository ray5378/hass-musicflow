"""DLNA 设备 → MediaPlayerEntity。

每个 MusicFlow 管理的 DLNA 设备变成一个 HA media_player 实体。
所有控制方法内部调用 MusicFlow REST,真正的推流发生在 MusicFlow 后端。
HA 在此只充当远程控制器。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .browse_media import build_browse_media
from .const import DOMAIN, MEDIA_URI_PREFIX
from .coordinator import MusicFlowCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """为每个已发现的 DLNA 设备创建实体。"""
    coordinator: MusicFlowCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        MusicFlowDevice(coordinator, dev_id)
        for dev_id, status in coordinator.devices.items()
        if status.get("available", True)
    ]
    async_add_entities(entities)

    # 监听后续设备增删
    @callback
    def _on_update() -> None:
        current_ids = set(coordinator.devices.keys())
        known = set(getattr(_on_update, "_known", set()))
        new_ids = current_ids - known
        if new_ids:
            setattr(_on_update, "_known", current_ids)
            async_add_entities(
                [MusicFlowDevice(coordinator, i) for i in new_ids]
            )

    setattr(_on_update, "_known", set(coordinator.devices.keys()))
    coordinator.async_add_listener(_on_update)


class MusicFlowDevice(MediaPlayerEntity):
    """单个 DLNA 设备对应的 HA 实体。"""

    _attr_has_entity_name = False

    def __init__(self, coordinator: MusicFlowCoordinator, device_id: str) -> None:
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}-{device_id}"

    @property
    def available(self) -> bool:
        status = self._coordinator.devices.get(self._device_id)
        return bool(status and status.get("available", True))

    @property
    def name(self) -> str | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("name") or self._device_id

    @property
    def state(self) -> MediaPlayerState:
        status = self._coordinator.devices.get(self._device_id, {})
        st = status.get("state", "STOPPED")
        return {
            "PLAYING": MediaPlayerState.PLAYING,
            "PAUSED_PLAYBACK": MediaPlayerState.PAUSED,
            "STOPPED": MediaPlayerState.IDLE,
            "TRANSITIONING": MediaPlayerState.BUFFERING,
            "NO_MEDIA_PRESENT": MediaPlayerState.IDLE,
        }.get(st, MediaPlayerState.IDLE)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        return (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.SEEK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.BROWSE_MEDIA
        )

    @property
    def volume_level(self) -> float | None:
        status = self._coordinator.devices.get(self._device_id, {})
        vol = status.get("volume", 0)
        return vol / 100.0 if vol else 0.0

    @property
    def media_duration(self) -> int | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("duration") or None

    @property
    def media_position(self) -> int | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("position") or None

    @property
    def media_title(self) -> str | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("media", {}).get("title")

    @property
    def media_artist(self) -> str | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("media", {}).get("artist")

    @property
    def media_album_name(self) -> str | None:
        status = self._coordinator.devices.get(self._device_id, {})
        return status.get("media", {}).get("album")

    @property
    def media_image_url(self) -> str | None:
        status = self._coordinator.devices.get(self._device_id, {})
        cover = status.get("media", {}).get("coverArt")
        if not cover:
            return None
        return f"{self._coordinator.client.base_url}/rest/getCoverArt?id={cover}&size=500"

    # ==================== 控制方法(转发给 MusicFlow)====================
    async def async_media_play(self) -> None:
        await self._coordinator.client.play(self._device_id)

    async def async_media_pause(self) -> None:
        await self._coordinator.client.pause(self._device_id)

    async def async_media_stop(self) -> None:
        await self._coordinator.client.stop(self._device_id)

    async def async_media_seek(self, position: float) -> None:
        await self._coordinator.client.seek(self._device_id, position)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._coordinator.client.set_volume(self._device_id, volume * 100)

    async def async_media_next_track(self) -> None:
        await self._coordinator.client.queue_next(self._device_id)

    async def async_media_previous_track(self) -> None:
        await self._coordinator.client.queue_prev(self._device_id)

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """处理 browse_media 点播 + automation 的 play_media。

        media_id 支持的自定义 URI:
          musicflow://song/<id>        → 单曲投射
          musicflow://album/<id>       → 整张专辑入队播放
          musicflow://playlist/<id>    → 歌单入队播放
        """
        if not media_id.startswith(MEDIA_URI_PREFIX):
            _LOGGER.warning("不支持的 media_id: %s", media_id)
            return

        path = media_id.replace(MEDIA_URI_PREFIX, "")
        parts = path.split("/")
        kind = parts[0] if parts else ""

        try:
            if kind == "song" and len(parts) > 1:
                await self._coordinator.client.cast_song(self._device_id, parts[1])
            elif kind == "album" and len(parts) > 1:
                resp = await self._coordinator.client.get_album(parts[1])
                album = resp.get("album", {})
                items = self._songs_to_queue_items(album.get("song", []))
                await self._coordinator.client.queue_play(self._device_id, items, 0)
            elif kind == "playlist" and len(parts) > 1:
                resp = await self._coordinator.client.get_playlist(parts[1])
                playlist = resp.get("playlist", {})
                items = self._songs_to_queue_items(playlist.get("entry", []))
                await self._coordinator.client.queue_play(self._device_id, items, 0)
            else:
                _LOGGER.warning("无法解析的 media_id: %s", media_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("播放媒体失败: %s", err)

    async def async_browse_media(
        self, media_content_type: str | None, media_content_id: str | None
    ) -> BrowseMedia:
        """HA 媒体浏览器入口。"""
        return await build_browse_media(
            self._coordinator.client,
            media_content_type,
            media_content_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @staticmethod
    def _songs_to_queue_items(songs: list[dict]) -> list[dict]:
        """OpenSubsonic song 对象 → MusicFlow QueueItem。"""
        mime_map = {
            "mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav",
            "aac": "audio/aac", "ogg": "audio/ogg", "m4a": "audio/mp4",
            "opus": "audio/opus", "wma": "audio/x-ms-wma", "ape": "audio/ape",
        }
        items = []
        for s in songs:
            suffix = (s.get("suffix") or s.get("contentType") or "mp3").lower()
            items.append(
                {
                    "songId": s["id"],
                    "title": s.get("title", "未知"),
                    "artist": s.get("artist"),
                    "album": s.get("album"),
                    "mime": mime_map.get(suffix, "audio/mpeg"),
                    "coverArt": s.get("coverArt"),
                }
            )
        return items
