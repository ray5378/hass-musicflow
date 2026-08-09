"""把 MusicFlow 曲库接进 Home Assistant 的全局「媒体」标签页。

和 media_player 里的浏览器(browse_media.py)是两套不同的东西:

  - media_player 的浏览器只服务于 MusicFlow 自己的实体,点一个专辑就把整张专辑
    的队列交给后端去排 —— 能力全,但只有 MusicFlow 播放器能用。
  - media_source 是 HA 的通用协议:曲库会出现在「媒体」标签页里,任何 HA 播放器
    (Chromecast、Sonos、esphome、浏览器本地播放…)都能播,自动化里也能直接引用
    `media-source://musicflow/...`。代价是 HA 只认"一个条目解析成一个 URL",
    所以只有单曲 can_play,专辑/歌单只能展开。

identifier 结构:  <config_entry_id>/<kind>/<value>
  ""                              → 根(多台服务器时先选服务器)
  <entry>                         → 该服务器的分类入口
  <entry>/playlists               → 歌单列表
  <entry>/playlist/<id>           → 歌单内曲目
  <entry>/albums、<entry>/album/<id>、<entry>/artists、<entry>/artist/<id>
  <entry>/genres、<entry>/genre/<name>
  <entry>/song/<id>               → 叶子,解析成 /rest/stream 直链
"""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .api import MusicFlowClient, MusicFlowError
from .browse_media import _as_list, _join
from .const import BROWSE_LIMIT, DOMAIN
from .coordinator import MusicFlowCoordinator

_LOGGER = logging.getLogger(__name__)

# 后缀 → mime。后端 songToChild 一般会给 contentType,这里只是兜底。
_MIME_BY_SUFFIX = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
    "ape": "audio/x-ape",
    "alac": "audio/mp4",
}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """HA 的 media_source 平台入口。"""
    return MusicFlowMediaSource(hass)


class MusicFlowMediaSource(MediaSource):
    """MusicFlow 曲库的 media_source 实现。"""

    name = "MusicFlow"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ==================== 内部工具 ====================
    def _entries(self) -> dict[str, MusicFlowCoordinator]:
        return dict(self.hass.data.get(DOMAIN) or {})

    def _client(self, entry_id: str) -> MusicFlowClient:
        coordinator = self._entries().get(entry_id)
        if coordinator is None:
            raise Unresolvable(f"MusicFlow 服务器 {entry_id} 未加载")
        return coordinator.client

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        """`<entry>/<kind>/<value>` → (entry, kind, value)。value 可能含 `/`。"""
        entry_id, _, rest = (identifier or "").partition("/")
        kind, _, value = rest.partition("/")
        return entry_id, kind, unquote(value)

    # ==================== 解析(播放)====================
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        entry_id, kind, value = self._split(item.identifier)
        if kind != "song" or not value:
            raise Unresolvable(f"只能播放单曲,收到 {item.identifier}")
        client = self._client(entry_id)
        try:
            song = await client.async_get_song(value)
        except MusicFlowError as err:
            raise Unresolvable(f"读取曲目信息失败: {err}") from err
        mime = song.get("contentType") or _MIME_BY_SUFFIX.get(
            str(song.get("suffix") or "").lower(), "audio/mpeg"
        )
        return PlayMedia(client.stream_url(value), mime)

    # ==================== 浏览 ====================
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        entries = self._entries()
        if not entries:
            raise BrowseError("还没有配置 MusicFlow 服务器")

        identifier = item.identifier or ""
        if not identifier:
            # 只有一台服务器时不必让用户先点一层,直接进分类
            if len(entries) == 1:
                only = next(iter(entries))
                return self._categories(only)
            return self._servers(entries)

        entry_id, kind, value = self._split(identifier)
        if entry_id not in entries:
            raise BrowseError("MusicFlow 服务器未加载")
        client = entries[entry_id].client

        try:
            if not kind:
                return self._categories(entry_id)
            if kind == "playlists":
                return await self._playlists(client, entry_id)
            if kind == "playlist":
                return await self._playlist(client, entry_id, value)
            if kind == "albums":
                return await self._albums(client, entry_id)
            if kind == "album":
                return await self._album(client, entry_id, value)
            if kind == "artists":
                return await self._artists(client, entry_id)
            if kind == "artist":
                return await self._artist(client, entry_id, value)
            if kind == "genres":
                return await self._genres(client, entry_id)
            if kind == "genre":
                return await self._genre(client, entry_id, value)
        except MusicFlowError as err:
            raise BrowseError(f"浏览 MusicFlow 曲库失败: {err}") from err

        raise BrowseError(f"无法浏览 {identifier}")

    # ---- 各层级 ----
    def _servers(self, entries: dict[str, MusicFlowCoordinator]) -> BrowseMediaSource:
        children = [
            self._dir(entry_id, "", coordinator.entry.title, MediaClass.DIRECTORY)
            for entry_id, coordinator in entries.items()
        ]
        return self._dir("", "", "MusicFlow", MediaClass.DIRECTORY, children=children)

    def _categories(self, entry_id: str) -> BrowseMediaSource:
        entries = (
            ("歌单", "playlists", MediaClass.PLAYLIST),
            ("专辑", "albums", MediaClass.ALBUM),
            ("艺术家", "artists", MediaClass.ARTIST),
            ("流派", "genres", MediaClass.GENRE),
        )
        children = [
            self._dir(entry_id, slug, title, media_class)
            for title, slug, media_class in entries
        ]
        return self._dir(entry_id, "", "MusicFlow", MediaClass.DIRECTORY, children=children)

    async def _playlists(self, client, entry_id) -> BrowseMediaSource:
        resp = await client.async_get_playlists()
        children = [
            self._dir(
                entry_id,
                f"playlist/{quote(str(p['id']), safe='')}",
                p.get("name") or "未命名歌单",
                MediaClass.PLAYLIST,
                thumbnail=client.cover_url(p.get("coverArt")),
            )
            for p in _as_list(resp.get("playlists", {}).get("playlist"))
            if p.get("id") is not None
        ]
        return self._dir(entry_id, "playlists", "歌单", MediaClass.PLAYLIST, children=children)

    async def _playlist(self, client, entry_id, playlist_id) -> BrowseMediaSource:
        resp = await client.async_get_playlist(playlist_id)
        playlist = resp.get("playlist", {})
        children = [self._song(client, entry_id, s) for s in _as_list(playlist.get("entry"))]
        return self._dir(
            entry_id,
            f"playlist/{quote(playlist_id, safe='')}",
            playlist.get("name") or "歌单",
            MediaClass.TRACK,
            children=children,
            thumbnail=client.cover_url(playlist.get("coverArt")),
        )

    async def _albums(self, client, entry_id) -> BrowseMediaSource:
        resp = await client.async_get_album_list("newest", BROWSE_LIMIT)
        children = [
            self._album_dir(client, entry_id, a)
            for a in _as_list(resp.get("albumList", {}).get("album"))
            if a.get("id") is not None
        ]
        return self._dir(entry_id, "albums", "专辑", MediaClass.ALBUM, children=children)

    async def _album(self, client, entry_id, album_id) -> BrowseMediaSource:
        resp = await client.async_get_album(album_id)
        album = resp.get("album", {})
        children = [self._song(client, entry_id, s) for s in _as_list(album.get("song"))]
        return self._dir(
            entry_id,
            f"album/{quote(album_id, safe='')}",
            _join(album.get("artist"), album.get("name") or "专辑"),
            MediaClass.TRACK,
            children=children,
            thumbnail=client.cover_url(album.get("coverArt")),
        )

    async def _artists(self, client, entry_id) -> BrowseMediaSource:
        resp = await client.async_get_artists()
        children: list[BrowseMediaSource] = []
        for index in _as_list(resp.get("artists", {}).get("index")):
            for artist in _as_list(index.get("artist")):
                if artist.get("id") is None:
                    continue
                children.append(
                    self._dir(
                        entry_id,
                        f"artist/{quote(str(artist['id']), safe='')}",
                        artist.get("name") or "未知艺术家",
                        MediaClass.ARTIST,
                        thumbnail=client.cover_url(artist.get("coverArt")),
                    )
                )
        return self._dir(entry_id, "artists", "艺术家", MediaClass.ARTIST, children=children)

    async def _artist(self, client, entry_id, artist_id) -> BrowseMediaSource:
        resp = await client.async_get_artist(artist_id)
        artist = resp.get("artist", {})
        children = [
            self._album_dir(client, entry_id, a)
            for a in _as_list(artist.get("album"))
            if a.get("id") is not None
        ]
        return self._dir(
            entry_id,
            f"artist/{quote(artist_id, safe='')}",
            artist.get("name") or "艺术家",
            MediaClass.ALBUM,
            children=children,
            thumbnail=client.cover_url(artist.get("coverArt")),
        )

    async def _genres(self, client, entry_id) -> BrowseMediaSource:
        resp = await client.async_get_genres()
        children: list[BrowseMediaSource] = []
        for g in _as_list(resp.get("genres", {}).get("genre")):
            name = g.get("value") or g.get("name")
            if not name:
                continue
            children.append(
                self._dir(
                    entry_id,
                    f"genre/{quote(str(name), safe='')}",
                    f"{name} ({g.get('songCount', 0)})",
                    MediaClass.GENRE,
                )
            )
        return self._dir(entry_id, "genres", "流派", MediaClass.GENRE, children=children)

    async def _genre(self, client, entry_id, genre) -> BrowseMediaSource:
        resp = await client.async_get_songs_by_genre(genre, BROWSE_LIMIT)
        children = [
            self._song(client, entry_id, s)
            for s in _as_list(resp.get("songsByGenre", {}).get("song"))
        ]
        return self._dir(
            entry_id,
            f"genre/{quote(genre, safe='')}",
            genre,
            MediaClass.TRACK,
            children=children,
        )

    # ---- 节点工厂 ----
    def _dir(
        self,
        entry_id: str,
        path: str,
        title: str,
        children_class: MediaClass,
        *,
        children: list[BrowseMediaSource] | None = None,
        thumbnail: str | None = None,
    ) -> BrowseMediaSource:
        identifier = f"{entry_id}/{path}" if path else entry_id
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            title=title,
            can_play=False,
            can_expand=True,
            children=children or [],
            children_media_class=children_class,
            thumbnail=thumbnail,
        )

    def _album_dir(self, client, entry_id, album: dict) -> BrowseMediaSource:
        return self._dir(
            entry_id,
            f"album/{quote(str(album['id']), safe='')}",
            _join(album.get("artist"), album.get("name") or "未知专辑"),
            MediaClass.TRACK,
            thumbnail=client.cover_url(album.get("coverArt")),
        )

    def _song(self, client, entry_id: str, song: dict) -> BrowseMediaSource:
        song_id = str(song.get("id", ""))
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry_id}/song/{quote(song_id, safe='')}",
            media_class=MediaClass.TRACK,
            media_content_type=MediaType.MUSIC,
            title=song.get("title") or "未知曲目",
            can_play=True,
            can_expand=False,
            thumbnail=client.cover_url(song.get("coverArt")),
        )
