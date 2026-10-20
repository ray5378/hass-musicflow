"""版本声明一致性守卫。

`hacs.json` 的 `homeassistant` 字段是本集成对外声明的最低 HA 核心版本。一旦代码
模块级 import 了只在更高核心才存在的 API，声明就会落后于真实依赖，用户装在旧核心
上会直接 import 失败（本项目就修复过一次这样的漂移：`media_player.py` 无条件
import 了 HA 2025.5 才有的 ``SearchMedia`` / ``SearchMediaQuery``，而声明还停在
2024.12）。

守卫规则：只要 ``custom_components/musicflow/`` 里出现了下表某个版本受限 API，
就要求 `hacs.json` 声明的最低核心版本不低于它被引入的版本。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "musicflow"
HACS_JSON = REPO_ROOT / "hacs.json"

# 声明的最低核心版本：改了这里就必须同步改 hacs.json
DECLARED_MINIMUM = "2025.5.0"

# 版本受限的 HA API：符号名 -> 该 API 被引入的最低 HA 核心版本
VERSION_GATED_APIS = {
    # HA 2025.5 才在 components.media_player 里提供这套媒体搜索协议
    "SearchMedia": "2025.5.0",
    "SearchMediaQuery": "2025.5.0",
}


def _parse(version: str) -> tuple[int, int, int]:
    """把 "2025.5.0" / "2025.5" 解析成可比较的三段元组。"""
    parts = [int(part) for part in re.split(r"[.\-]", str(version))[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _declared_minimum() -> str:
    return json.loads(HACS_JSON.read_text(encoding="utf-8"))["homeassistant"]


def _component_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(COMPONENT_DIR.glob("*.py"))
    )


def test_hacs_json_declares_the_expected_minimum_core_version():
    """hacs.json 的声明必须如实反映代码实际依赖的最低核心版本。"""
    assert _parse(_declared_minimum()) >= _parse(DECLARED_MINIMUM)


def test_declared_minimum_covers_every_version_gated_api_in_use():
    """代码里用到的版本受限 API 都必须被声明的最低版本覆盖。"""
    source = _component_source()
    declared = _parse(_declared_minimum())
    used = []
    for api, introduced in VERSION_GATED_APIS.items():
        if re.search(rf"\b{re.escape(api)}\b", source):
            used.append(api)
            assert declared >= _parse(introduced), (
                f"{api} 需要 HA {introduced}，"
                f"但 hacs.json 只声明了 {_declared_minimum()}"
            )
    # 至少命中一个受限 API，否则说明守卫表或代码扫描已失效（守卫本身需要维护）
    assert used, "未在集成源码中扫描到任何版本受限 API，请检查守卫的扫描逻辑"
