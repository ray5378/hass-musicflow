"""MusicFlow 数据协调器。

状态来源有两条,优先实时、轮询兜底:

1. WebSocket(`/ws`)—— 主通道。后端 services/ws/index.ts 会推:
   - peer_snapshot / peer_registered / peer_available / peer_unavailable
   - peer_queue_changed / peer_queue_cleared
   - snapshot / player_state_changed / media_changed / queue_changed(按 device_id)
   - group_changed / group_deleted / device_list_changed

2. 定时轮询(POLL_INTERVAL_SECONDS)—— 兜底。WS 断线、或某些状态没有独立事件
   (典型是 group 的传输状态)时靠它纠偏。

group 的实时性单独处理:组状态派生自 leader 设备,而 leader 只会以自己的
device_id 发 player_state_changed。所以这里维护 device → group 的反向索引,
收到成员设备状态变化时防抖拉一次 `/v1/peers/group:<id>/status`。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MusicFlowAuthError, MusicFlowClient, MusicFlowError
from .const import (
    CONTROLLABLE_KINDS,
    DOMAIN,
    PEER_KIND_DLNA,
    PEER_KIND_GROUP,
    POLL_INTERVAL_SECONDS,
    WS_RECONNECT_MAX,
    WS_RECONNECT_MIN,
)

_LOGGER = logging.getLogger(__name__)

# 成员设备状态变化后,合并多次抖动再拉组状态
GROUP_REFRESH_DEBOUNCE = 1.5


@dataclass
class PeerState:
    """单个 peer 的完整状态(peer 元信息 + 队列 + 传输状态)。"""

    peer_id: str
    kind: str
    name: str
    available: bool = False
    queue: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    status_updated_at: datetime | None = None
    # 进度时间轴修正:HA 的 media_position 必须随"当前曲目"归零,否则会拿旧锚点
    # 把上一首甚至几首歌的进度按 (now - 旧锚点) 持续外推,表现为"换歌后时间仍往前
    # 跑 / 进度是几首歌加起来的时间"。换歌瞬间把进度归零并立即重锚。
    last_track_id: str | None = None
    pending_reanchor: bool = False

    @property
    def controllable(self) -> bool:
        """local peer 的音频跑在浏览器里,HA 控不了,不建实体。"""
        return self.kind in CONTROLLABLE_KINDS

    @property
    def raw_id(self) -> str:
        """去掉 `dlna:` / `group:` 前缀的裸 id。"""
        _, _, rest = self.peer_id.partition(":")
        return rest or self.peer_id

    @property
    def current_item(self) -> dict[str, Any] | None:
        """当前曲目:优先队列(信息全,带 albumId/duration),回退设备上报的 media。"""
        items = self.queue.get("items") or []
        index = self.queue.get("currentIndex", -1)
        if isinstance(index, int) and 0 <= index < len(items):
            item = items[index]
            if isinstance(item, dict):
                return item
        media = self.status.get("media")
        return media if isinstance(media, dict) else None

    @property
    def _track_identity(self) -> str | None:
        """当前曲目身份(稳定字符串),用于在换歌时重置进度时间轴。

        优先用队列当前项的 id/songId(最权威),队列不可用时回退到 media 的
        songId / title|artist|album。空则返回 None(未知曲目,不触发重置)。
        """
        item = self.current_item
        if isinstance(item, dict):
            sid = item.get("id") or item.get("songId")
            if sid:
                return str(sid)
            title = item.get("title") or ""
            artist = item.get("artist") or ""
            album = item.get("album") or ""
            if title or artist:
                return f"{title}|{artist}|{album}"
        return None

    @property
    def media_position(self) -> int | None:
        """HA 标准 media_position:本轨内的播放位置(从 0 起),跨歌不累加。

        换歌后的极短窗口(pending_reanchor)直接返回 0,避免旧 position 被 HA 按
        旧锚点插值放大成"几首歌加起来的时间"。后端已保证 position 每轨归零
        (control.ts 按 songId 重置基线),这里只做 HA 合规的归零 + 钳制。
        """
        if self.pending_reanchor:
            return 0
        raw = self.status.get("position")
        if not isinstance(raw, (int, float)):
            return None
        return max(0, int(raw))

    def apply_peer(self, peer: dict[str, Any]) -> None:
        """合并一条后端 Peer / PeerWithQueue 记录。"""
        self.name = peer.get("name") or self.name
        self.available = bool(peer.get("available"))
        queue = peer.get("queue")
        if isinstance(queue, dict):
            self.queue = queue

    def apply_status(self, status: dict[str, Any]) -> None:
        """合并一条 DeviceStatus。字段缺失时保留旧值,避免局部事件抹掉已知状态。

        进度时间轴处理(关键,修 HA 标准媒体卡片进度跨歌累加):
        - 检测曲目身份变化(_track_identity),换歌时立刻把 media_position 归零
          (pending_reanchor)并把 media_position_updated_at 重锚到当前时刻,否则 HA
          会拿旧锚点把上一首甚至几首歌的进度按 (now - 旧锚点) 持续外推,表现为
          "换歌后时间仍往前跑 / 进度是几首歌加起来的时间"。
        - media_position_updated_at 的锚点在本条状态携带 position 时更新:优先用
          服务端的 updatedAt(ms epoch,服务端采样 position 的时刻),时间基准与
          position 一致,卡片按 (now - updatedAt) 插值才不会滞后/回跳。纯 volume /
          muted 事件不重置进度时间轴,避免旧 position + 新时间戳造成周期性回跳。
        """
        merged = dict(self.status)
        merged.update({k: v for k, v in status.items() if v is not None})
        self.status = merged

        # 换歌检测:曲目身份变了 -> 重置进度时间轴(归零 + 重锚)。
        tid = self._track_identity
        if tid and tid != self.last_track_id:
            self.last_track_id = tid
            self.pending_reanchor = True
            self.status_updated_at = dt_util.utcnow()

        if "position" in status and isinstance(status.get("position"), (int, float)):
            updated_at = status.get("updatedAt")
            if isinstance(updated_at, (int, float)) and updated_at > 0:
                self.status_updated_at = dt_util.utc_from_timestamp(updated_at / 1000)
            else:
                self.status_updated_at = dt_util.utcnow()
            # 本轨第一次带来 position 即解除"归零窗口",恢复正常读取。
            self.pending_reanchor = False


class MusicFlowCoordinator(DataUpdateCoordinator[dict[str, PeerState]]):
    """维护全部 peer 的状态,并把 WS 推送翻译成实体更新。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MusicFlowClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.client = client
        self.peers: dict[str, PeerState] = {}
        # device_id → 包含它的 group_id 集合(用于把设备事件放大到组)
        self._device_groups: dict[str, set[str]] = {}
        # 当前用户已收藏的 songId 集合(卡片心形按钮 / 实体 liked 属性)
        self._starred: set[str] = set()
        self._ws_task: asyncio.Task | None = None
        self._ws_connected = False
        self._closing = False
        self._pending_groups: set[str] = set()
        self._group_debounce_cancel: Any = None

        client.add_listener(self._on_ws_message)

    # ==================== 生命周期 ====================
    async def async_start(self) -> None:
        """启动 WS 后台任务。首次数据拉取由 config_entry_first_refresh 负责。"""
        if self._ws_task is None:
            self._ws_task = self.entry.async_create_background_task(
                self.hass, self._ws_loop(), f"{DOMAIN}_ws"
            )

    async def async_shutdown(self) -> None:
        self._closing = True
        if self._group_debounce_cancel is not None:
            self._group_debounce_cancel()
            self._group_debounce_cancel = None
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None
        await self.client.async_ws_close()
        await super().async_shutdown()

    # ==================== 轮询 ====================
    async def _async_update_data(self) -> dict[str, PeerState]:
        try:
            peers = await self.client.async_get_peers()
        except MusicFlowAuthError as err:
            # 触发 HA 的重新认证流程,而不是一直刷失败日志
            raise ConfigEntryAuthFailed(f"API Key 已失效: {err}") from err
        except MusicFlowError as err:
            raise UpdateFailed(str(err)) from err

        seen: set[str] = set()
        for peer in peers:
            peer_id = peer.get("peerId")
            kind = peer.get("kind")
            if not isinstance(peer_id, str) or not isinstance(kind, str):
                continue
            seen.add(peer_id)
            state = self.peers.get(peer_id)
            if state is None:
                state = PeerState(peer_id=peer_id, kind=kind, name=peer.get("name") or peer_id)
                self.peers[peer_id] = state
            state.apply_peer(peer)

        # group peer 会被后端整个删掉(removeGroup),这里同步移除
        for peer_id in list(self.peers):
            if peer_id not in seen:
                self.peers.pop(peer_id, None)

        await self._async_refresh_groups_index()
        await self._async_refresh_starred()

        # 只拉可控且在线的 peer 状态,离线设备拉状态会白等超时
        targets = [
            p for p in self.peers.values() if p.controllable and p.available
        ]
        if targets:
            results = await asyncio.gather(
                *(self.client.async_get_peer_status(p.peer_id) for p in targets),
                return_exceptions=True,
            )
            for peer_state, result in zip(targets, results):
                if isinstance(result, dict):
                    peer_state.apply_status(result)
                elif isinstance(result, Exception):
                    _LOGGER.debug("拉取 %s 状态失败: %s", peer_state.peer_id, result)

        return self.peers

    async def _async_refresh_groups_index(self) -> None:
        """重建 device → groups 索引。组变动不频繁,跟着轮询走即可。"""
        if not any(p.kind == PEER_KIND_GROUP for p in self.peers.values()):
            self._device_groups = {}
            return
        try:
            data = await self.client.async_get_groups()
        except MusicFlowError as err:
            _LOGGER.debug("拉取组列表失败: %s", err)
            return
        index: dict[str, set[str]] = {}
        for group in data:
            group_id = group.get("id")
            if not isinstance(group_id, str):
                continue
            ordered = [m for m in (group.get("memberIds") or []) if isinstance(m, str)]
            for member in ordered:
                index.setdefault(member, set()).add(group_id)
        self._device_groups = index

    async def _async_refresh_starred(self) -> None:
        """刷新「我喜欢的音乐」songId 集合(供实体的 liked 属性与卡片心形按钮)。"""
        try:
            self._starred = await self.client.async_get_starred()
        except MusicFlowError as err:
            _LOGGER.debug("拉取收藏列表失败: %s", err)

    # ==================== WebSocket ====================
    async def _ws_loop(self) -> None:
        """连接 → 监听 → 断线退避重连,直到集成卸载。"""
        delay = WS_RECONNECT_MIN
        while not self._closing:
            try:
                await self.client.async_ws_connect()
                self._ws_connected = True
                delay = WS_RECONNECT_MIN
                _LOGGER.debug("MusicFlow WebSocket 已连接")
                # 重连后立刻全量对齐一次,补上断线期间漏掉的事件
                await self.async_request_refresh()
                await self.client.async_ws_listen()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - 任何异常都要能重连
                _LOGGER.debug("MusicFlow WebSocket 异常: %s", err)
            finally:
                self._ws_connected = False
                await self.client.async_ws_close()
            if self._closing:
                break
            _LOGGER.debug("MusicFlow WebSocket %.0fs 后重连", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX)

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected

    @callback
    def _on_ws_message(self, msg: dict[str, Any]) -> None:
        """WS 消息分发。运行在事件循环里,只做内存更新 + 通知实体。"""
        msg_type = msg.get("type")
        changed = False

        if msg_type == "peer_snapshot":
            changed = self._apply_peer_snapshot(msg.get("peers") or [])
        elif msg_type in ("peer_registered", "peer_available", "peer_unavailable"):
            changed = self._apply_peer(msg.get("peer"))
        elif msg_type == "peer_queue_changed":
            changed = self._apply_queue(msg.get("peer_id"), msg.get("queue"))
        elif msg_type == "peer_queue_cleared":
            changed = self._apply_queue(
                msg.get("peer_id"),
                {"items": [], "currentIndex": -1, "isActive": False, "ended": False},
            )
        elif msg_type == "snapshot":
            changed = self._apply_device_snapshot(msg.get("devices") or {})
        elif msg_type == "player_state_changed":
            changed = self._apply_device_status(msg.get("device_id"), msg.get("state"))
        elif msg_type == "media_changed":
            media = msg.get("media")
            changed = self._apply_device_status(
                msg.get("device_id"), {"media": media} if media else None
            )
        elif msg_type == "queue_changed":
            # QueueController 的 key 对 dlna 是 deviceId、对 group 是 groupId,
            # 事件字段统一叫 device_id,所以两个命名空间都试一下。
            raw_id = msg.get("device_id")
            queue = msg.get("queue")
            changed = any(
                (
                    self._apply_queue(f"{PEER_KIND_DLNA}:{raw_id}", queue, strict=True),
                    self._apply_queue(f"{PEER_KIND_GROUP}:{raw_id}", queue, strict=True),
                )
            )
        elif msg_type in ("device_list_changed", "group_changed", "group_deleted"):
            # 设备/组增删 → 需要重新拉 peer 列表来建/删实体
            self.hass.async_create_task(self.async_request_refresh())
            return

        if changed:
            self.async_update_listeners()

    @callback
    def _apply_peer_snapshot(self, peers: list[Any]) -> bool:
        seen: set[str] = set()
        for peer in peers:
            if not isinstance(peer, dict):
                continue
            peer_id = peer.get("peerId")
            kind = peer.get("kind")
            if not isinstance(peer_id, str) or not isinstance(kind, str):
                continue
            seen.add(peer_id)
            state = self.peers.get(peer_id)
            if state is None:
                state = PeerState(peer_id=peer_id, kind=kind, name=peer.get("name") or peer_id)
                self.peers[peer_id] = state
            state.apply_peer(peer)
        for peer_id in list(self.peers):
            if peer_id not in seen:
                self.peers.pop(peer_id, None)
        return True

    @callback
    def _apply_peer(self, peer: Any) -> bool:
        if not isinstance(peer, dict):
            return False
        peer_id = peer.get("peerId")
        kind = peer.get("kind")
        if not isinstance(peer_id, str) or not isinstance(kind, str):
            return False
        state = self.peers.get(peer_id)
        if state is None:
            state = PeerState(peer_id=peer_id, kind=kind, name=peer.get("name") or peer_id)
            self.peers[peer_id] = state
            # 新 peer:让平台侧有机会补建实体
            self.hass.async_create_task(self.async_request_refresh())
        state.apply_peer(peer)
        return True

    @callback
    def _apply_queue(self, peer_id: Any, queue: Any, *, strict: bool = False) -> bool:
        if not isinstance(peer_id, str) or not isinstance(queue, dict):
            return False
        state = self.peers.get(peer_id)
        if state is None:
            # strict=True 用于 queue_changed 的双命名空间试探,命不中属正常
            return False
        merged = dict(state.queue)
        merged.update(queue)
        state.queue = merged
        return True

    @callback
    def _apply_device_snapshot(self, devices: dict[str, Any]) -> bool:
        changed = False
        for device_id, status in devices.items():
            if isinstance(status, dict) and self._apply_device_status(device_id, status):
                changed = True
        return changed

    @callback
    def _apply_device_status(self, device_id: Any, status: Any) -> bool:
        if not isinstance(device_id, str) or not isinstance(status, dict):
            return False
        state = self.peers.get(f"{PEER_KIND_DLNA}:{device_id}")
        if state is None:
            return False
        state.apply_status(status)
        # 该设备若属于某些组,组状态派生自 leader,防抖补一次组状态
        groups = self._device_groups.get(device_id)
        if groups:
            self._schedule_group_refresh(groups)
        return True

    @callback
    def _schedule_group_refresh(self, group_ids: set[str]) -> None:
        self._pending_groups |= group_ids
        if self._group_debounce_cancel is not None:
            self._group_debounce_cancel()
        self._group_debounce_cancel = async_call_later(
            self.hass, GROUP_REFRESH_DEBOUNCE, self._run_group_refresh
        )

    @callback
    def _run_group_refresh(self, _now: Any) -> None:
        self._group_debounce_cancel = None
        group_ids = self._pending_groups
        self._pending_groups = set()
        if group_ids:
            self.hass.async_create_task(self._async_refresh_group_status(group_ids))

    async def _async_refresh_group_status(self, group_ids: set[str]) -> None:
        updated = False
        for group_id in group_ids:
            peer_id = f"{PEER_KIND_GROUP}:{group_id}"
            state = self.peers.get(peer_id)
            if state is None or not state.available:
                continue
            try:
                status = await self.client.async_get_peer_status(peer_id)
            except MusicFlowError as err:
                _LOGGER.debug("拉取组 %s 状态失败: %s", group_id, err)
                continue
            if status:
                state.apply_status(status)
                updated = True
        if updated:
            self.async_update_listeners()

    # ==================== 便捷访问 ====================
    def controllable_peers(self) -> list[PeerState]:
        return [p for p in self.peers.values() if p.controllable]

    def get_peer(self, peer_id: str) -> PeerState | None:
        return self.peers.get(peer_id)

    @property
    def starred(self) -> set[str]:
        """当前用户已收藏的 songId 集合。"""
        return self._starred

    # ---- 分组归属(设备 → 组,只读)----
    def primary_group_of_device(self, device_id: str) -> str | None:
        """设备所属的组。

        MusicFlow 允许一台设备同时在多个组里,这里取任意一个作为它的归属,
        用于把成员实体的传输控制转发给组(镜像服务器行为,不改服务器配置)。
        """
        groups = self._device_groups.get(device_id)
        return next(iter(groups)) if groups else None
