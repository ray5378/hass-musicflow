"""const.py 常量契约 + manifest.json 元数据守卫（简体中文）。

常量是与后端 / 卡片 / 客户端的**跨端契约**：改一个值就可能让集成再也连不上
（路径前缀）或静默退化（限流 / 超时）。这里的用例把契约钉死，改了就必须
一起改用例 —— 逼一次「有意为之」的确认。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.musicflow import const

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "custom_components" / "musicflow" / "manifest.json"


def test_domain_and_config_entry_keys():
    assert const.DOMAIN == "musicflow"
    assert const.CONF_URL == "url"
    assert const.CONF_USERNAME == "username"
    assert const.CONF_PASSWORD == "password"
    assert const.CONF_VERIFY_SSL == "verify_ssl"
    assert const.DEFAULT_PORT == 46400


def test_backend_path_prefixes():
    # 后端 index.ts 的挂载点：/rest/api/v1(内部 REST) / /rest(OpenSubsonic) / /ws
    assert const.API_PREFIX == "/rest/api/v1"
    assert const.SUBSONIC_PREFIX == "/rest"
    assert const.WS_PATH == "/ws"


def test_runtime_parameters():
    assert const.REQUEST_TIMEOUT == 15
    assert const.WS_HEARTBEAT == 30
    assert const.WS_RECONNECT_MIN == 5
    assert const.WS_RECONNECT_MAX == 120
    assert const.POLL_INTERVAL_SECONDS == 30
    assert const.BROWSE_LIMIT == 300
    assert const.TRANSFER_SEEK_DELAY == 1.2


def test_zeroconf_type_matches_manifest():
    assert const.ZEROCONF_TYPE == "_musicflow._tcp.local."


def test_media_uri_and_playable_types():
    assert const.MEDIA_URI_PREFIX == "musicflow://"
    assert const.PLAYABLE_TYPES == ("song", "album", "playlist", "artist", "genre")


def test_controllable_kinds():
    assert const.PEER_KIND_LOCAL == "local"
    assert const.PEER_KIND_DLNA == "dlna"
    assert const.PEER_KIND_GROUP == "group"
    assert const.PEER_KIND_AIRPLAY == "airplay"
    assert const.PEER_KIND_SENDSPIN == "sendspin"
    # 只有这四种由后端驱动音频，local 不在其中（浏览器本地播放 HA 控不了）
    assert const.CONTROLLABLE_KINDS == ("dlna", "group", "airplay", "sendspin")
    assert const.PEER_KIND_LOCAL not in const.CONTROLLABLE_KINDS


def test_play_modes():
    assert const.PLAY_MODES == ("order", "one", "all", "shuffle")


def test_service_and_attribute_names():
    assert const.SERVICE_PLAY_CONTENT == "play_content"
    assert const.SERVICE_SET_PLAY_MODE == "set_play_mode"
    assert const.SERVICE_CLEAR_QUEUE == "clear_queue"
    assert const.SERVICE_LIKE_TRACK == "like_track"
    assert const.SERVICE_ADD_TO_PLAYLIST == "add_to_playlist"
    assert {
        const.ATTR_CONTENT_TYPE,
        const.ATTR_CONTENT_ID,
        const.ATTR_SONG_ID,
        const.ATTR_START_INDEX,
        const.ATTR_PLAY_MODE,
        const.ATTR_ENQUEUE,
        const.ATTR_LIKED,
        const.ATTR_PLAYLIST_ID,
        const.ATTR_SONG_IDS,
    } == {
        "content_type",
        "content_id",
        "song_id",
        "start_index",
        "play_mode",
        "enqueue",
        "liked",
        "playlist_id",
        "song_ids",
    }


@pytest.mark.parametrize(
    "value",
    [
        const.PLAYABLE_TYPES,
        const.CONTROLLABLE_KINDS,
        const.PLAY_MODES,
    ],
)
def test_tuple_constants_are_immutable(value):
    """元组语义：不可变（防止误改成 list 后被就地修改）且成员为字符串。"""
    assert isinstance(value, tuple)
    assert all(isinstance(item, str) for item in value)
    with pytest.raises(TypeError):
        value[0] = "mutated"  # type: ignore[index]


def test_manifest_matches_constants():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["domain"] == const.DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "local_push"
    # 与后端 index.ts 挂载的依赖；缺 http 则代理视图注册不了
    assert set(manifest["dependencies"]) == {"http", "media_source", "websocket_api"}
    assert const.ZEROCONF_TYPE in manifest["zeroconf"]
    # 集成不引三方依赖（HTTP/WS 都走 HA 自带 aiohttp）
    assert manifest["requirements"] == []
