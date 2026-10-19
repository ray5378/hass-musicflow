"""browse_media.py 单测（简体中文）。

锁死对象：OpenSubsonic 响应 → HA BrowseMedia 树的映射，以及 media_content_id
编码方案（`musicflow://<kind>/<value>`）。用假客户端喂各种"现实里会出现的脏数据"
（单条不是数组、缺字段、嵌套 index 等），确保不会崩也不会错位。
"""

from __future__ import annotations

import pytest
from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaType,
)

from custom_components.musicflow import browse_media as bm
from custom_components.musicflow.browse_media import (
    ROOT_ID,
    build_browse_media,
    build_search_results,
    parse_media_id,
)

try:
    from homeassistant.components.media_player import SearchMedia
except ImportError:  # pragma: no cover
    SearchMedia = None


def fake_thumb(content_type, content_id, cover_art):
    """可断言的假封面回调。"""
    if not cover_art:
        return None
    return f"thumb:{content_type}:{content_id}:{cover_art}"


# ==================== parse_media_id ====================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("musicflow://album/123", ("album", "123")),
        ("musicflow://playlist/pl-1", ("playlist", "pl-1")),
        ("musicflow://", ("", "")),
        ("musicflow://artists", ("artists", "")),
        # 流派名含 `/`：只切第一个斜杠并整体 URL 解码
        ("musicflow://genre/Rock/Pop", ("genre", "Rock/Pop")),
        ("musicflow://genre/Rock%2FPop", ("genre", "Rock/Pop")),
        ("musicflow://genre/Rock%20%26%20Roll", ("genre", "Rock & Roll")),
        # 没有前缀时 removeprefix 是恒等操作
        ("album/9", ("album", "9")),
    ],
)
def test_parse_media_id(raw, expected):
    assert parse_media_id(raw) == expected


def test_root_id_is_media_uri_prefix():
    assert ROOT_ID == "musicflow://"


# ==================== 根菜单 ====================
async def test_root_menu_for_none_and_root_id(make_client):
    client = make_client()
    for content_id in (None, "", ROOT_ID):
        node = await build_browse_media(client, content_id)
        assert isinstance(node, BrowseMedia)
        assert node.title == "MusicFlow"
        assert node.media_content_id == ROOT_ID
        assert node.media_class == MediaClass.DIRECTORY
        assert node.can_expand is True
        assert node.can_play is False
        assert node.can_search is True
        assert [c.title for c in node.children] == ["歌单", "专辑", "艺术家", "流派"]
        assert [c.media_content_id for c in node.children] == [
            "musicflow://playlists",
            "musicflow://albums",
            "musicflow://artists",
            "musicflow://genres",
        ]
        assert [c.media_class for c in node.children] == [
            MediaClass.PLAYLIST,
            MediaClass.ALBUM,
            MediaClass.ARTIST,
            MediaClass.GENRE,
        ]


# ==================== 歌单 ====================
async def test_browse_playlists(make_client):
    client = make_client(
        {"async_get_playlists": {"playlists": {"playlist": [
            {"id": "p1", "name": "行车", "coverArt": "c1"},
            {"id": None, "name": "无 id 应被丢弃"},
            {"name": "也没 id"},
        ]}}}
    )
    node = await build_browse_media(client, "musicflow://playlists", thumb=fake_thumb)
    assert node.title == "歌单"
    assert node.media_content_id == "musicflow://playlists"
    assert node.can_play is False
    assert [c.title for c in node.children] == ["行车"]
    assert node.children[0].media_content_id == "musicflow://playlist/p1"
    assert node.children[0].thumbnail == "thumb:playlist:musicflow://playlist/p1:c1"
    assert node.children[0].can_expand is True


async def test_browse_playlists_single_object_and_empty(make_client):
    # OpenSubsonic 单条结果可能是对象而非数组；空结果也不能炸
    client = make_client(
        {"async_get_playlists": {"playlists": {"playlist": {"id": "p1", "name": "收藏"}}}}
    )
    node = await build_browse_media(client, "musicflow://playlists", thumb=fake_thumb)
    assert [c.title for c in node.children] == ["收藏"]

    client = make_client({"async_get_playlists": {}})
    node = await build_browse_media(client, "musicflow://playlists", thumb=fake_thumb)
    assert node.children == []


async def test_browse_playlist_tracks(make_client):
    client = make_client(
        {
            "async_get_playlist": {
                "playlist": {
                    "name": "夜晚安静",
                    "coverArt": "cov",
                    "entry": [{"id": "s1", "title": "第一首"}, {"title": "无 id 歌"}],
                }
            }
        }
    )
    node = await build_browse_media(client, "musicflow://playlist/p1", thumb=fake_thumb)
    assert node.title == "夜晚安静"
    assert node.can_play is True
    assert node.children_media_class == MediaClass.TRACK
    assert node.thumbnail == "thumb:playlist:musicflow://playlist/p1:cov"
    assert [c.media_content_id for c in node.children] == [
        "musicflow://song/s1",
        "musicflow://song/",
    ]


async def test_browse_playlist_without_name_uses_fallback(make_client):
    client = make_client({"async_get_playlist": {"playlist": {}}})
    node = await build_browse_media(client, "musicflow://playlist/x", thumb=fake_thumb)
    assert node.title == "歌单"
    assert node.media_content_id == "musicflow://playlist/x"
    assert node.children == []


# ==================== 专辑 ====================
async def test_browse_albums_uses_browse_limit(make_client):
    client = make_client(
        {"async_get_album_list": {"albumList": {"album": [
            {"id": "a1", "name": "同名专辑", "artist": "歌手", "coverArt": "c"},
            {"name": "无 id"},
        ]}}}
    )
    node = await build_browse_media(client, "musicflow://albums", thumb=fake_thumb)
    assert node.title == "专辑"
    assert node.children_media_class == MediaClass.ALBUM
    assert [c.title for c in node.children] == ["歌手 - 同名专辑"]
    assert node.children[0].media_content_id == "musicflow://album/a1"
    # 列表请求必须带 BROWSE_LIMIT
    assert client.called("async_get_album_list")[0][1] == ("newest", 300)


async def test_browse_album_detail(make_client):
    client = make_client(
        {"async_get_album": {"album": {
            "name": "专辑名",
            "artist": "艺术家",
            "coverArt": "cv",
            "song": [{"id": "s1", "title": "曲 1", "coverArt": "sc"}],
        }}}
    )
    node = await build_browse_media(client, "musicflow://album/a1", thumb=fake_thumb)
    assert node.title == "艺术家 - 专辑名"
    assert node.can_play is True
    assert node.children_media_class == MediaClass.TRACK
    assert node.children[0].thumbnail == "thumb:track:musicflow://song/s1:sc"

    # 无 artist 时标题退化成专辑名
    client = make_client({"async_get_album": {"album": {"name": "只有名字"}}})
    node = await build_browse_media(client, "musicflow://album/a1", thumb=fake_thumb)
    assert node.title == "只有名字"
    assert node.thumbnail is None


# ==================== 艺术家 ====================
async def test_browse_artists_nested_index(make_client):
    client = make_client(
        {"async_get_artists": {"artists": {"index": [
            {"artist": [{"id": "ar1", "name": "甲"}, {"name": "无 id"}]},
            {"artist": {"id": "ar2", "name": "乙"}},
            {"artist": None},
        ]}}}
    )
    node = await build_browse_media(client, "musicflow://artists", thumb=fake_thumb)
    assert node.title == "艺术家"
    assert [c.title for c in node.children] == ["甲", "乙"]
    assert node.children[0].media_content_id == "musicflow://artist/ar1"


async def test_browse_artist_albums(make_client):
    client = make_client(
        {"async_get_artist": {"artist": {
            "name": "甲",
            "coverArt": "ac",
            "album": [{"id": "a1", "name": "唱片", "artist": "甲"}, {"name": "无 id"}],
        }}}
    )
    node = await build_browse_media(client, "musicflow://artist/ar1", thumb=fake_thumb)
    assert node.title == "甲"
    assert node.can_play is True
    assert node.children_media_class == MediaClass.ALBUM
    assert [c.media_content_id for c in node.children] == ["musicflow://album/a1"]

    client = make_client({"async_get_artist": {"artist": {}}})
    node = await build_browse_media(client, "musicflow://artist/x", thumb=fake_thumb)
    assert node.title == "艺术家"


# ==================== 流派 ====================
async def test_browse_genres_value_or_name(make_client):
    client = make_client(
        {"async_get_genres": {"genres": {"genre": [
            {"value": "Rock", "songCount": 12},
            {"name": "Jazz"},
            {"value": "", "name": ""},  # 无名字丢弃
            {"songCount": 3},
        ]}}}
    )
    node = await build_browse_media(client, "musicflow://genres", thumb=fake_thumb)
    assert node.title == "流派"
    assert [c.title for c in node.children] == ["Rock (12)", "Jazz (0)"]
    assert [c.media_content_id for c in node.children] == [
        "musicflow://genre/Rock",
        "musicflow://genre/Jazz",
    ]
    assert node.children[0].can_play is True


async def test_browse_genre_with_slash_is_encoded(make_client):
    client = make_client(
        {"async_get_genres": {"genres": {"genre": [{"value": "Rock/Pop"}]}}}
    )
    node = await build_browse_media(client, "musicflow://genres", thumb=fake_thumb)
    assert node.children[0].media_content_id == "musicflow://genre/Rock%2FPop"


async def test_browse_genre_songs(make_client):
    client = make_client(
        {"async_get_songs_by_genre": {"songsByGenre": {"song": [
            {"id": "s1", "title": "曲"},
        ]}}}
    )
    node = await build_browse_media(client, "musicflow://genre/Rock", thumb=fake_thumb)
    assert node.title == "Rock"
    assert node.media_content_id == "musicflow://genre/Rock"
    assert node.can_play is True
    assert [c.media_content_id for c in node.children] == ["musicflow://song/s1"]
    assert client.called("async_get_songs_by_genre")[0][1] == ("Rock", 300)


# ==================== 兜底封面策略 ====================
async def test_default_thumb_uses_client_cover_url(make_client):
    client = make_client(
        {"async_get_playlists": {"playlists": {"playlist": [{"id": "p1", "coverArt": "c1"}]}}}
    )
    node = await build_browse_media(client, "musicflow://playlists")  # 不传 thumb
    assert node.children[0].thumbnail == (
        "http://mf.local:46400/rest/getCoverArt?id=c1&size=400"
    )


# ==================== 不支持的 id ====================
@pytest.mark.parametrize(
    "content_id",
    [
        "musicflow://song/s1",  # song 不是可浏览目录（叶子直接播）
        "musicflow://unknown",
        "musicflow://album/",  # 值为空
        "musicflow://playlist/",
        "musicflow://genre/",
        "musicflow://artist/",
        "musicflow://search/abc",
    ],
)
async def test_unsupported_content_id_raises_browse_error(make_client, content_id):
    client = make_client()
    with pytest.raises(BrowseError):
        await build_browse_media(client, content_id, thumb=fake_thumb)


# ==================== 搜索 ====================
def _search_payload():
    return {
        "searchResult3": {
            "album": [{"id": "a1", "name": "专辑", "artist": "甲"}, {"name": "无 id"}],
            "artist": [{"id": "ar1", "name": "甲"}],
            "song": [{"id": "s1", "title": "曲"}, {"title": "无 id"}],
        }
    }


async def test_search_results_merges_all_kinds(make_client):
    client = make_client(
        {
            "async_search": _search_payload(),
            "async_get_playlists": {"playlists": {"playlist": [
                {"id": "p1", "name": "甲的歌单"},
                {"id": "p2", "name": "无关"},
                {"name": "无 id"},
            ]}},
        }
    )
    out = await build_search_results(client, "甲", 30, thumb=fake_thumb)
    # 专辑 / 艺术家 / 歌单 / 歌曲，按顺序拼接
    if SearchMedia is not None:
        assert isinstance(out, SearchMedia)
        children = out.result
    else:  # pragma: no cover
        children = out.children
    assert [c.media_content_id for c in children] == [
        "musicflow://album/a1",
        "musicflow://artist/ar1",
        "musicflow://playlist/p1",
        "musicflow://song/s1",
    ]
    # limit 透传给 search3
    assert client.called("async_search")[0][1] == ("甲", 30)


async def test_search_results_empty_placeholder(make_client):
    client = make_client({"async_search": {}, "async_get_playlists": {}})
    out = await build_search_results(client, "查不到", thumb=fake_thumb)
    children = out.result if SearchMedia is not None else out.children
    assert len(children) == 1
    assert children[0].title == "没有找到匹配的结果"
    assert children[0].media_content_id == "musicflow://search/empty"
    assert children[0].can_expand is False


async def test_search_results_without_search_result3_key(make_client):
    # 有些后端直接返回扁平结构（没有 searchResult3 包装）
    client = make_client(
        {
            "async_search": {"song": [{"id": "s1", "title": "曲"}]},
            "async_get_playlists": {"playlists": {"playlist": []}},
        }
    )
    out = await build_search_results(client, "曲", 5, thumb=fake_thumb)
    children = out.result if SearchMedia is not None else out.children
    assert [c.media_content_id for c in children] == ["musicflow://song/s1"]


async def test_search_results_empty_query_skips_playlists(make_client):
    client = make_client(
        {
            "async_search": {"song": [{"id": "s1", "title": "曲"}]},
            "async_get_playlists": {"playlists": {"playlist": [{"id": "p1", "name": "任意"}]}},
        }
    )
    out = await build_search_results(client, "   ", 5, thumb=fake_thumb)
    children = out.result if SearchMedia is not None else out.children
    # 查询串为空 → 歌单不做子串匹配（否则会把所有歌单都塞进来）
    assert [c.media_content_id for c in children] == ["musicflow://song/s1"]


async def test_search_default_thumb(make_client):
    client = make_client(
        {
            "async_search": {"song": [{"id": "s1", "title": "曲", "coverArt": "cover"}]},
            "async_get_playlists": {},
        }
    )
    out = await build_search_results(client, "曲")
    children = out.result if SearchMedia is not None else out.children
    assert children[0].thumbnail == (
        "http://mf.local:46400/rest/getCoverArt?id=cover&size=400"
    )


def test_as_list_and_join_helpers():
    assert bm._as_list([{"a": 1}, "x", None]) == [{"a": 1}]
    assert bm._as_list({"a": 1}) == [{"a": 1}]
    assert bm._as_list(None) == []
    assert bm._as_list("x") == []
    assert bm._join("甲", "乙") == "甲 - 乙"
    assert bm._join(None, "乙") == "乙"
    assert bm._join("", "乙") == "乙"


def test_search_media_type_hint():
    """media_content_type 用的是 HA 的 MediaType 枚举，避免手写字符串漂移。"""
    assert MediaType.PLAYLIST == "playlist"
    assert MediaType.TRACK == "track"
