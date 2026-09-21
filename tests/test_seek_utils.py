"""拖动 seek 纯函数单测（标准库 unittest，无第三方依赖，CI 直接跑）。

锁死对象：custom_components/musicflow/seek_utils.py —— 原生进度条
「拖后不播/回跳/截断」三连修的判定语义。任一处漂移都会静默退化，
必须改了语义就改用例。
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "musicflow"),
)

from seek_utils import (
    SEEK_GUARD_SECONDS,
    SEEK_SETTLE_TOLERANCE,
    SEEK_TAIL_MARGIN,
    clamp_seek_target,
    seek_guard_active,
    should_keep_optimistic,
)


class ClampSeekTargetTest(unittest.TestCase):
    def test_normal_passthrough(self):
        self.assertEqual(clamp_seek_target(65.7, 200), 65.7)

    def test_tail_clamped_with_margin(self):
        # 拖到 100%：四舍五入超 duration 会被 DLNA 拒收 → 留 0.5s 余量。
        self.assertEqual(clamp_seek_target(200, 200), 200 - SEEK_TAIL_MARGIN)
        self.assertEqual(clamp_seek_target(999, 200), 200 - SEEK_TAIL_MARGIN)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(clamp_seek_target(-5, 200), 0.0)

    def test_unknown_duration_only_lower_bound(self):
        self.assertEqual(clamp_seek_target(999, 0), 999)
        self.assertEqual(clamp_seek_target(999, None), 999)
        self.assertEqual(clamp_seek_target(-3, None), 0.0)

    def test_invalid_input_returns_none(self):
        for bad in ("abc", None, float("nan"), [1]):
            self.assertIsNone(clamp_seek_target(bad, 200), bad)


class SeekGuardTest(unittest.TestCase):
    def test_window(self):
        self.assertTrue(seek_guard_active(100.0, 108.0))
        self.assertFalse(seek_guard_active(108.0, 108.0))
        self.assertFalse(seek_guard_active(200.0, 108.0))
        self.assertFalse(seek_guard_active(100.0, None))

    def test_constants(self):
        self.assertEqual(SEEK_GUARD_SECONDS, 8.0)
        self.assertEqual(SEEK_SETTLE_TOLERANCE, 2.0)

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
