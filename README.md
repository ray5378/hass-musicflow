# MusicFlow Home Assistant Integration

将 [MusicFlow](https://github.com/ray5378/MusicFlow) 音乐服务器接入 Home Assistant 的自定义集成。

## 功能

- **媒体播放器实体**:每个 MusicFlow 管理的 DLNA 设备变成一个 HA `media_player` 实体
- **实时状态**:通过 WebSocket 长连接推送播放状态(不用轮询)
- **媒体浏览**:在 HA 媒体浏览器中浏览 MusicFlow 的艺术家 / 专辑 / 歌单 / 曲目
- **播放控制**:play / pause / stop / seek / volume / next / prev
- **点播播放**:单曲投射、整张专辑入队、歌单入队
- **Zeroconf 自动发现**:MusicFlow 后端广播后,HA 自动发现并提示配置

## 安装

### 方式 A:通过 HACS(推荐)

1. 安装 [HACS](https://hacs.xyz/)
2. HACS → 集成 → 右上角 **⋮ → 自定义仓库**
3. 填入:`https://github.com/ray5378/hass-musicflow`,类别选 **Integration**
4. 搜索 **MusicFlow** → 下载
5. **重启 Home Assistant**
6. 设置 → 设备与服务 → 添加集成 → **MusicFlow**

### 方式 B:手动安装

将 `custom_components/musicflow/` 复制到 HA 的 `config/custom_components/musicflow/`,然后重启 HA。

## 配置

如果已运行 MusicFlow(加载项或独立 Docker),集成会通过 Zeroconf 自动发现。手动配置时填写:

| 字段 | 说明 |
|---|---|
| URL | MusicFlow 地址,如 `http://192.168.1.10:46400` |
| API Key | MusicFlow 用户的 API Key(在 MusicFlow 设置中生成) |

## 架构

```
HA 仪表盘 (控制面)
  └─ media_player.musicflow_<device>
       └─ 集成 (Python, 本仓库)
            ├─ REST 调用 → MusicFlow 后端 (DLNA 控制 + 队列)
            └─ WebSocket ← MusicFlow 后端 (状态推送)
                              └─ SOAP → DLNA 设备 (真正发声)
```

HA 只充当**远程控制器**,音频流始终在 MusicFlow 后端 ↔ DLNA 设备之间。

## media_content_id 格式

自动化调用 `media_player.play_media` 时支持:

| URI | 行为 |
|---|---|
| `musicflow://song/<id>` | 投射单曲 |
| `musicflow://album/<id>` | 整张专辑入队播放 |
| `musicflow://playlist/<id>` | 歌单入队播放 |

## 前置要求

MusicFlow 后端需提供以下端点(主仓库待实现):

- `GET /ws` —— WebSocket 状态推送
- `_musicflow._tcp.local.` —— mDNS/Zeroconf 广播
- `POST /api/v1/dlna/devices/:id/queue/play` 等队列管理端点
- `/rest/*` 的 Bearer 认证旁路(集成用 API Key 调用 OpenSubsonic 接口)

详见 [集成方案文档](https://github.com/ray5378/MusicFlow) 中的 HA 集成章节。

## 相关仓库

- MusicFlow 服务端:https://github.com/ray5378/MusicFlow
- HA 加载项仓库:https://github.com/ray5378/hassio-addons
