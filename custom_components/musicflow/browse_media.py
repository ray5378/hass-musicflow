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

封面:节点的 thumbnail 由调用方通过 `thumb` 回调决定。media_player 传的是
"走 HA 代理"的实现(`get_browse_image_url`),这样从外网访问 HA 时前端不必去
直连内网的 MusicFlow;不传则回退成 MusicFlow 直链。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, BrowseMedia, MediaClass, MediaType

# 新版 HA(约 2024.x+) 的 async_search_media 需返回 SearchMedia(result=[...]),
# 旧版返回 BrowseMedia 根节点即可。这里做兼容导入。
try:
    from homeassistant.components.media_player import SearchMedia
except ImportError:  # pragma: no cover - 旧版 HA 没有 SearchMedia
    SearchMedia = None

from .api import MusicFlowClient
from .const import BROWSE_LIMIT, DOMAIN, MEDIA_URI_PREFIX

ROOT_ID = MEDIA_URI_PREFIX

# (media_content_type, media_content_id, cover_art) → thumbnail URL
ThumbFn = Callable[[str, str, str | None], str | None]


def parse_media_id(media_content_id: str) -> tuple[str, str]:
    """把 `musicflow://album/123` 拆成 ("album", "123")。

    流派名可能含 `/`(如 "Rock/Pop"),所以只切第一个斜杠,并做 URL 解码。
    """
    path = media_content_id.removeprefix(MEDIA_URI_PREFIX)
    kind, _, rest = path.partition("/")
    return kind, unquote(rest)


def _direct_thumb(client: MusicFlowClient) -> ThumbFn:
    """默认封面策略:MusicFlow 直链(局域网内最省事,外网访问会拉不到)。"""

    def _thumb(_content_type: str, _content_id: str, cover_art: str | None) -> str | None:
        return client.cover_url(cover_art)

    return _thumb


async def build_browse_media(
    client: MusicFlowClient,
    media_content_id: str | None,
    thumb: ThumbFn | None = None,
) -> BrowseMedia:
    """返回 media_content_id 对应的 BrowseMedia 节点。"""
    thumb = thumb or _direct_thumb(client)

    if not media_content_id or media_content_id == ROOT_ID:
        return _root_menu()

    kind, value = parse_media_id(media_content_id)

    if kind == "playlists":
        return await _browse_playlists(client, thumb)
    if kind == "playlist" and value:
        return await _browse_playlist(client, thumb, value)
    if kind == "albums":
        return await _browse_albums(client, thumb)
    if kind == "album" and value:
        return await _browse_album(client, thumb, value)
    if kind == "artists":
        return await _browse_artists(client, thumb)
    if kind == "artist" and value:
        return await _browse_artist(client, thumb, value)
    if kind == "genres":
        return await _browse_genres(client)
    if kind == "genre" and value:
        return await _browse_genre(client, thumb, value)

    raise BrowseError(
        translation_domain=DOMAIN,
        translation_key="cannot_browse",
        translation_placeholders={"media_content_id": media_content_id},
    )


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
        can_search=True,
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
async def _browse_playlists(client: MusicFlowClient, thumb: ThumbFn) -> BrowseMedia:
    resp = await client.async_get_playlists()
    playlists = _as_list(resp.get("playlists", {}).get("playlist"))
    children = [_playlist_node(thumb, p) for p in playlists if p.get("id") is not None]
    return _directory("歌单", f"{MEDIA_URI_PREFIX}playlists", MediaType.PLAYLIST, children, MediaClass.PLAYLIST)


async def _browse_playlist(
    client: MusicFlowClient, thumb: ThumbFn, playlist_id: str
) -> BrowseMedia:
    resp = await client.async_get_playlist(playlist_id)
    playlist = resp.get("playlist", {})
    children = [_song_node(thumb, s) for s in _as_list(playlist.get("entry"))]
    content_id = f"{MEDIA_URI_PREFIX}playlist/{quote(playlist_id, safe='')}"
    return _directory(
        playlist.get("name") or "歌单",
        content_id,
        MediaType.PLAYLIST,
        children,
        MediaClass.TRACK,
        can_play=True,
        thumbnail=thumb(MediaType.PLAYLIST, content_id, playlist.get("coverArt")),
    )


# ==================== 专辑 ====================
async def _browse_albums(client: MusicFlowClient, thumb: ThumbFn) -> BrowseMedia:
    resp = await client.async_get_album_list("newest", BROWSE_LIMIT)
    albums = _as_list(resp.get("albumList", {}).get("album"))
    children = [_album_node(thumb, a) for a in albums if a.get("id") is not None]
    return _directory("专辑", f"{MEDIA_URI_PREFIX}albums", MediaType.ALBUM, children, MediaClass.ALBUM)


async def _browse_album(
    client: MusicFlowClient, thumb: ThumbFn, album_id: str
) -> BrowseMedia:
    resp = await client.async_get_album(album_id)
    album = resp.get("album", {})
    children = [_song_node(thumb, s) for s in _as_list(album.get("song"))]
    content_id = f"{MEDIA_URI_PREFIX}album/{quote(album_id, safe='')}"
    return _directory(
        _join(album.get("artist"), album.get("name") or "专辑"),
        content_id,
        MediaType.ALBUM,
        children,
        MediaClass.TRACK,
        can_play=True,
        thumbnail=thumb(MediaType.ALBUM, content_id, album.get("coverArt")),
    )


# ==================== 艺术家 ====================
async def _browse_artists(client: MusicFlowClient, thumb: ThumbFn) -> BrowseMedia:
    resp = await client.async_get_artists()
    children: list[BrowseMedia] = []
    for index in _as_list(resp.get("artists", {}).get("index")):
        for artist in _as_list(index.get("artist")):
            if artist.get("id") is None:
                continue
            children.append(_artist_node(thumb, artist))
    return _directory("艺术家", f"{MEDIA_URI_PREFIX}artists", MediaType.ARTIST, children, MediaClass.ARTIST)


async def _browse_artist(
    client: MusicFlowClient, thumb: ThumbFn, artist_id: str
) -> BrowseMedia:
    resp = await client.async_get_artist(artist_id)
    artist = resp.get("artist", {})
    children = [_album_node(thumb, a) for a in _as_list(artist.get("album")) if a.get("id") is not None]
    content_id = f"{MEDIA_URI_PREFIX}artist/{quote(artist_id, safe='')}"
    return _directory(
        artist.get("name") or "艺术家",
        content_id,
        MediaType.ARTIST,
        children,
        MediaClass.ALBUM,
        can_play=True,
        thumbnail=thumb(MediaType.ARTIST, content_id, artist.get("coverArt")),
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


async def _browse_genre(
    client: MusicFlowClient, thumb: ThumbFn, genre: str
) -> BrowseMedia:
    resp = await client.async_get_songs_by_genre(genre, BROWSE_LIMIT)
    songs = _as_list(resp.get("songsByGenre", {}).get("song"))
    children = [_song_node(thumb, s) for s in songs]
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


def _album_node(thumb: ThumbFn, album: dict) -> BrowseMedia:
    content_id = f"{MEDIA_URI_PREFIX}album/{quote(str(album['id']), safe='')}"
    return BrowseMedia(
        title=_join(album.get("artist"), album.get("name") or "未知专辑"),
        media_class=MediaClass.ALBUM,
        media_content_id=content_id,
        media_content_type=MediaType.ALBUM,
        can_play=True,
        can_expand=True,
        thumbnail=thumb(MediaType.ALBUM, content_id, album.get("coverArt")),
    )


def _song_node(thumb: ThumbFn, song: dict) -> BrowseMedia:
    content_id = f"{MEDIA_URI_PREFIX}song/{quote(str(song.get('id', '')), safe='')}"
    return BrowseMedia(
        title=song.get("title") or "未知曲目",
        media_class=MediaClass.TRACK,
        media_content_id=content_id,
        media_content_type=MediaType.TRACK,
        can_play=True,
        can_expand=False,
        thumbnail=thumb(MediaType.TRACK, content_id, song.get("coverArt")),
    )


def _artist_node(thumb: ThumbFn, artist: dict) -> BrowseMedia:
    content_id = f"{MEDIA_URI_PREFIX}artist/{quote(str(artist['id']), safe='')}"
    return BrowseMedia(
        title=artist.get("name") or "未知艺术家",
        media_class=MediaClass.ARTIST,
        media_content_id=content_id,
        media_content_type=MediaType.ARTIST,
        can_play=True,
        can_expand=True,
        thumbnail=thumb(MediaType.ARTIST, content_id, artist.get("coverArt")),
    )


def _playlist_node(thumb: ThumbFn, playlist: dict) -> BrowseMedia:
    content_id = f"{MEDIA_URI_PREFIX}playlist/{quote(str(playlist['id']), safe='')}"
    return BrowseMedia(
        title=playlist.get("name") or "未命名歌单",
        media_class=MediaClass.PLAYLIST,
        media_content_id=content_id,
        media_content_type=MediaType.PLAYLIST,
        can_play=True,
        can_expand=True,
        thumbnail=thumb(MediaType.PLAYLIST, content_id, playlist.get("coverArt")),
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
    can_search: bool = True,
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
        can_search=can_search,
    )


# ==================== 搜索 ====================
async def build_search_results(
    client: MusicFlowClient,
    query: str,
    limit: int = 30,
    thumb: ThumbFn | None = None,
) -> BrowseMedia | "SearchMedia":
    """把 search3 的结果拼成一个可浏览的搜索结果。

    search3 返回 artist/album/song;歌单不在结果里,这里单独拉取并按名称过滤。
    专辑/艺术家/歌单可继续展开(点击后走既有 browse 路径),歌曲可直接播放。

    新版 HA 要求 async_search_media 返回 SearchMedia(result=[...BrowseMedia]),
    旧版则返回 BrowseMedia 根节点 —— 此处按可用类做兼容。
    """
    thumb = thumb or _direct_thumb(client)

    resp = await client.async_search(query, count=limit)
    result = resp.get("searchResult3", resp)
    albums = _as_list(result.get("album"))
    artists = _as_list(result.get("artist"))
    songs = _as_list(result.get("song"))

    playlists_resp = await client.async_get_playlists()
    q = query.strip().lower()
    playlists = [
        p
        for p in _as_list(playlists_resp.get("playlists", {}).get("playlist"))
        if q and q in (p.get("name") or "").lower()
    ]

    children: list[BrowseMedia] = []
    children.extend(_album_node(thumb, a) for a in albums if a.get("id") is not None)
    children.extend(_artist_node(thumb, a) for a in artists if a.get("id") is not None)
    children.extend(_playlist_node(thumb, p) for p in playlists if p.get("id") is not None)
    children.extend(_song_node(thumb, s) for s in songs if s.get("id") is not None)

    if not children:
        children = [
            BrowseMedia(
                title="没有找到匹配的结果",
                media_class=MediaClass.DIRECTORY,
                media_content_id=f"{MEDIA_URI_PREFIX}search/empty",
                media_content_type="",
                can_play=False,
                can_expand=False,
            )
        ]

    if SearchMedia is not None:
        # 新版 HA: 搜索结果必须是 SearchMedia(result=[...])
        return SearchMedia(result=children)

    # 旧版 HA 兼容: 返回包裹的根节点
    return BrowseMedia(
        title=f"搜索: {query}",
        media_class=MediaClass.DIRECTORY,
        media_content_id=f"{MEDIA_URI_PREFIX}search/{quote(query, safe='')}",
        media_content_type="",
        can_play=False,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.ALBUM,
        can_search=True,
    )
