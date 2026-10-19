"""media_source.py 单测（简体中文）。

锁死对象：HA 通用「媒体」标签页的入口 ——
`media-source://musicflow/<entry>/<kind>/<value>` 的解析、浏览与单曲解析。
专辑/歌单只能展开，只有单曲能解析成 /rest/stream 直链。
"""

from __future__ import annotations

import types

import pytest
from homeassistant.components.media_source import BrowseError, Unresolvable
from homeassistant.core import HomeAssistant

from custom_components.musicflow import media_source as ms
from custom_components.musicflow.api import MusicFlowError
from custom_components.musicflow.const import DOMAIN, MEDIA_URI_PREFIX
from custom_components.musicflow.media_source import (
    MusicFlowMediaSource,
    async_get_media_source,
)


class Item:
    """最小可用的 MediaSourceItem 替身（被测代码只读 identifier）。"""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier


@pytest.fixture
def source(hass: HomeAssistant):
    return MusicFlowMediaSource(hass)


@pytest.fixture
def loaded(hass: HomeAssistant, fake_client):
    """把一个（假）已加载服务器放进 hass.data，返回 (source, client)。"""
    hass.data.setdefault(DOMAIN, {})["entry1"] = types.SimpleNamespace(
        client=fake_client, entry=types.SimpleNamespace(title="客厅服务器")
    )
    return MusicFlowMediaSource(hass), fake_client


def test_split_identifier():
    assert MusicFlowMediaSource._split("e1/album/123") == ("e1", "album", "123")
    assert MusicFlowMediaSource._split("e1") == ("e1", "", "")
    assert MusicFlowMediaSource._split("") == ("", "", "")
    # value 可能含 `/`（流派名）或百分号编码
    assert MusicFlowMediaSource._split("e1/genre/Rock/Pop") == ("e1", "genre", "Rock/Pop")
    assert MusicFlowMediaSource._split("e1/genre/Rock%2FPop") == ("e1", "genre", "Rock/Pop")


async def test_async_get_media_source_returns_instance(hass):
    src = await async_get_media_source(hass)
    assert isinstance(src, MusicFlowMediaSource)
    # MediaSource 的 domain 必须是集成 domain，HA 才能按 musicflow:// 路由
    assert src.domain == DOMAIN
    assert src.name == "MusicFlow"


# ==================== 解析（播放）====================
async def test_resolve_media_only_accepts_song(loaded):
    source, _client = loaded
    for identifier in ("entry1", "entry1/album/1", "entry1/song/", "other/song/1"):
        with pytest.raises(Unresolvable):
            await source.async_resolve_media(Item(identifier))


async def test_resolve_media_missing_server(loaded):
    source, _client = loaded
    with pytest.raises(Unresolvable, match="未加载"):
        await source.async_resolve_media(Item("nope/song/1"))


async def test_resolve_media_uses_content_type_then_suffix(loaded):
    source, client = loaded

    client.responses["async_get_song"] = {"contentType": "audio/flac", "suffix": "mp3"}
    out = await source.async_resolve_media(Item("entry1/song/9"))
    assert out.url == "http://mf.local:46400/rest/stream?id=9&token=fake-jwt"
    assert out.mime_type == "audio/flac"

    # 没有 contentType 时按后缀推断
    client.responses["async_get_song"] = {"suffix": "FLAC"}
    out = await source.async_resolve_media(Item("entry1/song/9"))
    assert out.mime_type == "audio/flac"

    # 未知后缀兜底 audio/mpeg
    client.responses["async_get_song"] = {"suffix": "xyz"}
    out = await source.async_resolve_media(Item("entry1/song/9"))
    assert out.mime_type == "audio/mpeg"

    client.responses["async_get_song"] = {}
    out = await source.async_resolve_media(Item("entry1/song/9"))
    assert out.mime_type == "audio/mpeg"


async def test_resolve_media_error_becomes_unresolvable(loaded):
    source, client = loaded
    client.raises["async_get_song"] = MusicFlowError("boom")
    with pytest.raises(Unresolvable, match="读取曲目信息失败"):
        await source.async_resolve_media(Item("entry1/song/1"))


async def test_mime_by_suffix_table():
    assert ms._MIME_BY_SUFFIX["mp3"] == "audio/mpeg"
    assert ms._MIME_BY_SUFFIX["alac"] == "audio/mp4"
    assert ms._MIME_BY_SUFFIX["wma"] == "audio/x-ms-wma"


# ==================== 浏览 ====================
async def test_browse_without_any_server_raises(hass):
    source = MusicFlowMediaSource(hass)
    with pytest.raises(BrowseError, match="还没有配置"):
        await source.async_browse_media(Item(""))


async def test_browse_root_single_vs_multiple_servers(loaded, hass):
    source, _client = loaded
    # 只有一台服务器：直接进分类，不让用户多点一层
    node = await source.async_browse_media(Item(""))
    assert node.title == "MusicFlow"
    assert [c.identifier for c in node.children] == [
        "entry1/playlists",
        "entry1/albums",
        "entry1/artists",
        "entry1/genres",
    ]

    # 多台服务器：先列服务器
    hass.data[DOMAIN]["entry2"] = types.SimpleNamespace(
        client=_client, entry=types.SimpleNamespace(title="卧室")
    )
    node = await source.async_browse_media(Item(""))
    assert node.title == "MusicFlow"
    assert sorted(c.title for c in node.children) == ["卧室", "客厅服务器"]
    assert sorted(c.identifier for c in node.children) == ["entry1", "entry2"]


async def test_browse_unknown_entry_and_kind(loaded):
    source, _client = loaded
    with pytest.raises(BrowseError, match="未加载"):
        await source.async_browse_media(Item("nope"))
    with pytest.raises(BrowseError, match="无法浏览"):
        await source.async_browse_media(Item("entry1/whatever/x"))


async def test_browse_categories_direct(loaded):
    source, _client = loaded
    node = await source.async_browse_media(Item("entry1"))
    assert [c.identifier for c in node.children] == [
        "entry1/playlists",
        "entry1/albums",
        "entry1/artists",
        "entry1/genres",
    ]


async def test_browse_playlists_and_playlist(loaded):
    source, client = loaded
    client.responses["async_get_playlists"] = {
        "playlists": {"playlist": [{"id": "p1", "name": "行车", "coverArt": "c1"}, {"name": "无 id"}]}
    }
    node = await source.async_browse_media(Item("entry1/playlists"))
    assert node.identifier == "entry1/playlists"
    assert [c.identifier for c in node.children] == ["entry1/playlist/p1"]
    assert node.children[0].thumbnail == "http://mf.local:46400/rest/getCoverArt?id=c1&size=400"

    client.responses["async_get_playlist"] = {
        "playlist": {"name": "夜", "entry": [{"id": "s1", "title": "曲"}]}
    }
    node = await source.async_browse_media(Item("entry1/playlist/p1"))
    assert node.title == "夜"
    assert node.children_media_class.value == "track"
    assert node.children[0].identifier == "entry1/song/s1"
    assert node.children[0].can_play is True
    assert node.children[0].can_expand is False


async def test_browse_albums_and_album(loaded):
    source, client = loaded
    client.responses["async_get_album_list"] = {
        "albumList": {"album": [{"id": "a1", "name": "唱片", "artist": "甲"}, {"name": "无 id"}]}
    }
    node = await source.async_browse_media(Item("entry1/albums"))
    assert [c.identifier for c in node.children] == ["entry1/album/a1"]
    assert node.children[0].title == "甲 - 唱片"
    assert client.called("async_get_album_list")[0][1] == ("newest", 300)

    client.responses["async_get_album"] = {
        "album": {"name": "唱片", "artist": "甲", "song": [{"id": "s1"}]}
    }
    node = await source.async_browse_media(Item("entry1/album/a1"))
    assert node.title == "甲 - 唱片"
    assert [c.identifier for c in node.children] == ["entry1/song/s1"]

    # 无 artist 的专辑：标题退化成专辑名
    client.responses["async_get_album"] = {"album": {"name": "独名"}}
    node = await source.async_browse_media(Item("entry1/album/a1"))
    assert node.title == "独名"


async def test_browse_artists_and_artist(loaded):
    source, client = loaded
    client.responses["async_get_artists"] = {
        "artists": {"index": [{"artist": [{"id": "ar1", "name": "甲"}, {"name": "无 id"}]}]}
    }
    node = await source.async_browse_media(Item("entry1/artists"))
    assert [c.identifier for c in node.children] == ["entry1/artist/ar1"]

    client.responses["async_get_artist"] = {
        "artist": {"name": "甲", "album": [{"id": "a1", "name": "专"}, {"name": "无 id"}]}
    }
    node = await source.async_browse_media(Item("entry1/artist/ar1"))
    assert node.title == "甲"
    assert [c.identifier for c in node.children] == ["entry1/album/a1"]


async def test_browse_genres_and_genre(loaded):
    source, client = loaded
    client.responses["async_get_genres"] = {
        "genres": {"genre": [{"value": "Rock", "songCount": 4}, {"name": "Jazz"}, {"songCount": 1}]}
    }
    node = await source.async_browse_media(Item("entry1/genres"))
    assert [c.title for c in node.children] == ["Rock (4)", "Jazz (0)"]
    assert [c.identifier for c in node.children] == [
        "entry1/genre/Rock",
        "entry1/genre/Jazz",
    ]

    client.responses["async_get_songs_by_genre"] = {"songsByGenre": {"song": [{"id": "s1"}]}}
    node = await source.async_browse_media(Item("entry1/genre/Rock"))
    assert node.title == "Rock"
    assert [c.identifier for c in node.children] == ["entry1/song/s1"]
    assert client.called("async_get_songs_by_genre")[0][1] == ("Rock", 300)


async def test_browse_musicflow_error_becomes_browse_error(loaded):
    source, client = loaded
    client.raises["async_get_playlists"] = MusicFlowError("boom")
    with pytest.raises(BrowseError, match="浏览 MusicFlow 曲库失败"):
        await source.async_browse_media(Item("entry1/playlists"))


# ==================== 搜索 ====================
async def test_search_media(loaded):
    source, client = loaded
    client.responses["async_search"] = {"song": [{"id": "s1", "title": "曲"}]}
    client.responses["async_get_playlists"] = {}
    out = await source.async_search_media(Item("entry1"), _query("曲"))
    # SearchMedia.result 是 HA 的 BrowseMedia（字段名 media_content_id，无 identifier），
    # 且内容 id 一律带 MEDIA_URI_PREFIX（"musicflow://"）前缀
    assert [c.media_content_id for c in out.result] == [f"{MEDIA_URI_PREFIX}song/s1"]
    assert client.called("async_search")[0][1] == ("曲", 50)


async def test_search_media_unknown_server(loaded):
    source, _client = loaded
    with pytest.raises(Unresolvable):
        await source.async_search_media(Item("nope"), _query("甲"))


async def test_search_media_error_becomes_browse_error(loaded):
    source, client = loaded
    client.raises["async_search"] = MusicFlowError("boom")
    with pytest.raises(BrowseError, match="搜索 MusicFlow 曲库失败"):
        await source.async_search_media(Item("entry1"), _query("甲"))


def _query(text: str):
    from homeassistant.components.media_player import SearchMediaQuery

    return SearchMediaQuery(search_query=text)
