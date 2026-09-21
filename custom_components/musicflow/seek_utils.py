"""拖动 seek 纯函数（无 HA/网络依赖，供 CI 单测 + 调用方共用同一套语义）。

背景：原生进度条「拖后不播/回跳/截断」三连修的判定散在 media_player.py 与
coordinator.py 两处 —— 钳制、保护窗、落位判定任一处漂移都会静默退化
（显示不对、手测易当网络卡）。纯函数抽到这里后，CI 可用标准库直接锁死。
"""

from __future__ import annotations

from typing import Any

# seek 乐观保护窗：下发后旧位置上报在此窗口内不覆盖目标值与锚点。
SEEK_GUARD_SECONDS = 8.0
# 落位判定容差：上报 >= 目标 - 容差即视为 seek 已生效，解除保护。
SEEK_SETTLE_TOLERANCE = 2.0
# 尾部余量：目标不得超过 duration - 余量（拖到 100% 四舍五入超 duration
# 会被 DLNA 渲染器拒收/跳开头）。
SEEK_TAIL_MARGIN = 0.5


def clamp_seek_target(position: Any, duration: Any) -> float | None:
    """seek 目标钳制：非法(NaN/非数字)返回 None（调用方直接丢弃）；否则钳到
    [0, duration - 0.5s]（时长未知时只保下限 0）。"""
    try:
        target = float(position)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if target != target:  # NaN
        return None
    if isinstance(duration, (int, float)) and duration > 0:
        cap = float(duration) - SEEK_TAIL_MARGIN
        return max(0.0, min(target, cap if cap > 0 else float(duration)))
    return max(0.0, target)


def seek_guard_active(now_ts: float, guard_until_ts: float | None) -> bool:
    """保护窗是否仍有效（过期即失效，调用方清 guard 恢复正常采样）。"""
    return guard_until_ts is not None and now_ts < guard_until_ts


def should_keep_optimistic(
    reported: float,
    target: float | None,
    now_ts: float,
    guard_until_ts: float | None,
    tolerance: float = SEEK_SETTLE_TOLERANCE,
) -> bool:
    """旧位置上报是否应被丢弃（保持乐观目标值与锚点）。

    保护窗外 → False；窗内且上报仍是旧位置（目标 - 容差以外）→ True；
    上报已落位 → False（调用方解除保护、采纳新采样）。
    """
    if target is None or guard_until_ts is None:
        return False
    if now_ts >= guard_until_ts:
        return False
    return reported < target - tolerance
