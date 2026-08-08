"""OpenSubsonic 接口 → HA BrowseMedia 树。

media_content_id 编码方案(与 `/rest/api/v1/play` 的 type 一一对应,
播放时直接拆出 type + id 丢给后端,集成侧不解析曲目列表):

  musicflow://                根(分类入口)
  musicflow://playlists       歌单列表
  musicflow://playlist/<id>   歌单内曲目          → play type=playlist
  musicflow://albums          专辑列表(最新)
  musicflow://album/<id>      专辑内曲目          → play type=album
  musicflow://artists         艺术家列表
  musicflow://artist/<id>     艺术家的专辑        → play type=artist
  musicflow://genres          流派列表
  musicflow://genre/<name>    流派内曲目          → play type=genre
  musicflow://song/<id>       单曲(叶子)         → play type=song
"""

from __future__ import annotations

from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, BrowseMedia, MediaClass, MediaType

from .api import MusicFlowClient
from .const import BROWSE_LIMIT, MEDIA_URI_PREFIX

ROOT_ID = MEDIA_URI_PREFIX


def parse_media_id(media_content_id: str) -> tuple[str, str]:
    """把 `musicflow://album/123` 拆成 ("album", "123")。

    流派名可能含 `/`(如 "Rock/Pop"),所以只切第一个斜杠,并做 URL 解码。
    """
    path = media_content_id.removeprefix(MEDIA_URI_PREFIX)
    kind, _, rest = path.partition("/")
    return kind, unquote(rest)


async def build_browse_media(
    client: MusicFlowClient,
    media_content_id: str | None,
) -> BrowseMedia:
    """返回 media_content_id 对应的 BrowseMedia 节点。"""
    if not media_content_id or media_content_id == ROOT_ID:
        return _root_menu()

    kind, value = parse_media_id(media_content_id)

    if kind == "playlists":
        return await _browse_playlists(client)
    if kind == "playlist" and value:
        return await _browse_playlist(client, value)
    if kind == "albums":
        return await _browse_albums(client)
    if kind == "album" and value:
        return await _browse_album(client, value)
    if kind == "artists":
        return await _browse_artists(client)
    if kind == "artist" and value:
        return await _browse_artist(client, value)
    if kind == "genres":
        return await _browse_genres(client)
    if kind == "genre" and value:
        return await _browse_genre(client, value)

    raise BrowseError(f"无法浏览 {media_content_id}")


# ==================== 根菜单 ====================
def _root_menu() -> BrowseMedia:
    entries = (
        ("歌单", "playlists", MediaClass.PLAYLIST, MediaType.PLAYLIST),
        ("专辑", "albums", MediaClass.ALBUM, MediaType.ALBUM),
        ("艺术家", "artists", MediaClass.ARTIST, MediaType.ARTIST),
        ("流派", "genres", MediaClass.GENRE, MediaType.GENRE),
    )
    return BrowseMedia(
        title="MusicFlow",
        media_class=MediaClass.DIRECTORY,
        media_content_id=ROOT_ID,
        media_content_type="",
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.DIRECTORY,
        children=[
            BrowseMedia(
                title=title,
                media_class=media_class,
                media_content_id=f"{MEDIA_URI_PREFIX}{slug}",
                media_content_type=media_type,
                can_play=False,
                can_expand=True,
            )
            for title, slug, media_class, media_type in entries
        ],
    )


# ==================== 歌单 ====================
async def _browse_playlists(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.async_get_playlists()
    playlists = _as_list(resp.get("playlists", {}).get("playlist"))
    children = [
        BrowseMedia(
            title=p.get("name") or "未命名歌单",
            media_class=MediaClass.PLAYLIST,
            media_content_id=f"{MEDIA_URI_PREFIX}playlist/{quote(str(p['id']), safe='')}",
            media_content_type=MediaType.PLAYLIST,
            can_play=True,
            can_expand=True,
            thumbnail=client.cover_url(p.get("coverArt")),
        )
        for p in playlists
        if p.get("id") is not None
    ]
    return _directory("歌单", f"{MEDIA_URI_PREFIX}playlists", MediaType.PLAYLIST, children, MediaClass.PLAYLIST)


async def _browse_playlist(client: MusicFlowClient, playlist_id: str) -> BrowseMedia:
    resp = await client.async_get_playlist(playlist_id)
    playlist = resp.get("playlist", {})
    children = [_song_node(client, s) for s in _as_list(playlist.get("entry"))]
    return _directory(
        playlist.get("name") or "歌单",
        f"{MEDIA_URI_PREFIX}playlist/{quote(playlist_id, safe='')}",
        MediaType.PLAYLIST,
        children,
        MediaClass.TRACK,
        can_play=True,
        thumbnail=client.cover_url(playlist.get("coverArt")),
    )


# ==================== 专辑 ====================
async def _browse_albums(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.async_get_album_list("newest", BROWSE_LIMIT)
    albums = _as_list(resp.get("albumList", {}).get("album"))
    children = [_album_node(client, a) for a in albums if a.get("id") is not None]
    return _directory("专辑", f"{MEDIA_URI_PREFIX}albums", MediaType.ALBUM, children, MediaClass.ALBUM)


async def _browse_album(client: MusicFlowClient, album_id: str) -> BrowseMedia:
    resp = await client.async_get_album(album_id)
    album = resp.get("album", {})
    children = [_song_node(client, s) for s in _as_list(album.get("song"))]
    return _directory(
        _join(album.get("artist"), album.get("name") or "专辑"),
        f"{MEDIA_URI_PREFIX}album/{quote(album_id, safe='')}",
        MediaType.ALBUM,
        children,
        MediaClass.TRACK,
        can_play=True,
        thumbnail=client.cover_url(album.get("coverArt")),
    )


# ==================== 艺术家 ====================
async def _browse_artists(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.async_get_artists()
    children: list[BrowseMedia] = []
    for index in _as_list(resp.get("artists", {}).get("index")):
        for artist in _as_list(index.get("artist")):
            if artist.get("id") is None:
                continue
            children.append(
                BrowseMedia(
                    title=artist.get("name") or "未知艺术家",
                    media_class=MediaClass.ARTIST,
                    media_content_id=f"{MEDIA_URI_PREFIX}artist/{quote(str(artist['id']), safe='')}",
                    media_content_type=MediaType.ARTIST,
                    can_play=True,
                    can_expand=True,
                    thumbnail=client.cover_url(artist.get("coverArt")),
                )
            )
    return _directory("艺术家", f"{MEDIA_URI_PREFIX}artists", MediaType.ARTIST, children, MediaClass.ARTIST)


async def _browse_artist(client: MusicFlowClient, artist_id: str) -> BrowseMedia:
    resp = await client.async_get_artist(artist_id)
    artist = resp.get("artist", {})
    children = [_album_node(client, a) for a in _as_list(artist.get("album")) if a.get("id") is not None]
    return _directory(
        artist.get("name") or "艺术家",
        f"{MEDIA_URI_PREFIX}artist/{quote(artist_id, safe='')}",
        MediaType.ARTIST,
        children,
        MediaClass.ALBUM,
        can_play=True,
        thumbnail=client.cover_url(artist.get("coverArt")),
    )


# ==================== 流派 ====================
async def _browse_genres(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.async_get_genres()
    genres = _as_list(resp.get("genres", {}).get("genre"))
    children: list[BrowseMedia] = []
    for g in genres:
        # OpenSubsonic 的 genre 条目文本在 "value",部分实现放在 "name"
        name = g.get("value") or g.get("name")
        if not name:
            continue
        children.append(
            BrowseMedia(
                title=f"{name} ({g.get('songCount', 0)})",
                media_class=MediaClass.GENRE,
                media_content_id=f"{MEDIA_URI_PREFIX}genre/{quote(str(name), safe='')}",
                media_content_type=MediaType.GENRE,
                can_play=True,
                can_expand=True,
            )
        )
    return _directory("流派", f"{MEDIA_URI_PREFIX}genres", MediaType.GENRE, children, MediaClass.GENRE)


async def _browse_genre(client: MusicFlowClient, genre: str) -> BrowseMedia:
    resp = await client.async_get_songs_by_genre(genre, BROWSE_LIMIT)
    songs = _as_list(resp.get("songsByGenre", {}).get("song"))
    children = [_song_node(client, s) for s in songs]
    return _directory(
        genre,
        f"{MEDIA_URI_PREFIX}genre/{quote(genre, safe='')}",
        MediaType.GENRE,
        children,
        MediaClass.TRACK,
        can_play=True,
    )


# ==================== 工具 ====================
def _as_list(value) -> list[dict]:
    """OpenSubsonic 单条结果可能不是数组,统一成 list[dict]。"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _join(artist: str | None, name: str) -> str:
    return f"{artist} - {name}" if artist else name


def _album_node(client: MusicFlowClient, album: dict) -> BrowseMedia:
    return BrowseMedia(
        title=_join(album.get("artist"), album.get("name") or "未知专辑"),
        media_class=MediaClass.ALBUM,
        media_content_id=f"{MEDIA_URI_PREFIX}album/{quote(str(album['id']), safe='')}",
        media_content_type=MediaType.ALBUM,
        can_play=True,
        can_expand=True,
        thumbnail=client.cover_url(album.get("coverArt")),
    )


def _song_node(client: MusicFlowClient, song: dict) -> BrowseMedia:
    return BrowseMedia(
        title=song.get("title") or "未知曲目",
        media_class=MediaClass.TRACK,
        media_content_id=f"{MEDIA_URI_PREFIX}song/{quote(str(song.get('id', '')), safe='')}",
        media_content_type=MediaType.TRACK,
        can_play=True,
        can_expand=False,
        thumbnail=client.cover_url(song.get("coverArt")),
    )


def _directory(
    title: str,
    content_id: str,
    content_type: str,
    children: list[BrowseMedia],
    children_class: MediaClass,
    *,
    can_play: bool = False,
    thumbnail: str | None = None,
) -> BrowseMedia:
    return BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id=content_id,
        media_content_type=content_type,
        can_play=can_play,
        can_expand=True,
        children=children,
        children_media_class=children_class,
        thumbnail=thumbnail,
    )
