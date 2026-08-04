"""OpenSubsonic 接口 → HA BrowseMedia 树映射。

media_content_id 编码方案:
  musicflow://                      根(分类入口)
  musicflow://artists               艺术家列表
  musicflow://artist/<id>           某艺术家的专辑
  musicflow://albums                专辑列表(默认 newest)
  musicflow://album/<id>            某专辑的曲目(可整张播放)
  musicflow://playlists             歌单列表
  musicflow://playlist/<id>         某歌单的曲目
  musicflow://song/<id>             单曲(叶子节点)
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import BrowseMedia, MediaClass

from .api import MusicFlowClient
from .const import BROWSE_LIMIT, MEDIA_URI_PREFIX


async def build_browse_media(
    client: MusicFlowClient,
    media_content_type: str | None,
    media_content_id: str | None,
) -> BrowseMedia:
    """根据 media_content_id 返回对应的 BrowseMedia 节点。"""
    if media_content_id in (None, "", "musicflow://"):
        return _root_menu(client)

    path = media_content_id.replace(MEDIA_URI_PREFIX, "")
    parts = path.split("/")
    kind = parts[0] if parts else ""

    if kind == "artists":
        return await _browse_artists(client)
    if kind == "artist" and len(parts) > 1:
        return await _browse_artist(client, parts[1])
    if kind == "albums":
        return await _browse_albums(client)
    if kind == "album" and len(parts) > 1:
        return await _browse_album(client, parts[1])
    if kind == "playlists":
        return await _browse_playlists(client)
    if kind == "playlist" and len(parts) > 1:
        return await _browse_playlist(client, parts[1])

    return _root_menu(client)


# ==================== 根菜单 ====================
def _root_menu(client: MusicFlowClient) -> BrowseMedia:
    """根菜单:艺术家 / 专辑 / 歌单。"""
    return BrowseMedia(
        title="MusicFlow",
        media_class=MediaClass.DIRECTORY,
        media_content_id="musicflow://",
        media_content_type="directory",
        can_play=False,
        can_expand=True,
        children=[
            BrowseMedia(
                title="艺术家",
                media_class=MediaClass.ARTIST,
                media_content_id="musicflow://artists",
                media_content_type="artist",
                can_play=False,
                can_expand=True,
            ),
            BrowseMedia(
                title="专辑",
                media_class=MediaClass.ALBUM,
                media_content_id="musicflow://albums",
                media_content_type="album",
                can_play=False,
                can_expand=True,
            ),
            BrowseMedia(
                title="歌单",
                media_class=MediaClass.PLAYLIST,
                media_content_id="musicflow://playlists",
                media_content_type="playlist",
                can_play=False,
                can_expand=True,
            ),
        ],
    )


# ==================== 艺术家 ====================
async def _browse_artists(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.get_artists()
    artists_data = resp.get("artists", {})
    children: list[BrowseMedia] = []
    for index in artists_data.get("index", []):
        for artist in index.get("artist", []):
            children.append(
                BrowseMedia(
                    title=artist.get("name", "未知"),
                    media_class=MediaClass.ARTIST,
                    media_content_id=f"musicflow://artist/{artist['id']}",
                    media_content_type="artist",
                    can_play=False,
                    can_expand=True,
                    thumbnail=_cover_url(client, artist.get("coverArt")),
                )
            )
    return _make_dir("艺术家", "musicflow://artists", children, MediaClass.ARTIST)


async def _browse_artist(client: MusicFlowClient, artist_id: str) -> BrowseMedia:
    resp = await client.get_artist(artist_id)
    artist = resp.get("artist", {})
    children: list[BrowseMedia] = []
    for album in artist.get("album", []):
        children.append(
            BrowseMedia(
                title=album.get("name", "未知专辑"),
                media_class=MediaClass.ALBUM,
                media_content_id=f"musicflow://album/{album['id']}",
                media_content_type="album",
                can_play=True,
                can_expand=True,
                thumbnail=_cover_url(client, album.get("coverArt")),
            )
        )
    return _make_dir(
        artist.get("name", "艺术家"),
        f"musicflow://artist/{artist_id}",
        children,
        MediaClass.ALBUM,
    )


# ==================== 专辑 ====================
async def _browse_albums(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.get_album_list("newest", BROWSE_LIMIT)
    albums = resp.get("albumList", {}).get("album", [])
    children: list[BrowseMedia] = [
        BrowseMedia(
            title=f"{a.get('artist', '')} - {a.get('name', '未知')}".strip(" -"),
            media_class=MediaClass.ALBUM,
            media_content_id=f"musicflow://album/{a['id']}",
            media_content_type="album",
            can_play=True,
            can_expand=True,
            thumbnail=_cover_url(client, a.get("coverArt")),
        )
        for a in albums
    ]
    return _make_dir("专辑", "musicflow://albums", children, MediaClass.ALBUM)


async def _browse_album(client: MusicFlowClient, album_id: str) -> BrowseMedia:
    resp = await client.get_album(album_id)
    album = resp.get("album", {})
    children: list[BrowseMedia] = []
    for song in album.get("song", []):
        children.append(
            BrowseMedia(
                title=song.get("title", "未知曲目"),
                media_class=MediaClass.TRACK,
                media_content_id=f"musicflow://song/{song['id']}",
                media_content_type="track",
                can_play=True,
                can_expand=False,
                thumbnail=_cover_url(client, song.get("coverArt")),
            )
        )
    title = f"{album.get('artist', '')} - {album.get('name', '专辑')}".strip(" -")
    return _make_dir(
        title,
        f"musicflow://album/{album_id}",
        children,
        MediaClass.ALBUM,
        can_play=True,
    )


# ==================== 歌单 ====================
async def _browse_playlists(client: MusicFlowClient) -> BrowseMedia:
    resp = await client.get_playlists()
    playlists = resp.get("playlists", {}).get("playlist", [])
    children: list[BrowseMedia] = [
        BrowseMedia(
            title=p.get("name", "未知歌单"),
            media_class=MediaClass.PLAYLIST,
            media_content_id=f"musicflow://playlist/{p['id']}",
            media_content_type="playlist",
            can_play=True,
            can_expand=True,
        )
        for p in playlists
    ]
    return _make_dir("歌单", "musicflow://playlists", children, MediaClass.PLAYLIST)


async def _browse_playlist(client: MusicFlowClient, playlist_id: str) -> BrowseMedia:
    resp = await client.get_playlist(playlist_id)
    playlist = resp.get("playlist", {})
    children: list[BrowseMedia] = []
    for entry in playlist.get("entry", []):
        children.append(
            BrowseMedia(
                title=entry.get("title", "未知曲目"),
                media_class=MediaClass.TRACK,
                media_content_id=f"musicflow://song/{entry['id']}",
                media_content_type="track",
                can_play=True,
                can_expand=False,
                thumbnail=_cover_url(client, entry.get("coverArt")),
            )
        )
    return _make_dir(
        playlist.get("name", "歌单"),
        f"musicflow://playlist/{playlist_id}",
        children,
        MediaClass.PLAYLIST,
        can_play=True,
    )


# ==================== 工具 ====================
def _make_dir(
    title: str,
    content_id: str,
    children: list[BrowseMedia],
    media_class: MediaClass = MediaClass.DIRECTORY,
    can_play: bool = False,
) -> BrowseMedia:
    return BrowseMedia(
        title=title,
        media_class=media_class,
        media_content_id=content_id,
        media_content_type="directory",
        can_play=can_play,
        can_expand=True,
        children=children or None,
    )


def _cover_url(client: MusicFlowClient, cover_art_id: str | None) -> str | None:
    if not cover_art_id:
        return None
    return f"{client.base_url}/rest/getCoverArt?id={cover_art_id}&size=300"
