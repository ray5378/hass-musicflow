"""MusicFlow 客户端:封装 REST 调用 + WebSocket 长连接。

集成侧所有与 MusicFlow 的交互都经过这里,业务层不直接碰 HTTP/WS。
认证:所有请求带 Authorization: Bearer <api_key>。
OpenSubsonic 端点(/rest/*)同样用 Bearer(后端需放行或加旁路认证)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp
import async_timeout
import websockets

_LOGGER = logging.getLogger(__name__)

WS_PING_INTERVAL = 30


class MusicFlowClient:
    """MusicFlow REST + WebSocket 客户端。"""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._ws_url = self._base_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        ) + "/ws"
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._listen_task: asyncio.Task | None = None

    # ==================== 通用 ====================
    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with async_timeout.timeout(15):
                async with self._session.get(
                    url, params=params, headers=self._headers()
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as err:
            _LOGGER.debug("GET %s 失败: %s", path, err)
            raise

    async def _post_json(self, path: str, payload: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with async_timeout.timeout(15):
                async with self._session.post(
                    url, json=payload or {}, headers=self._headers()
                ) as resp:
                    resp.raise_for_status()
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return None
        except Exception as err:
            _LOGGER.debug("POST %s 失败: %s", path, err)
            raise

    # ==================== DLNA 设备 ====================
    async def get_devices(self) -> list[dict]:
        """GET /api/v1/dlna/devices —— 返回 DLNA 设备列表。"""
        data = await self._get_json("/api/v1/dlna/devices")
        return data if isinstance(data, list) else data.get("devices", [])

    async def get_device_status(self, device_id: str) -> dict:
        """GET /api/v1/dlna/devices/:id/status —— 单设备状态。"""
        return await self._get_json(f"/api/v1/dlna/devices/{device_id}/status")

    async def cast_song(self, device_id: str, song_id: str) -> None:
        """POST /api/v1/dlna/cast —— 投射单曲(base_url 由后端自动推断)。"""
        await self._post_json(
            "/api/v1/dlna/cast",
            {"songId": song_id, "deviceId": device_id, "baseUrl": self._base_url},
        )

    async def play(self, device_id: str) -> None:
        await self._post_json(f"/api/v1/dlna/devices/{device_id}/play")

    async def pause(self, device_id: str) -> None:
        await self._post_json(f"/api/v1/dlna/devices/{device_id}/pause")

    async def stop(self, device_id: str) -> None:
        await self._post_json(f"/api/v1/dlna/devices/{device_id}/stop")

    async def seek(self, device_id: str, position: float) -> None:
        await self._post_json(
            f"/api/v1/dlna/devices/{device_id}/seek", {"position": position}
        )

    async def set_volume(self, device_id: str, volume: float) -> None:
        await self._post_json(
            f"/api/v1/dlna/devices/{device_id}/volume", {"volume": volume}
        )

    # ==================== 队列管理 ====================
    async def queue_play(
        self, device_id: str, items: list[dict], start_index: int = 0
    ) -> None:
        """POST /api/v1/dlna/devices/:id/queue/play —— 替换队列并播放。"""
        await self._post_json(
            f"/api/v1/dlna/devices/{device_id}/queue/play",
            {"items": items, "startIndex": start_index, "baseUrl": self._base_url},
        )

    async def queue_enqueue(self, device_id: str, items: list[dict]) -> None:
        await self._post_json(
            f"/api/v1/dlna/devices/{device_id}/queue/enqueue",
            {"items": items, "baseUrl": self._base_url},
        )

    async def queue_next(self, device_id: str) -> None:
        await self._post_json(f"/api/v1/dlna/devices/{device_id}/next")

    async def queue_prev(self, device_id: str) -> None:
        await self._post_json(f"/api/v1/dlna/devices/{device_id}/prev")

    async def queue_clear(self, device_id: str) -> None:
        async with self._session.delete(
            f"{self._base_url}/api/v1/dlna/devices/{device_id}/queue",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()

    async def get_queue(self, device_id: str) -> dict:
        return await self._get_json(f"/api/v1/dlna/devices/{device_id}/queue")

    # ==================== OpenSubsonic 浏览 ====================
    async def get_artists(self) -> dict:
        """GET /rest/getArtists.view —— 艺术家列表。"""
        data = await self._get_json("/rest/getArtists.view", {"f": "json"})
        return data.get("subsonic-response", {})

    async def get_artist(self, artist_id: str) -> dict:
        data = await self._get_json(
            "/rest/getArtist.view", {"id": artist_id, "f": "json"}
        )
        return data.get("subsonic-response", {})

    async def get_album_list(self, list_type: str = "newest", size: int = 200) -> dict:
        data = await self._get_json(
            "/rest/getAlbumList.view",
            {"type": list_type, "size": size, "f": "json"},
        )
        return data.get("subsonic-response", {})

    async def get_album(self, album_id: str) -> dict:
        data = await self._get_json(
            "/rest/getAlbum.view", {"id": album_id, "f": "json"}
        )
        return data.get("subsonic-response", {})

    async def get_playlists(self) -> dict:
        data = await self._get_json("/rest/getPlaylists.view", {"f": "json"})
        return data.get("subsonic-response", {})

    async def get_playlist(self, playlist_id: str) -> dict:
        data = await self._get_json(
            "/rest/getPlaylist.view", {"id": playlist_id, "f": "json"}
        )
        return data.get("subsonic-response", {})

    # ==================== WebSocket ====================
    async def connect_ws(self) -> None:
        """建立 WS 长连接并启动监听任务。"""
        self._ws = await websockets.connect(
            f"{self._ws_url}?token={self._api_key}",
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=10,
        )
        self._listen_task = asyncio.create_task(self._ws_listen())

    async def _ws_listen(self) -> None:
        """监听 WS 消息,分发给所有 listener。"""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for listener in list(self._listeners):
                    try:
                        listener(msg)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("WS listener 异常: %s", err)
        except websockets.ConnectionClosed:
            _LOGGER.info("MusicFlow WebSocket 已断开,将重连")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MusicFlow WebSocket 监听异常: %s", err)

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    async def disconnect(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
