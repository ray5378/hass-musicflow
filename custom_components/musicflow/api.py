"""MusicFlow 客户端:封装 REST 调用 + WebSocket 长连接。

集成侧所有与 MusicFlow 的交互都经过这里,业务层不直接碰 HTTP/WS。

认证:所有请求带 `Authorization: Bearer <api_key>`。后端 middleware/auth.ts 的
Bearer 分支先按 JWT 校验,失败再回退到长期 API Key,所以直接传 API Key 即可,
`/rest/*`(OpenSubsonic)与 `/rest/api/*`(内部 REST)通吃。

WebSocket 用 HA 自带的 aiohttp,不引入 `websockets` 三方依赖。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from .const import (
    API_PREFIX,
    REQUEST_TIMEOUT,
    SUBSONIC_PREFIX,
    WS_HEARTBEAT,
    WS_PATH,
)

_LOGGER = logging.getLogger(__name__)


class MusicFlowError(Exception):
    """MusicFlow 请求失败。"""


class MusicFlowAuthError(MusicFlowError):
    """API Key 无效或已过期。"""


class MusicFlowClient:
    """MusicFlow REST + WebSocket 客户端。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str,
    ) -> None:
        self._session = session
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._listeners: list[Callable[[dict], None]] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    # ==================== 通用 ====================
    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self._base_url.startswith("https") else "ws"
        host = self._base_url.split("://", 1)[-1]
        return f"{scheme}://{host}{WS_PATH}?token={quote(self._api_key, safe='')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _encode(segment: str) -> str:
        """peerId 形如 `dlna:<udn>`,极端情况下 udn 会回退成设备 location URL
        (含 `/`),必须整体转义成单个路径段。后端 decodePeerId 会 decodeURIComponent。
        """
        return quote(segment, safe="")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        """发一次请求。path 必须是已编码好的完整路径(不含 host)。"""
        url = URL(f"{self._base_url}{path}", encoded=True)
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status in (401, 403):
                    raise MusicFlowAuthError(f"{method} {path} 认证失败({resp.status})")
                if resp.status >= 400:
                    text = await resp.text()
                    raise MusicFlowError(f"{method} {path} 失败 {resp.status}: {text[:200]}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return None
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise MusicFlowError(f"{method} {path} 超时") from err
        except aiohttp.ClientError as err:
            raise MusicFlowError(f"{method} {path} 网络错误: {err}") from err

    async def _api_get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", f"{API_PREFIX}{path}", params=params)

    async def _api_post(self, path: str, body: dict | None = None) -> Any:
        return await self._request("POST", f"{API_PREFIX}{path}", json_body=body or {})

    async def _api_delete(self, path: str) -> Any:
        return await self._request("DELETE", f"{API_PREFIX}{path}")

    async def _subsonic(self, view: str, params: dict | None = None) -> dict:
        """调用 OpenSubsonic 端点,返回已剥壳的 subsonic-response。"""
        query = {"f": "json", **(params or {})}
        data = await self._request("GET", f"{SUBSONIC_PREFIX}/{view}", params=query)
        if not isinstance(data, dict):
            return {}
        resp = data.get("subsonic-response", {})
        if resp.get("status") == "failed":
            err = resp.get("error", {})
            if err.get("code") in (40, 41):
                raise MusicFlowAuthError(err.get("message", "认证失败"))
            raise MusicFlowError(f"{view}: {err.get('message', '未知错误')}")
        return resp

    # ==================== 连通性 / 账号 ====================
    async def async_verify(self) -> dict:
        """校验地址 + API Key,返回当前用户信息。"""
        data = await self._api_get("/users/me")
        return data if isinstance(data, dict) else {}

    # ==================== Peer(统一播放目标)====================
    async def async_get_peers(self) -> list[dict]:
        """GET /v1/peers —— local / dlna / group 全量列表(含队列快照)。"""
        data = await self._api_get("/peers")
        peers = data.get("peers") if isinstance(data, dict) else None
        return peers if isinstance(peers, list) else []

    async def async_get_peer_status(self, peer_id: str) -> dict:
        """GET /v1/peers/:peerId/status —— dlna 为设备状态,group 为 leader 状态。"""
        data = await self._api_get(f"/peers/{self._encode(peer_id)}/status")
        return data if isinstance(data, dict) else {}

    async def async_get_peer_queue(self, peer_id: str, *, offset: int = 0, size: int = 0) -> dict:
        """GET /v1/peers/:peerId/queue —— offset/size 分页(缺省全量,兼容旧端点)。"""
        qs = f"?offset={offset}&size={size}" if size > 0 else ""
        data = await self._api_get(f"/peers/{self._encode(peer_id)}/queue{qs}")
        return data if isinstance(data, dict) else {}

    # ---- 传输控制 ----
    async def async_play(self, peer_id: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/play")

    async def async_pause(self, peer_id: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/pause")

    async def async_stop(self, peer_id: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/stop")

    async def async_next(self, peer_id: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/next")

    async def async_previous(self, peer_id: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/prev")

    async def async_seek(self, peer_id: str, seconds: float) -> None:
        await self._api_post(
            f"/peers/{self._encode(peer_id)}/seek", {"seconds": float(seconds)}
        )

    async def async_set_volume(self, peer_id: str, volume: int) -> None:
        """volume 为 0-100 的整数(后端 setDeviceVolume 的量纲)。"""
        await self._api_post(
            f"/peers/{self._encode(peer_id)}/volume",
            {"volume": max(0, min(100, int(round(volume))))},
        )

    async def async_set_mute(self, peer_id: str, muted: bool) -> None:
        """静音开关。

        后端把它当成独立于音量的 RenderingControl 状态量(SetMute),不会去动
        Volume —— 取消静音后设备自己恢复原音量,集成侧不需要缓存旧音量。
        """
        await self._api_post(
            f"/peers/{self._encode(peer_id)}/mute", {"muted": bool(muted)}
        )

    async def async_set_play_mode(self, peer_id: str, mode: str) -> None:
        await self._api_post(f"/peers/{self._encode(peer_id)}/play-mode", {"mode": mode})

    async def async_clear_queue(self, peer_id: str) -> None:
        await self._api_delete(f"/peers/{self._encode(peer_id)}/queue")

    async def async_queue_play(
        self, peer_id: str, items: list[dict], start_index: int = 0
    ) -> None:
        """POST /v1/peers/:peerId/queue/play —— 直接把一组 QueueItem 灌给播放器。

        用于"播放转移":QueueItem 是后端自产自销的结构,原样搬到另一个 peer 即可,
        集成侧不需要知道这批歌当初是从哪个歌单/专辑点出来的。
        """
        await self._api_post(
            f"/peers/{self._encode(peer_id)}/queue/play",
            {"items": items, "startIndex": max(0, int(start_index))},
        )

    async def async_announce(
        self, peer_id: str, url: str, *, volume: int | None = None
    ) -> None:
        """POST /v1/peers/:peerId/announce —— 打断当前播放放一段外链音频。

        后端默认非阻塞(202):保存现场 → 播报 → 播完自动回到原曲原进度。
        HA 的 play_media(announce=True) 不该被一段 30 秒语音卡住,所以这里不传
        blocking。
        """
        body: dict[str, Any] = {"url": url}
        if volume is not None:
            body["volume"] = max(0, min(100, int(round(volume))))
        await self._api_post(f"/peers/{self._encode(peer_id)}/announce", body)

    # ==================== 播放器群组 ====================
    async def async_get_groups(self) -> list[dict]:
        """GET /v1/groups —— 组列表(含 memberIds)。

        组本身已经以 `group:<id>` 的形式出现在 peers 里,这里既用于拿到成员设备
        (把 leader 的实时状态事件映射回组),也用于 HA 分组 UI 的成员展示。
        """
        data = await self._api_get("/groups")
        groups = data.get("groups") if isinstance(data, dict) else None
        return groups if isinstance(groups, list) else []

    # ---- 内容播放(统一入口)----
    async def async_play_content(
        self,
        peer_id: str,
        content_type: str,
        content_id: str,
        *,
        start_index: int = 0,
        play_mode: str | None = None,
        enqueue: bool = False,
    ) -> dict:
        """POST /v1/play —— 后端负责把 song/album/playlist/artist/genre 解析成队列。

        队列构造、mime 推断、封面回退全在后端 services/content.ts 里,集成侧
        不重复实现,避免和主仓库漂移。
        """
        body: dict[str, Any] = {
            "peerId": peer_id,
            "type": content_type,
            "id": content_id,
            "startIndex": start_index,
            "enqueue": enqueue,
        }
        if play_mode:
            body["playMode"] = play_mode
        data = await self._request("POST", f"{API_PREFIX}/play", json_body=body)
        return data if isinstance(data, dict) else {}

    # ==================== OpenSubsonic 浏览 ====================
    async def async_get_artists(self) -> dict:
        return await self._subsonic("getArtists")

    async def async_get_artist(self, artist_id: str) -> dict:
        return await self._subsonic("getArtist", {"id": artist_id})

    async def async_get_album_list(
        self, list_type: str = "newest", size: int = 300
    ) -> dict:
        return await self._subsonic("getAlbumList", {"type": list_type, "size": size})

    async def async_get_album(self, album_id: str) -> dict:
        return await self._subsonic("getAlbum", {"id": album_id})

    async def async_get_song(self, song_id: str) -> dict:
        """单曲详情。media_source 解析时用它拿 contentType/suffix 定 mime。"""
        resp = await self._subsonic("getSong", {"id": song_id})
        song = resp.get("song")
        return song if isinstance(song, dict) else {}

    async def async_get_playlists(self) -> dict:
        return await self._subsonic("getPlaylists")

    async def async_get_playlist(self, playlist_id: str) -> dict:
        return await self._subsonic("getPlaylist", {"id": playlist_id})

    async def async_get_genres(self) -> dict:
        return await self._subsonic("getGenres")

    async def async_get_songs_by_genre(self, genre: str, count: int = 300) -> dict:
        return await self._subsonic("getSongsByGenre", {"genre": genre, "count": count})

    async def async_search(self, query: str, count: int = 60) -> dict:
        return await self._subsonic(
            "search3",
            {
                "query": query,
                "artistCount": count,
                "albumCount": count,
                "songCount": count,
            },
        )

    # ==================== 收藏 / 歌词 / 歌单编辑(播放控件用)====================
    async def async_star(self, song_ids: list[str]) -> None:
        """喜欢:把歌曲加进当前用户的「我喜欢的音乐」。"""
        await self._subsonic("star", {"id": ",".join(song_ids)})

    async def async_unstar(self, song_ids: list[str]) -> None:
        """取消喜欢。"""
        await self._subsonic("unstar", {"id": ",".join(song_ids)})

    async def async_get_starred(self) -> set[str]:
        """当前用户已收藏的 songId 集合。"""
        resp = await self._subsonic("getStarred")
        songs = resp.get("starred", {}).get("song") or []
        return {str(s.get("id")) for s in songs if s.get("id")}

    async def async_get_lyrics(self, song_id: str) -> list[dict]:
        """结构化歌词行: [{start: 毫秒, value: str}]。无歌词返回空列表。"""
        resp = await self._subsonic("getLyricsBySongId", {"id": song_id})
        structured = resp.get("lyricsList", {}).get("structuredLyrics") or []
        lines: list[dict[str, Any]] = []
        for block in structured:
            for line in block.get("line") or []:
                if isinstance(line, dict) and line.get("value"):
                    lines.append(
                        {"start": int(line.get("start") or 0), "value": str(line["value"])}
                    )
        return lines

    async def async_add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        """把歌曲追加到指定歌单(后端校验归属,非本人/非管理员歌单只读)。"""
        await self._subsonic(
            "updatePlaylist",
            {"playlistId": playlist_id, "songIdToAdd": ",".join(song_ids)},
        )

    def cover_url(self, cover_art: str | None, size: int = 400) -> str | None:
        """封面直链。后端 index.ts 对 /rest/getCoverArt 显式放行鉴权,
        所以这个 URL 可以直接交给 HA 前端加载,不需要带 token。
        """
        if not cover_art:
            return None
        return (
            f"{self._base_url}{SUBSONIC_PREFIX}/getCoverArt"
            f"?id={quote(str(cover_art), safe='')}&size={size}"
        )

    def stream_url(self, song_id: str) -> str:
        """歌曲直链。给 media_source 用:HA 会把这个 URL 交给播放器/浏览器,
        没法带 Authorization 头,所以凭据走 `?token=` —— 后端 auth 中间件对该参数
        先按 JWT 验、再回退 API Key(与 WebSocket 的 ?token= 同一套契约)。
        """
        return (
            f"{self._base_url}{SUBSONIC_PREFIX}/stream"
            f"?id={quote(str(song_id), safe='')}"
            f"&token={quote(self._api_key, safe='')}"
        )

    async def async_fetch_image(self, url: str) -> tuple[bytes | None, str | None]:
        """拉一张图并返回 (内容, content-type)。

        用于 `async_get_browse_image`:HA 前端在外网访问时够不着内网的 MusicFlow,
        由 HA 服务端代拉再喂给前端。
        """
        try:
            async with self._session.get(
                URL(url, encoded=True),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    return None, None
                return await resp.read(), resp.content_type
        except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("拉取封面 %s 失败: %s", url, err)
            return None, None

    # ==================== WebSocket ====================
    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    async def async_ws_connect(self) -> aiohttp.ClientWebSocketResponse:
        # heartbeat 让 aiohttp 自动发 ping,后端 ws 库会回 pong;
        # 不传 timeout,避免不同 aiohttp 版本对该参数的语义差异。
        self._ws = await self._session.ws_connect(
            URL(self.ws_url, encoded=True),
            heartbeat=WS_HEARTBEAT,
        )
        return self._ws

    async def async_ws_listen(self) -> None:
        """阻塞读取 WS 消息并分发。连接断开时正常返回,由调用方决定是否重连。"""
        ws = self._ws
        if ws is None:
            return
        async for msg in ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                try:
                    payload = msg.json()
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    self._dispatch(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    def _dispatch(self, payload: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(payload)
            except Exception:  # noqa: BLE001 - 单个 listener 异常不应中断分发
                _LOGGER.exception("WS listener 处理 %s 事件异常", payload.get("type"))

    async def async_ws_close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
