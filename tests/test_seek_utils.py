"""拖动 seek 纯函数单测（标准库 unittest，无第三方依赖，CI 直接跑）。

锁死对象：custom_components/musicflow/seek_utils.py —— 原生进度条
「拖后不播/回跳/截断」三连修的判定语义，外加「精度」（最小粒度 1 秒）。
任一处漂移都会静默退化，必须改了语义就改用例。
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "musicflow"),
)

from seek_utils import (
    SEEK_GRANULARITY_SECONDS,
    SEEK_GUARD_SECONDS,
    SEEK_SETTLE_TOLERANCE,
    SEEK_TAIL_MARGIN,
    align_seek_seconds,
    clamp_seek_target,
    seek_guard_active,
    should_keep_optimistic,
)


class AlignSeekSecondsTest(unittest.TestCase):
    """精度守卫：下发目标一律整秒（服务端 25ms 帧栅格的整数倍）。"""

    def test_truncates_to_whole_second(self):
        # 与客户端 Duration.inSeconds 同语义：**向下取整**（不是四舍五入），
        # 否则同一落点会在各端落到不同的秒。
        self.assertEqual(align_seek_seconds(31.178), 31.0)
        self.assertEqual(align_seek_seconds(30.178), 30.0)
        self.assertEqual(align_seek_seconds(87.033), 87.0)
        self.assertEqual(align_seek_seconds(31.999), 31.0)
        self.assertEqual(align_seek_seconds(0.9), 0.0)

    def test_whole_seconds_are_identity(self):
        # 客户端路径本来就整秒 → 该函数对它是恒等变换（零行为变更）。
        for sec in (0, 1, 37, 49, 62, 83, 108, 111):
            self.assertEqual(align_seek_seconds(sec), float(sec))

    def test_result_always_on_25ms_frame_grid(self):
        # 服务端硬契约：整秒必然是 25ms 整数倍（1000 / 25 = 40），因此窗口的
        # 毫秒基准与帧栅格取帧严格同源，不会出现「每轮淘汰但游标不前进」的自旋。
        for raw in (1, 12.345, 31.178, 87.033, 108.999, 3599.4):
            aligned = align_seek_seconds(raw)
            self.assertEqual((aligned * 1000) % 25, 0)
            # 代价上界：截断误差 < 1 秒，且恒为「偏小」（与客户端同向）。
            self.assertGreaterEqual(raw - aligned, 0.0)
            self.assertLess(raw - aligned, SEEK_GRANULARITY_SECONDS)

    def test_negative_and_non_finite_to_zero(self):
        self.assertEqual(align_seek_seconds(-1), 0.0)
        self.assertEqual(align_seek_seconds(float("nan")), 0.0)
        self.assertEqual(align_seek_seconds(float("inf")), 0.0)
        self.assertEqual(align_seek_seconds(float("-inf")), 0.0)
        self.assertEqual(align_seek_seconds("abc"), 0.0)
        self.assertEqual(align_seek_seconds(None), 0.0)

    def test_granularity_constant(self):
        # 改它即改跨端契约（主仓/卡片/客户端各自持一份同语义实现）。
        self.assertEqual(SEEK_GRANULARITY_SECONDS, 1.0)


class ClampSeekTargetTest(unittest.TestCase):
    def test_normal_passthrough(self):
        # 钳制 + 整秒对齐：65.7 → 65.0（对齐发生在钳制之后）。
        self.assertEqual(clamp_seek_target(65.7, 200), 65.0)
        self.assertEqual(clamp_seek_target(65.0, 200), 65.0)

    def test_tail_clamped_with_margin(self):
        # 拖到 100%：四舍五入超 duration 会被 DLNA 拒收 → 留 0.5s 余量，
        # 再向下取整（199.5 → 199.0）。
        self.assertEqual(clamp_seek_target(200, 200), 199.0)
        self.assertEqual(clamp_seek_target(999, 200), 199.0)
        # 余量本身仍严格成立（不得越界到 duration）。
        self.assertLess(clamp_seek_target(999, 200), 200)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(clamp_seek_target(-5, 200), 0.0)

    def test_unknown_duration_only_lower_bound(self):
        self.assertEqual(clamp_seek_target(999, 0), 999.0)
        self.assertEqual(clamp_seek_target(999, None), 999.0)
        self.assertEqual(clamp_seek_target(-3, None), 0.0)

    def test_invalid_input_returns_none(self):
        for bad in ("abc", None, float("nan"), [1]):
            self.assertIsNone(clamp_seek_target(bad, 200), bad)

    def test_result_is_always_whole_second(self):
        # 无论走哪条分支（有/无时长、钳制与否），出口都必须是整秒。
        for pos, dur in ((31.178, 200), (31.178, 0), (31.178, None), (-1, 200), (199.9, 200)):
            target = clamp_seek_target(pos, dur)
            self.assertIsNotNone(target, (pos, dur))
            self.assertEqual(float(target) % SEEK_GRANULARITY_SECONDS, 0.0, (pos, dur))


class SeekGuardTest(unittest.TestCase):
    def test_window(self):
        self.assertTrue(seek_guard_active(100.0, 108.0))
        self.assertFalse(seek_guard_active(108.0, 108.0))
        self.assertFalse(seek_guard_active(200.0, 108.0))
        self.assertFalse(seek_guard_active(100.0, None))

    def test_constants(self):
        self.assertEqual(SEEK_GUARD_SECONDS, 8.0)
        self.assertEqual(SEEK_SETTLE_TOLERANCE, 2.0)
        self.assertEqual(SEEK_TAIL_MARGIN, 0.5)

    def test_old_sample_discarded(self):
        # 下发 65.7s，读回 10s（旧采样）→ 保持乐观值。
        self.assertTrue(should_keep_optimistic(10.0, 65.7, 100.0, 108.0))

    def test_landed_sample_accepted(self):
        # 读回 64s（目标-2s 以内）→ 落位，采纳。
        self.assertFalse(should_keep_optimistic(64.0, 65.7, 100.0, 108.0))
        self.assertFalse(should_keep_optimistic(65.7, 65.7, 100.0, 108.0))

    def test_expired_guard_releases(self):
        self.assertFalse(should_keep_optimistic(10.0, 65.7, 200.0, 108.0))

    def test_no_target_never_guards(self):
        self.assertFalse(should_keep_optimistic(10.0, None, 100.0, 108.0))
        self.assertFalse(should_keep_optimistic(10.0, 65.7, 100.0, None))


if __name__ == "__main__":
    unittest.main()
