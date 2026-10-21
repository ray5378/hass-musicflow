"""media_player.py 单测（简体中文）。

锁死对象：一个 MusicFlow peer 对应的 HA 播放器实体 ——
状态映射、音量/静音、进度时间轴、队列/曲目展示、传输控制（含 seek 乐观保护
与「入组后转发到组」语义）、播放转移、内容点播、媒体浏览与搜索，
以及 async_setup_entry 的实体增删剪枝与自定义服务注册。

策略：绝大多数用例直接构造实体 + 真实协调器 + 假客户端（写法轻、跑得快）；
另有一条完整 async_setup_entry 集成用例覆盖实体剪枝与服务注册。
"""

from __future__ import annotations

import pytest
from homeassistant.components.media_player import (
    MediaPlayerEnqueue,
    MediaPlayerState,
    RepeatMode,
    SearchMediaQuery,
)
from homeassistant.exceptions import HomeAssistantError

from custom_components.musicflow import media_player as mp_mod
from custom_components.musicflow.api import MusicFlowError
from custom_components.musicflow.const import DOMAIN, MEDIA_URI_PREFIX
from custom_components.musicflow.coordinator import PeerState
from custom_components.musicflow.media_player import (
    PLAY_MODE_TO_REPEAT,
    STATE_MAP,
    SUPPORTED_FEATURES,
    MusicFlowMediaPlayer,
    async_setup_entry,
)


# ==================== 辅助 ====================
def add_peer(
    coordinator,
    peer_id,
    kind,
    *,
    name=None,
    available=True,
    platform=None,
    queue=None,
    status=None,
):
    state = PeerState(peer_id, kind, name or peer_id, platform=platform, available=available)
    if queue is not None:
        state.queue = queue
    if status is not None:
        state.status = status
    coordinator.peers[peer_id] = state
    coordinator.last_update_success = True
    # 控制类方法末尾 `_call` 会请求一次状态对齐刷新，而 _async_update_data 以
    # /v1/peers 为准剪枝。把本用例注入的 peer 同步镜像进假客户端的 peers 列表，
    # 刷新时才不会被当成「后端已移除」剪掉（async_get_peers 默认返回 []）。
    records = coordinator.client.responses.setdefault("async_get_peers", [])
    record = next(
        (r for r in records if isinstance(r, dict) and r.get("peerId") == peer_id), None
    )
    if record is None:
        record = {"peerId": peer_id}
        records.append(record)
    record["kind"] = kind
    record["name"] = name or peer_id
    record["available"] = available
    if platform is not None:
        record["platform"] = platform
    if queue is not None:
        record["queue"] = queue
    return state


@pytest.fixture
def player(coordinator, mock_entry, monkeypatch):
    """直接构造的实体；async_write_ha_state 打成空操作，免去平台注册。"""
    entity = MusicFlowMediaPlayer(coordinator, mock_entry, "dlna:d1")
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    return entity


# ==================== 状态映射 ====================
def test_state_map_table():
    assert STATE_MAP["PLAYING"] is MediaPlayerState.PLAYING
    assert STATE_MAP["PAUSED_PLAYBACK"] is MediaPlayerState.PAUSED
    assert STATE_MAP["PAUSED_RECORDING"] is MediaPlayerState.PAUSED
    assert STATE_MAP["TRANSITIONING"] is MediaPlayerState.BUFFERING
    assert STATE_MAP["STOPPED"] is MediaPlayerState.IDLE
    assert STATE_MAP["NO_MEDIA_PRESENT"] is MediaPlayerState.IDLE
    assert STATE_MAP["PAUSED"] is MediaPlayerState.PAUSED
    assert STATE_MAP["BUFFERING"] is MediaPlayerState.BUFFERING
    assert STATE_MAP["IDLE"] is MediaPlayerState.IDLE
    assert PLAY_MODE_TO_REPEAT["one"] is RepeatMode.ONE
    assert PLAY_MODE_TO_REPEAT["all"] is RepeatMode.ALL
    assert PLAY_MODE_TO_REPEAT["shuffle"] is RepeatMode.OFF


def test_supported_features_include_core():
    from homeassistant.components.media_player import MediaPlayerEntityFeature as F

    for flag in (F.PLAY, F.PAUSE, F.STOP, F.NEXT_TRACK, F.SEEK, F.VOLUME_SET, F.PLAY_MEDIA):
        assert SUPPORTED_FEATURES & flag


def test_state_variants(player, coordinator):
    # 无 peer → OFF
    assert player.state is MediaPlayerState.OFF
    add_peer(coordinator, "dlna:d1", "dlna", available=False, status={"state": "PLAYING"})
    assert player.state is MediaPlayerState.OFF  # 设备离线
    add_peer(coordinator, "dlna:d1", "dlna", status={"state": "playing"})
    assert player.state is MediaPlayerState.PLAYING
    # 软关机
    player._soft_off = True
    assert player.state is MediaPlayerState.OFF
    # 设备又出声 → 软关机自动解除
    player._soft_off = True
    player._handle_coordinator_update()
    assert player._soft_off is False
    # 未知状态但队列活跃 → PLAYING
    add_peer(coordinator, "dlna:d1", "dlna", status={"state": "WHAT"}, queue={"isActive": True})
    assert player.state is MediaPlayerState.PLAYING
    add_peer(coordinator, "dlna:d1", "dlna", status={}, queue={})
    assert player.state is MediaPlayerState.IDLE


def test_available(player, coordinator):
    assert player.available is False
    add_peer(coordinator, "dlna:d1", "dlna")
    assert player.available is True
    coordinator.last_update_success = False
    assert player.available is False


# ==================== 音量 / 静音 / 进度 ====================
def test_volume_level(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", status={})
    assert player.volume_level is None
    add_peer(coordinator, "dlna:d1", "dlna", status={"volume": 50})
    assert player.volume_level == 0.5
    add_peer(coordinator, "dlna:d1", "dlna", status={"volume": 150})
    assert player.volume_level == 1.0
    add_peer(coordinator, "dlna:d1", "dlna", status={"volume": -5})
    assert player.volume_level == 0.0


def test_is_volume_muted(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", status={})
    assert player.is_volume_muted is None
    add_peer(coordinator, "dlna:d1", "dlna", status={"muted": True})
    assert player.is_volume_muted is True
    add_peer(coordinator, "dlna:d1", "dlna", status={"muted": 0})
    assert player.is_volume_muted is None


def test_media_position_and_anchor(player, coordinator):
    assert player.media_position is None
    state = add_peer(coordinator, "dlna:d1", "dlna", status={"position": 12.34})
    assert player.media_position == 12.3
    assert player.media_position_updated_at == state.status_updated_at


def test_media_duration_from_status_or_item(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", status={"duration": 200})
    assert player.media_duration == 200
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        status={"duration": 0},
        queue={"items": [{"duration": 180}], "currentIndex": 0},
    )
    assert player.media_duration == 180
    add_peer(coordinator, "dlna:d1", "dlna", status={}, queue={})
    assert player.media_duration is None


def test_current_item_and_metadata(player, coordinator):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={
            "items": [
                {
                    "title": "歌",
                    "artist": "甲",
                    "album": "专",
                    "albumArtist": "合辑",
                    "track": 3,
                    "songId": "s1",
                    "coverArt": "c1",
                }
            ],
            "currentIndex": 0,
        },
        status={},
    )
    assert player.media_title == "歌"
    assert player.media_artist == "甲"
    assert player.media_album_name == "专"
    assert player.media_album_artist == "合辑"
    assert player.media_track == 3
    assert player.media_content_id == f"{MEDIA_URI_PREFIX}song/s1"
    assert player.media_image_url == "http://mf.local:46400/rest/getCoverArt?id=c1&size=400"
    # 无当前项时各字段为 None
    add_peer(coordinator, "dlna:d1", "dlna", queue={}, status={})
    assert player.media_title is None
    assert player.media_album_artist is None
    assert player.media_track is None
    assert player.media_content_id is None
    assert player.media_image_url is None


def test_album_artist_falls_back_to_artist(player, coordinator):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"artist": "甲"}], "currentIndex": 0},
    )
    assert player.media_album_artist == "甲"
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"track": 0}], "currentIndex": 0},
    )
    assert player.media_track is None


def test_shuffle_and_repeat(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", queue={"playMode": "shuffle"})
    assert player.shuffle is True
    assert player.repeat is RepeatMode.OFF
    add_peer(coordinator, "dlna:d1", "dlna", queue={"playMode": "one"})
    assert player.shuffle is False
    assert player.repeat is RepeatMode.ONE
    add_peer(coordinator, "dlna:d1", "dlna", queue={})
    assert player.repeat is RepeatMode.OFF


def test_extra_state_attributes(player, coordinator):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        name="客厅",
        queue={"playMode": "all", "currentIndex": 2, "total": 10, "items": []},
        status={"media": {"songId": "s5"}},
    )
    coordinator._starred = {"s5"}
    attrs = player.extra_state_attributes
    assert attrs["peer_id"] == "dlna:d1"
    assert attrs["peer_kind"] == "dlna"
    assert attrs["play_mode"] == "all"
    assert attrs["queue_size"] == 10  # total 兜底
    assert attrs["queue_position"] == 2
    assert attrs["control_peer_id"] == "dlna:d1"
    assert attrs["song_id"] == "s5"
    assert attrs["liked"] is True


# ==================== 设备信息 ====================
def test_device_info_kinds(player, coordinator, mock_entry):
    add_peer(coordinator, "dlna:d1", "dlna", name="客厅")
    dlna = player.device_info
    assert dlna["model"] == "DLNA 渲染器"
    assert dlna["via_device"] == (DOMAIN, mock_entry.entry_id)

    add_peer(coordinator, "dlna:d1", "group", name="组")
    assert player.device_info["model"] == "播放器组"
    add_peer(coordinator, "dlna:d1", "airplay", name="AP")
    assert player.device_info["model"] == "AirPlay 播放器"
    add_peer(coordinator, "dlna:d1", "sendspin", name="SS")
    assert player.device_info["model"] == "Sendspin 播放器"
    add_peer(coordinator, "dlna:d1", "local", name="安卓端", platform="android")
    assert player.device_info["model"] == "MusicFlow 客户端 (android)"
    add_peer(coordinator, "dlna:d1", "local", name="客户端")
    assert player.device_info["model"] == "MusicFlow 客户端"


# ==================== 入组转发 ====================
def test_control_peer_id_forwards_to_group(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", name="甲")
    add_peer(coordinator, "group:g1", "group", name="组")
    coordinator._device_groups = {"d1": {"g1"}}
    assert player._control_peer_id == "group:g1"
    # 无归属组 → 仍是自己
    coordinator._device_groups = {}
    assert player._control_peer_id == "dlna:d1"
    # 组实体自己 → 自己
    player.peer_id = "group:g1"
    assert player._control_peer_id == "group:g1"
    # peer 不存在 → 自己
    player.peer_id = "dlna:nope"
    assert player._control_peer_id == "dlna:nope"


def test_queue_and_item_fall_back_to_group(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", queue={})
    add_peer(
        coordinator,
        "group:g1",
        "group",
        queue={"items": [{"title": "组曲", "songId": "s9"}], "currentIndex": 0},
    )
    coordinator._device_groups = {"d1": {"g1"}}
    assert player._queue["items"][0]["title"] == "组曲"
    assert player._current_item["songId"] == "s9"
    # 成员自己有队列 → 优先用自己的
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"title": "自己", "songId": "s1"}], "currentIndex": 0},
    )
    assert player._current_item["songId"] == "s1"
    # peer 不存在 → 空队列
    player.peer_id = "dlna:nope"
    assert player._queue == {}


# ==================== 传输控制 ====================
async def test_play_pause_stop_next_previous(player, coordinator, fake_client, hass):
    add_peer(coordinator, "dlna:d1", "dlna")
    player._soft_off = True
    await player.async_media_play()
    assert player._soft_off is False
    await player.async_media_pause()
    await player.async_media_stop()
    await player.async_media_next_track()
    await player.async_media_previous_track()
    for name in ("async_play", "async_pause", "async_stop", "async_next", "async_previous"):
        assert fake_client.called(name)[-1][1] == ("dlna:d1",)


async def test_control_error_translated(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    fake_client.raises["async_pause"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_media_pause()


async def test_seek_invalid_target_discarded(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_media_seek(float("nan"))
    assert fake_client.called("async_seek") == []


async def test_seek_playing_clamps_and_writes_optimistic(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", status={"state": "PLAYING", "duration": 200})
    await player.async_media_seek(65.7)
    peer = coordinator.peers["dlna:d1"]
    assert fake_client.called("async_seek")[-1][1] == ("dlna:d1", 65.0)
    assert peer.seek_target == 65.0
    assert peer.status["position"] == 65.0


async def test_seek_stopped_starts_playback_first(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", status={"state": "STOPPED", "duration": 100})
    fake_client.responses["async_play"] = None
    await player.async_media_seek(30.0)
    assert fake_client.called("async_play")  # 先起播
    assert fake_client.called("async_seek")[-1][1] == ("dlna:d1", 30.0)


async def test_seek_start_playback_error(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", status={"state": "STOPPED"})
    fake_client.raises["async_play"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_media_seek(30.0)


async def test_volume_methods(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", status={"volume": 50})
    await player.async_set_volume_level(0.3)
    assert fake_client.called("async_set_volume")[-1][1] == ("dlna:d1", 30)
    await player.async_volume_up()
    assert fake_client.called("async_set_volume")[-1][1] == ("dlna:d1", 55)
    await player.async_volume_down()
    assert fake_client.called("async_set_volume")[-1][1] == ("dlna:d1", 45)
    # 音量未知时按 0 起算
    add_peer(coordinator, "dlna:d1", "dlna", status={})
    await player.async_volume_up()
    assert fake_client.called("async_set_volume")[-1][1] == ("dlna:d1", 5)
    await player.async_mute_volume(True)
    assert fake_client.called("async_set_mute")[-1][1] == ("dlna:d1", True)


async def test_turn_off_and_on(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_turn_off()
    assert player._soft_off is True
    assert fake_client.called("async_stop")
    assert fake_client.called("async_clear_queue")
    await player.async_turn_on()
    assert player._soft_off is False


async def test_clear_playlist(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_clear_playlist()
    assert fake_client.called("async_clear_queue")[-1][1] == ("dlna:d1",)


async def test_like_track_no_song(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", queue={}, status={})
    with pytest.raises(HomeAssistantError):
        await player.async_like_track()


async def test_like_track_toggle_and_explicit(player, coordinator, fake_client):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"songId": "s1"}], "currentIndex": 0},
    )
    coordinator._starred = set()
    await player.async_like_track()
    assert fake_client.called("async_star")[-1][1] == (["s1"],)
    coordinator._starred = {"s1"}
    await player.async_like_track()  # 已收藏 → 取消
    assert fake_client.called("async_unstar")[-1][1] == (["s1"],)
    await player.async_like_track(True)  # 显式喜欢
    assert fake_client.called("async_star")[-1][1] == (["s1"],)


async def test_like_track_error(player, coordinator, fake_client):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"songId": "s1"}], "currentIndex": 0},
    )
    fake_client.raises["async_star"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_like_track(True)


async def test_add_to_playlist(player, coordinator, fake_client):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"songId": "s1"}], "currentIndex": 0},
    )
    await player.async_add_to_playlist("p1")
    assert fake_client.called("async_add_to_playlist")[-1][1] == ("p1", ["s1"])
    await player.async_add_to_playlist("p1", ["s2", "s3"])
    assert fake_client.called("async_add_to_playlist")[-1][1] == ("p1", ["s2", "s3"])


async def test_add_to_playlist_errors(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", queue={}, status={})
    with pytest.raises(HomeAssistantError):
        await player.async_add_to_playlist("p1")
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"songId": "s1"}], "currentIndex": 0},
    )
    fake_client.raises["async_add_to_playlist"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_add_to_playlist("p1")


async def test_play_mode_methods(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_apply_play_mode("one")
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "one")
    await player.async_set_shuffle(True)
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "shuffle")
    await player.async_set_shuffle(False)
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "order")
    await player.async_set_repeat(RepeatMode.ONE)
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "one")
    await player.async_set_repeat(RepeatMode.ALL)
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "all")
    await player.async_set_repeat(RepeatMode.OFF)
    assert fake_client.called("async_set_play_mode")[-1][1] == ("dlna:d1", "order")


# ==================== 内容点播 ====================
async def test_play_media_announce(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_play_media("music", "http://tts/x.mp3", announce=True, extra={"volume": 30})
    assert fake_client.called("async_announce")[-1][1] == ("dlna:d1", "http://tts/x.mp3")
    assert fake_client.called("async_announce")[-1][2] == {"volume": 30}


async def test_play_media_announce_volume_from_kwargs(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_play_media("music", "http://tts/x.mp3", announce=True, volume=15)
    assert fake_client.called("async_announce")[-1][2] == {"volume": 15}


async def test_play_media_resolves_content(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    await player.async_play_media("music", f"{MEDIA_URI_PREFIX}album/7")
    call = fake_client.called("async_play_content")[-1]
    assert call[1][:3] == ("dlna:d1", "album", "7")
    # enqueue=ADD → enqueue True
    await player.async_play_media(
        "music", f"{MEDIA_URI_PREFIX}album/7", enqueue=MediaPlayerEnqueue.ADD
    )
    assert fake_client.called("async_play_content")[-1][2]["enqueue"] is True


def test_resolve_content(player):
    assert player._resolve_content("music", f"{MEDIA_URI_PREFIX}song/9") == ("song", "9")
    assert player._resolve_content("music", f"{MEDIA_URI_PREFIX}track/9") == ("song", "9")
    assert player._resolve_content("music", f"{MEDIA_URI_PREFIX}album/9") == ("album", "9")
    assert player._resolve_content("track", "9") == ("song", "9")
    assert player._resolve_content("music", "9") == ("song", "9")
    # 不支持的类型 → 报错
    with pytest.raises(HomeAssistantError):
        player._resolve_content("music", f"{MEDIA_URI_PREFIX}song/")
    with pytest.raises(HomeAssistantError):
        player._resolve_content("music", f"{MEDIA_URI_PREFIX}weird/1")
    with pytest.raises(HomeAssistantError):
        player._resolve_content("weird", "1")


async def test_play_content(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna")
    player._soft_off = True
    await player.async_play_content("playlist", "p1", song_id="s1")
    call = fake_client.called("async_play_content")[-1]
    assert call[1][:3] == ("dlna:d1", "playlist", "p1")
    assert call[2]["song_id"] == "s1"
    assert player._soft_off is False


# ==================== 播放转移 ====================
def test_source_and_source_list(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", name="甲")
    add_peer(coordinator, "dlna:d2", "dlna", name="乙")
    add_peer(coordinator, "dlna:d3", "dlna", name="丙", available=False)
    assert player.source == "甲"
    assert player.source_list == ["甲", "乙"]  # 离线设备不进列表


async def test_select_source_same_name_noop(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", name="甲")
    await player.async_select_source("甲")
    assert fake_client.called("async_queue_play") == []


async def test_select_source_unknown_player(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", name="甲")
    with pytest.raises(HomeAssistantError):
        await player.async_select_source("不存在")


async def test_select_source_no_queue(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", name="甲", queue={})
    add_peer(coordinator, "dlna:d2", "dlna", name="乙")
    with pytest.raises(HomeAssistantError):
        await player.async_select_source("乙")


async def test_select_source_transfers(player, coordinator, fake_client, monkeypatch):
    monkeypatch.setattr(mp_mod, "TRANSFER_SEEK_DELAY", 0)
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        name="甲",
        queue={"items": [{"songId": "s1"}], "currentIndex": 0},
        status={"position": 5, "state": "PLAYING"},
    )
    add_peer(coordinator, "dlna:d2", "dlna", name="乙")
    await player.async_select_source("乙")
    assert fake_client.called("async_clear_queue")[-1][1] == ("dlna:d1",)
    assert fake_client.called("async_stop")[-1][1] == ("dlna:d1",)
    assert fake_client.called("async_queue_play")[-1][1][:2] == ("dlna:d2", [{"songId": "s1"}])
    assert fake_client.called("async_seek")[-1][1] == ("dlna:d2", 5)


async def test_playback_snapshot_paginates(player, coordinator, fake_client):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [], "total": 2, "currentIndex": 1, "playMode": "order"},
        status={"position": 3},
    )
    fake_client.responses["async_get_peer_queue"] = {
        "items": [{"songId": "s1"}, {"songId": "s2"}],
        "total": 2,
    }
    snap = await player._playback_snapshot()
    assert [i["songId"] for i in snap["items"]] == ["s1", "s2"]
    assert snap["index"] == 1
    assert snap["position"] == 3
    assert snap["playing"] is False


async def test_playback_snapshot_empty_returns_none(player, coordinator):
    add_peer(coordinator, "dlna:d1", "dlna", queue={})
    assert await player._playback_snapshot() is None


async def test_playback_snapshot_fetch_error(player, coordinator, fake_client):
    add_peer(coordinator, "dlna:d1", "dlna", queue={"items": [], "total": 5})
    fake_client.raises["async_get_peer_queue"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player._playback_snapshot()


async def test_transfer_playback_pauses_when_not_playing(player, coordinator, fake_client, monkeypatch):
    monkeypatch.setattr(mp_mod, "TRANSFER_SEEK_DELAY", 0)
    add_peer(coordinator, "dlna:d2", "dlna")
    await player._transfer_playback(
        "dlna:d2",
        {"items": [{"songId": "s1"}], "index": 0, "position": 0, "playing": False},
    )
    assert fake_client.called("async_queue_play")
    assert fake_client.called("async_pause")
    assert fake_client.called("async_seek") == []


async def test_transfer_playback_error(player, coordinator, fake_client, monkeypatch):
    monkeypatch.setattr(mp_mod, "TRANSFER_SEEK_DELAY", 0)
    add_peer(coordinator, "dlna:d2", "dlna")
    fake_client.raises["async_queue_play"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player._transfer_playback(
            "dlna:d2",
            {"items": [{"songId": "s1"}], "index": 0, "position": 0, "playing": True},
        )


# ==================== 浏览 / 封面 / 搜索 ====================
def test_thumbnail(player, monkeypatch):
    monkeypatch.setattr(
        player,
        "get_browse_image_url",
        lambda ct, ci, media_image_id=None: f"proxy://{media_image_id}",
    )
    assert player._thumbnail("track", "id", "c1") == "proxy://c1"
    assert player._thumbnail("track", "id", None) is None


async def test_get_media_image(player, coordinator, fake_client):
    add_peer(
        coordinator,
        "dlna:d1",
        "dlna",
        queue={"items": [{"coverArt": "c1"}], "currentIndex": 0},
    )
    assert await player.async_get_media_image() == (b"img", "image/png")
    add_peer(coordinator, "dlna:d1", "dlna", queue={}, status={})
    assert await player.async_get_media_image() == (None, None)


async def test_get_browse_image(player, coordinator, fake_client):
    assert await player.async_get_browse_image(media_image_id="c1") == (b"img", "image/png")
    assert await player.async_get_browse_image(media_image_id="") == (None, None)


async def test_browse_media_ok(player, coordinator, fake_client, monkeypatch):
    monkeypatch.setattr(
        player,
        "get_browse_image_url",
        lambda ct, ci, media_image_id=None: f"proxy://{media_image_id}",
    )
    fake_client.responses["async_get_playlists"] = {
        "playlists": {"playlist": [{"id": "p1", "name": "夜"}]}
    }
    node = await player.async_browse_media(media_content_id=f"{MEDIA_URI_PREFIX}playlists")
    assert node.title == "歌单"
    assert [c.title for c in node.children] == ["夜"]
    # 根菜单
    root = await player.async_browse_media()
    assert root.title == "MusicFlow"


async def test_browse_media_error(player, coordinator, fake_client):
    fake_client.raises["async_get_playlists"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_browse_media(media_content_id=f"{MEDIA_URI_PREFIX}playlists")


async def test_search_media(player, coordinator, fake_client, monkeypatch):
    monkeypatch.setattr(
        player,
        "get_browse_image_url",
        lambda ct, ci, media_image_id=None: f"proxy://{media_image_id}",
    )
    fake_client.responses["async_search"] = {"song": [{"id": "s1", "title": "曲"}]}
    fake_client.responses["async_get_playlists"] = {}
    out = await player.async_search_media(SearchMediaQuery(search_query="曲"))
    assert [c.media_content_id for c in out.result] == [f"{MEDIA_URI_PREFIX}song/s1"]


async def test_search_media_error(player, coordinator, fake_client):
    fake_client.raises["async_search"] = MusicFlowError("boom")
    with pytest.raises(HomeAssistantError):
        await player.async_search_media(SearchMediaQuery(search_query="x"))


# ==================== async_setup_entry 集成 ====================
async def test_async_setup_entry_creates_prunes_and_registers_services(
    hass, fake_client, monkeypatch
):
    import custom_components.musicflow as init_mod
    from custom_components.musicflow.const import (
        SERVICE_ADD_TO_PLAYLIST,
        SERVICE_CLEAR_QUEUE,
        SERVICE_LIKE_TRACK,
        SERVICE_PLAY_CONTENT,
        SERVICE_SET_PLAY_MODE,
    )

    monkeypatch.setattr(init_mod, "MusicFlowClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(
        init_mod, "async_get_clientsession", lambda h, verify_ssl=True: object()
    )
    fake_client.responses["async_get_peers"] = [
        {"peerId": "dlna:d1", "kind": "dlna", "name": "甲", "available": True},
        {"peerId": "dlna:d2", "kind": "dlna", "name": "乙", "available": True},
    ]
    fake_client.responses["async_get_peer_status"] = {}
    entry = _entry_for(hass)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()

    component = hass.data["entity_components"]["media_player"]
    assert {e.peer_id for e in component.entities} == {"dlna:d1", "dlna:d2"}

    # 自定义服务已注册
    services = hass.services.async_services().get(DOMAIN, {})
    for name in (
        SERVICE_PLAY_CONTENT,
        SERVICE_SET_PLAY_MODE,
        SERVICE_CLEAR_QUEUE,
        SERVICE_LIKE_TRACK,
        SERVICE_ADD_TO_PLAYLIST,
    ):
        assert name in services

    # 剪枝：d2 从后端消失,但**恢复窗口内不剪**。后端重启时 /v1/peers 会瞬时
    # 缺 peer;若立刻剪掉实体,HA 会在实体重建时回放上次音量(2026-10-21 现场:
    # 容器重启后 HiVi 被 HA 推成 20)。
    coordinator = hass.data[DOMAIN][entry.entry_id]
    import custom_components.musicflow.media_player as mp_mod

    t0 = mp_mod._monotonic()
    mono = {"now": t0}
    monkeypatch.setattr(mp_mod, "_monotonic", lambda: mono["now"])
    fake_client.responses["async_get_peers"] = [
        {"peerId": "dlna:d1", "kind": "dlna", "name": "甲", "available": True}
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert {e.peer_id for e in component.entities} == {"dlna:d1", "dlna:d2"}

    # 超过恢复窗口仍缺席 → 才真正剪掉
    mono["now"] = t0 + mp_mod.PRUNE_AFTER_S + 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert {e.peer_id for e in component.entities} == {"dlna:d1"}

    # 补建：d3 重新出现 → 新实体
    fake_client.responses["async_get_peers"] = [
        {"peerId": "dlna:d1", "kind": "dlna", "name": "甲", "available": True},
        {"peerId": "dlna:d3", "kind": "dlna", "name": "丁", "available": True},
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert {e.peer_id for e in component.entities} == {"dlna:d1", "dlna:d3"}

    assert await hass.config_entries.async_unload(entry.entry_id) is True


def _entry_for(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        entry_id="mp_entry",
        data={
            "url": "http://mf.local:46400",
            "username": "user",
            "password": "pass",
            "verify_ssl": True,
        },
    )
