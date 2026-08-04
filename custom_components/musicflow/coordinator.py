"""DataUpdateCoordinator + WS 事件分发。

WS 推来的 player_state_changed / media_changed / queue_changed / device_list_changed
事件更新到本地状态,触发所有实体刷新。首次加载用 REST 拉取全量快照,
之后完全依赖 WS 推送(local_push)。
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import MusicFlowClient
from .const import WS_RECONNECT_DELAY

_LOGGER = logging.getLogger(__name__)


class MusicFlowCoordinator(DataUpdateCoordinator):
    """MusicFlow 协调器:维护设备状态 + WS 事件分发。"""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MusicFlowClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="MusicFlow",
            update_interval=None,  # 不轮询,靠 WS 推送
        )
        self.client = client
        self.config_entry = entry
        # device_id -> status(含 state/position/duration/volume/media)
        self.devices: dict[str, dict] = {}
        # device_id -> queue snapshot
        self.queues: dict[str, dict] = {}
        self._reconnect_task: asyncio.Task | None = None
        self._known_device_ids: set[str] = set()

    async def _async_update_data(self) -> dict[str, dict]:
        """首次拉取:全量设备列表 + 各设备状态快照。"""
        try:
            devices = await self.client.get_devices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("拉取设备列表失败: %s", err)
            return self.devices

        for dev in devices:
            dev_id = dev.get("id")
            if not dev_id:
                continue
            try:
                status = await self.client.get_device_status(dev_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("拉取设备 %s 状态失败: %s", dev_id, err)
                status = {"state": "STOPPED", "position": 0, "duration": 0, "volume": 0}
            status["name"] = dev.get("name") or dev.get("friendlyName") or dev_id
            status["available"] = dev.get("available", True)
            self.devices[dev_id] = status
            self._known_device_ids.add(dev_id)
        return self.devices

    async def start_listening(self) -> None:
        """连接 WS 并注册事件回调。"""
        self.client.add_listener(self._on_ws_message)
        self._reconnect_task = asyncio.create_task(self._maintain_connection())

    async def _maintain_connection(self) -> None:
        """维持 WS 连接,断开后自动重连。"""
        while True:
            try:
                await self.client.connect_ws()
                # connect_ws 内部启动了监听任务,阻塞等待它结束(即断开)
                while self.client._ws and not self.client._ws.closed:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("WS 连接异常,%ds 后重连: %s", WS_RECONNECT_DELAY, err)
            await asyncio.sleep(WS_RECONNECT_DELAY)

    def _on_ws_message(self, msg: dict) -> None:
        """WS 事件回调:更新本地状态,触发实体刷新。"""
        msg_type = msg.get("type")
        device_id = msg.get("device_id")

        if msg_type == "snapshot":
            # 初始全量快照:devices 是 { deviceId: status, ... }
            snapshot_devices = msg.get("devices", {}) or {}
            for dev_id, status in snapshot_devices.items():
                self.devices[dev_id] = status
                self._known_device_ids.add(dev_id)
        elif msg_type == "player_state_changed" and device_id:
            existing = self.devices.get(device_id, {})
            existing.update(msg.get("state", {}))
            self.devices[device_id] = existing
            self._known_device_ids.add(device_id)
        elif msg_type == "media_changed" and device_id:
            existing = self.devices.get(device_id, {})
            existing["media"] = msg.get("media", {})
            self.devices[device_id] = existing
        elif msg_type == "queue_changed" and device_id:
            self.queues[device_id] = msg.get("queue", {})
        elif msg_type == "device_list_changed":
            # 设备增删:触发实体平台重新加载(简单实现:仅刷新)
            asyncio.create_task(self._refresh_devices())
            return

        self.async_update_listeners()

    async def _refresh_devices(self) -> None:
        """设备列表变化时重新拉取。"""
        try:
            devices = await self.client.get_devices()
        except Exception:  # noqa: BLE001
            return
        for dev in devices:
            dev_id = dev.get("id")
            if dev_id and dev_id not in self._known_device_ids:
                self._known_device_ids.add(dev_id)
        self.async_update_listeners()

    async def shutdown(self) -> None:
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        await self.client.disconnect()
