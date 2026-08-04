"""MusicFlow 集成常量。"""

DOMAIN = "musicflow"

# ConfigEntry 字段
CONF_URL = "url"  # MusicFlow 服务器地址,如 http://192.168.1.10:46400
CONF_API_KEY = "api_key"  # MusicFlow 用户的 API Key(Bearer 认证)

# 默认值
DEFAULT_PORT = 46400

# WebSocket 重连延迟(秒)
WS_RECONNECT_DELAY = 5

# Zeroconf 服务类型(必须与后端 mDNS 广播一致)
ZEROCONF_TYPE = "_musicflow._tcp.local."

# 自定义 media_content_id 前缀
MEDIA_URI_PREFIX = "musicflow://"

# 浏览器默认拉取数量
BROWSE_LIMIT = 200

# 服务名
SERVICE_PLAY_MEDIA = "play_media"
