"""coordinator.py 单测（简体中文）。

锁死对象：WS 事件 → PeerState 更新 → 实体刷新这条主链路，外加
轮询兜底、device→group 反向索引与防抖、seek 乐观保护在协调器侧的落地。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.musicflow.api import MusicFlowAuthError, MusicFlowError
from custom_components.musicflow.coordinator import (
    GROUP_REFRESH_DEBOUNCE,
    MusicFlowCoordinator,
    PeerState,
)


def peer(peer_id, kind, *, name=None, available=True, platform=None, queue=None):
    """构造一条后端 Peer 记录。"""
    out = {
        "peerId": peer_id,
        "kind": kind,
        "name": name or peer_id,
        "available": available,
    }
    if platform is not None:
        out["platform"] = platform
    if queue is not None:
        out["queue"] = queue
    return out


def listen(coordinator):
    """注册一个计数监听器，返回计数列表。"""
    calls: list[int] = []
    coordinator.async_add_listener(lambda: calls.append(1))
    return calls


# ==================== PeerState 纯属性 ====================
def test_controllable():
    assert PeerState("dlna:d1", "dlna", "d1").controllable is True
    assert PeerState("group:g1", "group", "g1").controllable is True
    assert PeerState("airplay:a1", "airplay", "a1").controllable is True
    assert PeerState("sendspin:s1", "sendspin", "s1").controllable is True
    # web 端的 local 播放跑在浏览器里，HA 控不了 —— 不建实体
    assert PeerState("local:u", "local", "u", platform="web").controllable is False
    # 安卓 / Windows 客户端是真实可遥控设备
    assert PeerState("local:u", "local", "u", platform="android").controllable is True
    assert PeerState("local:u", "local", "u").controllable is True
    assert PeerState("weird:x", "weird", "x").controllable is False


def test_raw_id():
    assert PeerState("dlna:abc", "dlna", "n").raw_id == "abc"
    assert PeerState("group:g/1", "group", "n").raw_id == "g/1"
    assert PeerState("abc", "dlna", "n").raw_id == "abc"
    # 前缀后为空时退回整个 peer_id
    assert PeerState("group:", "group", "n").raw_id == "group:"


def test_current_item_prefers_queue_then_media():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.queue = {"items": [{"id": "s1"}, {"id": "s2"}], "currentIndex": 1}
    state.status = {"media": {"songId": "from-media"}}
    assert state.current_item == {"id": "s2"}
    # 索引越界 → 回退设备上报的 media
    state.queue = {"items": [{"id": "s1"}], "currentIndex": 7}
    assert state.current_item == {"songId": "from-media"}
    # 队列与 media 都没有 → None
    state.queue = {}
    state.status = {"media": "not-a-dict"}
    assert state.current_item is None


def test_track_identity_prefers_ids_then_text():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.queue = {"items": [{"id": "s1"}], "currentIndex": 0}
    assert state._track_identity == "s1"
    state.queue = {"items": [{"songId": "s2"}], "currentIndex": 0}
    assert state._track_identity == "s2"
    state.queue = {"items": [{"title": "歌", "artist": "甲", "album": "专"}], "currentIndex": 0}
    assert state._track_identity == "歌|甲|专"
    state.queue = {"items": [{"album": "专"}], "currentIndex": 0}
    assert state._track_identity is None
    state.queue = {}
    state.status = {}
    assert state._track_identity is None


def test_media_position_pending_reanchor_and_raw():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.pending_reanchor = True
    state.status = {"position": 42}
    assert state.media_position == 0  # 换歌窗口内直接归零，避免跨歌累加
    state.pending_reanchor = False
    assert state.media_position == 42.0
    state.status = {"position": -3}
    assert state.media_position == 0.0
    state.status = {"position": "not-a-number"}
    assert state.media_position is None
    state.status = {"position": 12.3456}
    assert state.media_position == 12.3


def test_media_position_seek_guard_active_and_expired():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.status = {"position": 5}
    state.seek_target = 65.7
    state.seek_guard_until = dt_util.utcnow() + timedelta(seconds=8)
    assert state.media_position == 65.7  # 保护窗内显示乐观目标
    # 过期 → 清掉保护、恢复正常采样
    state.seek_guard_until = dt_util.utcnow() - timedelta(seconds=1)
    assert state.media_position == 5.0
    assert state.seek_target is None
    assert state.seek_guard_until is None


def test_apply_peer_merges_and_keeps_unknown_fields():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.apply_peer(peer("dlna:d1", "dlna", name="客厅", available=True, platform="dlna", queue={"items": [1]}))
    assert state.name == "客厅"
    assert state.available is True
    assert state.platform == "dlna"
    assert state.queue == {"items": [1]}
    # 缺字段时保留旧值（局部事件不抹掉已知状态）
    state.apply_peer({"peerId": "dlna:d1", "kind": "dlna"})
    assert state.name == "客厅"
    assert state.platform == "dlna"
    assert state.queue == {"items": [1]}
    assert state.available is False


def test_apply_status_reanchors_on_track_change():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.queue = {"items": [{"id": "s1"}], "currentIndex": 0}
    state.apply_status({})
    assert state.pending_reanchor is True  # 换歌 → 立即归零
    assert state.media_position == 0
    state.apply_status({"position": 3.0})
    assert state.pending_reanchor is False
    assert state.media_position == 3.0
    # 同一首再上报 position：不再触发归零
    state.apply_status({"position": 4.0})
    assert state.pending_reanchor is False
    assert state.media_position == 4.0


def test_apply_status_ignores_none_and_volume_only():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.status = {"position": 5, "volume": 50}
    state.status_updated_at = dt_util.utcnow() - timedelta(seconds=100)
    anchor = state.status_updated_at
    state.apply_status({"position": None, "volume": 60})  # None 不覆盖
    assert state.status["position"] == 5
    assert state.status["volume"] == 60
    # 纯 volume 事件不该推动进度锚点（否则旧 position + 新时间戳会周期性回跳）
    assert state.status_updated_at == anchor


def test_apply_status_uses_server_updated_at():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.apply_status({"position": 1.0, "updatedAt": 1_700_000_000_000})
    assert state.status_updated_at == dt_util.utc_from_timestamp(1_700_000_000)
    # 非法 updatedAt 回退本地时钟
    state.apply_status({"position": 2.0, "updatedAt": 0})
    assert abs((dt_util.utcnow() - state.status_updated_at).total_seconds()) < 5


def test_apply_status_seek_guard_discards_old_sample():
    state = PeerState("dlna:d1", "dlna", "d1")
    anchor = dt_util.utcnow() - timedelta(seconds=1)
    state.status_updated_at = anchor
    state.seek_target = 65.7
    state.seek_guard_until = dt_util.utcnow() + timedelta(seconds=8)
    state.apply_status({"position": 10.0})
    # 锚点不被旧上报推动，显示保持乐观目标
    assert state.status_updated_at == anchor
    assert state.media_position == 65.7
    assert state.seek_target == 65.7


def test_apply_status_seek_landed_releases_guard():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.seek_target = 65.7
    state.seek_guard_until = dt_util.utcnow() + timedelta(seconds=8)
    state.apply_status({"position": 65.0})  # >= 目标 - 2s → 落位
    assert state.seek_target is None
    assert state.seek_guard_until is None
    assert state.media_position == 65.0


def test_apply_status_seek_expired_releases_guard():
    state = PeerState("dlna:d1", "dlna", "d1")
    state.seek_target = 65.7
    state.seek_guard_until = dt_util.utcnow() - timedelta(seconds=1)
    state.apply_status({"position": 10.0})
    assert state.seek_target is None
    assert state.media_position == 10.0


# ==================== 轮询兜底：_async_update_data ====================
async def test_update_data_builds_prunes_and_pulls_status(coordinator, fake_client):
    fake_client.responses["async_get_peers"] = [
        peer("dlna:d1", "dlna", queue={"items": [1], "currentIndex": 0}),
        peer("group:g1", "group", available=True),
        peer("local:web1", "local", platform="web", available=True),
        peer("local:app1", "local", platform="android", available=True),
        {"peerId": "no-kind"},  # 缺 kind → 跳过
        {"kind": "dlna"},  # 缺 peerId → 跳过
    ]
    fake_client.responses["async_get_peer_status"] = {"state": "PLAYING"}
    fake_client.responses["async_get_starred"] = {"s1"}
    # 预置一个会消失的 peer，验证剪枝
    coordinator.peers["group:gone"] = PeerState("group:gone", "group", "gone")

    await coordinator.async_refresh()

    assert set(coordinator.peers) == {"dlna:d1", "group:g1", "local:web1", "local:app1"}
    assert coordinator.peers["dlna:d1"].queue == {"items": [1], "currentIndex": 0}
    assert coordinator.starred == {"s1"}
    # 只拉可控且在线的 peer 状态（web 端 local 不拉）
    pulled = {call[1][0] for call in fake_client.called("async_get_peer_status")}
    assert pulled == {"dlna:d1", "group:g1", "local:app1"}
    assert len(coordinator.controllable_peers()) == 3


async def test_update_data_skips_status_when_none_available(coordinator, fake_client):
    fake_client.responses["async_get_peers"] = [peer("dlna:d1", "dlna", available=False)]
    await coordinator.async_refresh()
    assert fake_client.called("async_get_peer_status") == []


async def test_update_data_auth_error_raises_config_entry_auth_failed(coordinator, fake_client):
    fake_client.raises["async_get_peers"] = MusicFlowAuthError("凭据过期")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)


async def test_update_data_musicflow_error_raises_update_failed(coordinator, fake_client):
    fake_client.raises["async_get_peers"] = MusicFlowError("网络不通")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_update_data_status_result_exception_is_swallowed(coordinator, fake_client):
    fake_client.responses["async_get_peers"] = [peer("dlna:d1", "dlna")]
    fake_client.raises["async_get_peer_status"] = MusicFlowError("timeout")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.peers["dlna:d1"].status == {}


async def test_groups_index_built_and_skipped_without_group_peers(coordinator, fake_client):
    fake_client.responses["async_get_peers"] = [
        peer("group:g1", "group"),
        peer("group:g2", "group"),
        peer("dlna:d1", "dlna"),
        peer("dlna:d2", "dlna"),
    ]
    fake_client.responses["async_get_groups"] = [
        {"id": "g1", "memberIds": ["d1", "d2", 7]},
        {"id": "g2", "memberIds": ["d2"]},
        {"memberIds": ["d3"]},  # 缺 id → 跳过
        {"id": 9, "memberIds": ["d4"]},  # id 非字符串 → 跳过
    ]
    await coordinator.async_refresh()
    assert coordinator._device_groups == {"d1": {"g1"}, "d2": {"g1", "g2"}}
    assert coordinator.primary_group_of_device("d1") in {"g1"}
    assert coordinator.primary_group_of_device("d2") in {"g1", "g2"}
    assert coordinator.primary_group_of_device("unknown") is None

    # 没有任何 group peer 时直接清空索引，不浪费一次请求
    coordinator2_peers = [peer("dlna:d1", "dlna")]
    fake_client.responses["async_get_peers"] = coordinator2_peers
    await coordinator.async_refresh()
    assert coordinator._device_groups == {}
    assert len(fake_client.called("async_get_groups")) == 1  # 第二次没再拉


async def test_groups_index_error_keeps_previous(coordinator, fake_client):
    fake_client.responses["async_get_peers"] = [peer("group:g1", "group")]
    fake_client.raises["async_get_groups"] = MusicFlowError("boom")
    coordinator._device_groups = {"keep": {"g1"}}
    await coordinator.async_refresh()
    assert coordinator._device_groups == {"keep": {"g1"}}


async def test_starred_error_keeps_previous(coordinator, fake_client):
    fake_client.raises["async_get_starred"] = MusicFlowError("boom")
    coordinator._starred = {"old"}
    await coordinator.async_refresh()
    assert coordinator.starred == {"old"}


# ==================== WS 事件分发 ====================
async def test_on_ws_peer_snapshot_replaces_and_prunes(coordinator):
    calls = listen(coordinator)
    coordinator.peers["dlna:old"] = PeerState("dlna:old", "dlna", "old")
    coordinator._on_ws_message(
        {
            "type": "peer_snapshot",
            "peers": [peer("dlna:d1", "dlna"), {"peerId": "x"}, "not-a-dict"],
        }
    )
    assert set(coordinator.peers) == {"dlna:d1"}
    assert calls == [1]


async def test_on_ws_peer_registered_available_unavailable(coordinator, hass, fake_client):
    # 新 peer 会立刻触发一次请求刷新；刷新以 /v1/peers 为准，假客户端必须也返回它，
    # 否则刚注册进来的 peer 会被当成「后端已移除」在刷新中剪掉。
    fake_client.responses["async_get_peers"] = [peer("dlna:d1", "dlna")]
    calls = listen(coordinator)
    coordinator._on_ws_message({"type": "peer_registered", "peer": peer("dlna:d1", "dlna")})
    assert "dlna:d1" in coordinator.peers
    assert len(calls) == 1

    # 刷新是防抖的（cooldown 10s），推进时间后真正拉 peers。
    # 注意：这里会额外产生一次「刷新完成」的实体通知，所以后续一律用
    # 相对计数（len(calls) == before ± n）断言，避免与刷新时序耦合。
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert coordinator.client.called("async_get_peers")

    # peer_unavailable 是合法负载 → 改状态并通知实体一次
    before = len(calls)
    coordinator._on_ws_message({"type": "peer_unavailable", "peer": peer("dlna:d1", "dlna", available=False)})
    assert coordinator.peers["dlna:d1"].available is False
    assert len(calls) == before + 1

    # 非字典 / 缺字段 → 不改动、不通知
    before = len(calls)
    coordinator._on_ws_message({"type": "peer_available", "peer": None})
    coordinator._on_ws_message({"type": "peer_available", "peer": {"peerId": "y"}})
    assert len(calls) == before


async def test_on_ws_queue_changed_and_cleared(coordinator):
    calls = listen(coordinator)
    coordinator.peers["dlna:d1"] = PeerState("dlna:d1", "dlna", "d1")
    coordinator._on_ws_message(
        {"type": "peer_queue_changed", "peer_id": "dlna:d1", "queue": {"items": [1]}}
    )
    assert coordinator.peers["dlna:d1"].queue == {"items": [1]}
    coordinator._on_ws_message(
        {"type": "peer_queue_cleared", "peer_id": "dlna:d1"}
    )
    assert coordinator.peers["dlna:d1"].queue["items"] == []
    assert coordinator.peers["dlna:d1"].queue["isActive"] is False
    assert calls == [1, 1]

    # 非法负载 → 不改动、不通知
    coordinator._on_ws_message({"type": "peer_queue_changed", "peer_id": 1, "queue": {}})
    coordinator._on_ws_message({"type": "peer_queue_changed", "peer_id": "dlna:d1", "queue": "x"})
    assert calls == [1, 1]


async def test_on_ws_queue_changed_matches_any_namespace(coordinator):
    calls = listen(coordinator)
    namespaces = ("dlna:d1", "group:d1", "airplay:d1", "sendspin:d1")
    for peer_id in namespaces:
        coordinator.peers[peer_id] = PeerState(peer_id, peer_id.split(":")[0], "x")
    # `queue_changed` 的裸 device_id 会在四个命名空间里依次试探（同一物理设备在
    # 不同命名空间下对应不同来源的实体），any(...) 会全部求值 → 四个都写入队列。
    coordinator._on_ws_message({"type": "queue_changed", "device_id": "d1", "queue": {"items": [9]}})
    for peer_id in namespaces:
        assert coordinator.peers[peer_id].queue == {"items": [9]}
    assert calls == [1]

    # 谁都没命中 → 不改动、不通知
    coordinator._on_ws_message({"type": "queue_changed", "device_id": "nope", "queue": {"items": [1]}})
    assert calls == [1]


async def test_on_ws_device_snapshot_and_status(coordinator):
    calls = listen(coordinator)
    coordinator.peers["dlna:d1"] = PeerState("dlna:d1", "dlna", "d1")
    coordinator.peers["airplay:a1"] = PeerState("airplay:a1", "airplay", "a1")
    coordinator.peers["sendspin:s1"] = PeerState("sendspin:s1", "sendspin", "s1")
    coordinator._on_ws_message(
        {
            "type": "snapshot",
            "devices": {"d1": {"state": "PLAYING"}, "a1": {"state": "PAUSED"}, "bad": "x"},
        }
    )
    assert coordinator.peers["dlna:d1"].status["state"] == "PLAYING"
    assert coordinator.peers["airplay:a1"].status["state"] == "PAUSED"
    assert calls == [1]

    coordinator._on_ws_message({"type": "player_state_changed", "device_id": "s1", "state": {"state": "STOPPED"}})
    assert coordinator.peers["sendspin:s1"].status["state"] == "STOPPED"
    assert calls == [1, 1]

    # media_changed 带 media
    coordinator._on_ws_message({"type": "media_changed", "device_id": "d1", "media": {"songId": "s9"}})
    assert coordinator.peers["dlna:d1"].status["media"] == {"songId": "s9"}
    # media 为空 → 走空状态分支（apply_status(None) 返回 False）
    coordinator._on_ws_message({"type": "media_changed", "device_id": "d1", "media": None})
    # 未知设备 / 非法负载
    assert not coordinator._apply_device_status("unknown", {"state": "PLAYING"})
    assert not coordinator._apply_device_status(1, {"state": "PLAYING"})
    assert not coordinator._apply_device_status("d1", "not-a-dict")


async def test_on_ws_device_or_group_change_triggers_refresh(coordinator, hass):
    for msg_type in ("device_list_changed", "group_changed", "group_deleted"):
        coordinator._on_ws_message({"type": msg_type})
    # 三次事件各自安排一次防抖刷新；推进时间后合并成一次真正的拉取
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert coordinator.client.called("async_get_peers")


async def test_on_ws_unknown_type_is_noop(coordinator):
    calls = listen(coordinator)
    coordinator._on_ws_message({"type": "unknown_event"})
    coordinator._on_ws_message({})
    assert calls == []


# ==================== device → group 反向索引与防抖 ====================
async def test_group_refresh_debounce_and_execution(coordinator, hass, fake_client):
    # 成员设备状态来自裸 device_id "d1" → 需存在 dlna:d1 peer 才会命中
    coordinator.peers["dlna:d1"] = PeerState("dlna:d1", "dlna", "d1", available=True)
    coordinator.peers["group:g1"] = PeerState("group:g1", "group", "g1", available=True)
    coordinator._device_groups = {"d1": {"g1"}}
    fake_client.responses["async_get_peer_status"] = {"state": "PLAYING"}
    calls = listen(coordinator)

    # 成员设备状态变化 → 排入待刷新集合并起防抖定时器
    assert coordinator._apply_device_status("d1", {"state": "PAUSED"}) is True
    assert coordinator._pending_groups == {"g1"}
    assert coordinator._group_debounce_cancel is not None
    assert fake_client.called("async_get_peer_status") == []  # 防抖窗口内尚未拉取

    # 推进时钟越过防抖窗口（GROUP_REFRESH_DEBOUNCE=1.5s）→ 定时器自然触发。
    # 不能手工调 _run_group_refresh：它会把取消句柄置空，导致已排定的定时器
    # 无法在 teardown 被撤销，从而留下 Lingering timer 报错。
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert coordinator._pending_groups == set()
    assert coordinator._group_debounce_cancel is None
    assert fake_client.called("async_get_peer_status")[-1][1][0] == "group:g1"
    assert coordinator.peers["group:g1"].status["state"] == "PLAYING"
    assert calls  # 组状态更新 → 通知实体


async def test_run_group_refresh_without_pending_is_noop(coordinator, hass):
    coordinator._run_group_refresh(None)
    await hass.async_block_till_done()
    assert coordinator.client.called("async_get_peer_status") == []


async def test_group_status_refresh_skips_unknown_or_offline(coordinator, fake_client):
    coordinator.peers["group:offline"] = PeerState("group:offline", "group", "o", available=False)
    fake_client.responses["async_get_peer_status"] = {"state": "PLAYING"}
    calls = listen(coordinator)
    await coordinator._async_refresh_group_status({"offline", "missing", "g1"})
    assert fake_client.called("async_get_peer_status") == []
    assert calls == []


async def test_group_status_refresh_error_is_swallowed(coordinator, fake_client):
    coordinator.peers["group:g1"] = PeerState("group:g1", "group", "g1", available=True)
    fake_client.raises["async_get_peer_status"] = MusicFlowError("boom")
    await coordinator._async_refresh_group_status({"g1"})  # 不应抛出


async def test_group_status_refresh_empty_status_skipped(coordinator, fake_client):
    coordinator.peers["group:g1"] = PeerState("group:g1", "group", "g1", available=True)
    fake_client.responses["async_get_peer_status"] = {}
    calls = listen(coordinator)
    await coordinator._async_refresh_group_status({"g1"})
    assert calls == []


# ==================== 便捷访问 ====================
async def test_convenience_accessors(coordinator):
    coordinator.peers["dlna:d1"] = PeerState("dlna:d1", "dlna", "d1")
    coordinator.peers["local:web1"] = PeerState("local:web1", "local", "web1", platform="web")
    coordinator.peers["group:g1"] = PeerState("group:g1", "group", "g1")
    assert {p.peer_id for p in coordinator.controllable_peers()} == {"dlna:d1", "group:g1"}
    assert coordinator.get_peer("group:g1") is coordinator.peers["group:g1"]
    assert coordinator.get_peer("nope") is None
    coordinator._starred = {"s1"}
    assert coordinator.starred == {"s1"}


# ==================== 生命周期 / WS 循环 ====================
async def test_async_start_creates_task_and_shutdown_cancels(coordinator, hass):
    await coordinator.async_start()
    assert coordinator._ws_task is not None
    assert coordinator.ws_connected is False
    # 让 WS 循环立刻退出，避免测试里真的睡 5 秒
    coordinator._closing = True
    await hass.async_block_till_done()
    await coordinator.async_shutdown()
    assert coordinator._ws_task is None


async def test_async_start_is_idempotent(coordinator, hass):
    await coordinator.async_start()
    task = coordinator._ws_task
    await coordinator.async_start()
    assert coordinator._ws_task is task
    coordinator._closing = True
    await hass.async_block_till_done()
    await coordinator.async_shutdown()


async def test_async_shutdown_cancels_debounce_and_ws(coordinator, hass, fake_client):
    await coordinator.async_start()
    cancelled: list[int] = []
    coordinator._group_debounce_cancel = lambda: cancelled.append(1)
    coordinator._closing = True
    await hass.async_block_till_done()
    await coordinator.async_shutdown()
    assert cancelled == [1]  # 卸载时把未执行的组刷新防抖定时器取消掉
    assert coordinator._group_debounce_cancel is None
    assert fake_client.closed is True


async def test_ws_loop_bails_out_when_closing(coordinator, hass, fake_client):
    coordinator._closing = True
    await coordinator._ws_loop()
    assert fake_client.called("async_ws_connect") == []


async def test_ws_loop_one_iteration_on_connect_error(coordinator, fake_client):
    """连接失败也要能收尾重连（这里用 close 钩子把循环掐断，避免真睡 5 秒）。"""
    fake_client.raises["async_ws_connect"] = RuntimeError("down")

    async def close():
        fake_client.closed = True
        coordinator._closing = True

    fake_client.async_ws_close = close
    await coordinator._ws_loop()
    assert fake_client.called("async_ws_connect")
    assert coordinator.ws_connected is False


async def test_ws_loop_normal_iteration_closes_and_stops(coordinator, fake_client):
    async def close():
        fake_client.closed = True
        coordinator._closing = True

    fake_client.async_ws_close = close
    await coordinator._ws_loop()
    # 连上 → 监听返回 → 收尾关闭
    assert fake_client.called("async_ws_connect")
    assert fake_client.called("async_ws_listen")
    assert coordinator.ws_connected is False


async def test_ws_loop_propagates_cancelled(coordinator, fake_client):
    fake_client.raises["async_ws_listen"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await coordinator._ws_loop()


async def test_group_refresh_debounce_constant():
    assert GROUP_REFRESH_DEBOUNCE == 1.5


async def test_peer_state_field_defaults():
    state = PeerState("dlna:d1", "dlna", "d1")
    assert state.queue == {}
    assert state.status == {}
    assert state.status_updated_at is None
    assert state.last_track_id is None
    assert state.pending_reanchor is False
    assert state.seek_target is None
    assert state.seek_guard_until is None
