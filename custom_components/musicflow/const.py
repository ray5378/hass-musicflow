"""MusicFlow 集成常量。

路径前缀务必与 MusicFlow 后端 index.ts 的挂载点保持一致:
    app.route("/rest", restRoutes)            → OpenSubsonic
    app.route("/rest/api", apiRoutes)         → 内部 REST(/v1/...)
    server upgrade "/ws"                      → WebSocket
注意 `/api/*` 挂的是空的 navidromeRoutes,不要往那里发请求。
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "musicflow"

# ==================== ConfigEntry 字段 ====================
CONF_URL: Final = "url"  # MusicFlow 服务器地址,如 http://192.168.1.10:46400
CONF_API_KEY: Final = "api_key"  # 用户 API Key(Bearer 认证)
CONF_VERIFY_SSL: Final = "verify_ssl"

DEFAULT_PORT: Final = 46400

# ==================== 后端路径 ====================
# 内部 REST API 前缀(apiRoutes 挂在 /rest/api,路由自身再带 /v1)
API_PREFIX: Final = "/rest/api/v1"
# OpenSubsonic 前缀(浏览曲库用)
SUBSONIC_PREFIX: Final = "/rest"
# WebSocket 路径(挂在根,不在 /rest 下)
WS_PATH: Final = "/ws"

# ==================== 运行参数 ====================
REQUEST_TIMEOUT: Final = 15
WS_HEARTBEAT: Final = 30
WS_RECONNECT_MIN: Final = 5
WS_RECONNECT_MAX: Final = 120
# WS 已覆盖实时推送,轮询只作兜底(组状态没有独立事件,靠这个刷新)
POLL_INTERVAL_SECONDS: Final = 30

# Zeroconf 服务类型(必须与后端 services/discovery/mdns.ts 广播一致)
ZEROCONF_TYPE: Final = "_musicflow._tcp.local."

# ==================== 媒体 URI ====================
MEDIA_URI_PREFIX: Final = "musicflow://"
# /rest/api/v1/play 支持的内容类型(见 backend services/content.ts)
PLAYABLE_TYPES: Final = ("song", "album", "playlist", "artist", "genre")

BROWSE_LIMIT: Final = 300

# ==================== 播放转移 / 分组 ====================
# queue/play 之后设备要先起播才吃得下 seek,这段是给它的缓冲(见后端 announce.ts 同款处理)
TRANSFER_SEEK_DELAY: Final = 1.2
# HA 分组 UI 直接建组时的组名后缀:"客厅音箱 组"

# ==================== peer 种类 ====================
PEER_KIND_LOCAL: Final = "local"
PEER_KIND_DLNA: Final = "dlna"
PEER_KIND_GROUP: Final = "group"
# 只有 dlna / group 由后端驱动音频,能被 HA 控制;
# local peer 的音频跑在浏览器里,后端只存队列,不建实体。
CONTROLLABLE_KINDS: Final = (PEER_KIND_DLNA, PEER_KIND_GROUP)

# ==================== 自定义服务 ====================
SERVICE_PLAY_CONTENT: Final = "play_content"
SERVICE_SET_PLAY_MODE: Final = "set_play_mode"
SERVICE_CLEAR_QUEUE: Final = "clear_queue"

ATTR_CONTENT_TYPE: Final = "content_type"
ATTR_CONTENT_ID: Final = "content_id"
ATTR_START_INDEX: Final = "start_index"
ATTR_PLAY_MODE: Final = "play_mode"
ATTR_ENQUEUE: Final = "enqueue"

PLAY_MODES: Final = ("order", "one", "all", "shuffle")
