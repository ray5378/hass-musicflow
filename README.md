# MusicFlow Home Assistant Integration

将 [MusicFlow](https://github.com/ray5378/MusicFlow) 音乐服务器接入 Home Assistant 的自定义集成。

## 功能

- **媒体播放器实体**:MusicFlow 里每个 **DLNA 设备**和每个**播放组**各对应一个 HA `media_player` 实体
- **实时状态**:通过 WebSocket 长连接推送播放状态,断线自动重连;另有 30s 轮询兜底
- **媒体浏览**:在 HA 媒体浏览器中浏览 **歌单 / 专辑 / 艺术家 / 流派**,带封面
- **播放控制**:play / pause / stop / next / previous / seek / volume / 音量步进 / 循环 / 随机 / 清空队列
- **点播播放**:单曲、整张专辑、歌单、艺术家、流派,支持追加入队
- **Zeroconf 自动发现**:MusicFlow 后端广播后,HA 自动发现并提示配置;IP 变化会自动更新
- **凭据失效自动重认证**:API Key 失效时 HA 会弹出重新认证流程

## 安装

### 方式 A:通过 HACS(推荐)

1. 安装 [HACS](https://hacs.xyz/)
2. HACS → 右上角 **⋮ → 自定义仓库**
3. 填入:`https://github.com/ray5378/hass-musicflow`,类别选 **Integration**
4. 搜索 **MusicFlow** → 下载
5. **重启 Home Assistant**
6. 设置 → 设备与服务 → 添加集成 → **MusicFlow**

### 方式 B:手动安装

将 `custom_components/musicflow/` 复制到 HA 的 `config/custom_components/musicflow/`,然后重启 HA。

## 配置

### 先拿到 API Key

集成用 API Key 作长期凭据(登录 Token 24 小时过期,不适合常驻客户端)。两个入口任选:

- **给自己**:MusicFlow Web UI → **设置** → **API Key** → **生成** → **复制**
- **给指定用户**(管理员):**管理 → 用户管理** → 对应用户卡片 → **API Key** → **生成** → **复制**

建议单独建一个 `homeassistant` 账号发 Key,这样撤销时不影响自己日常登录。

> 改密码会自动使该用户的 Key 失效,所以请**先改密码再生成 Key**。

### 再添加集成

如果已运行 MusicFlow(加载项或独立 Docker),集成会通过 Zeroconf 自动发现,地址自动填好,只需补 API Key。手动配置时填写:

| 字段 | 说明 |
|---|---|
| URL | MusicFlow 地址,如 `http://192.168.1.10:46400`(省略协议和端口时按 `http` / `46400` 补全) |
| API Key | 上一步复制的 Key,形如 `mf_xxxxxxxx...` |
| 校验 SSL 证书 | 仅在使用 https 且证书为自签名时取消勾选 |

> 在 MusicFlow 里点「重新生成」或「撤销」会让旧 Key 立即失效,HA 侧会弹出重新认证提示,填入新 Key 即可。

## 实体与设备

集成会注册一个网关设备(MusicFlow 服务端本身),每个播放器实体挂在它下面:

- **DLNA 设备** → `media_player.<设备名>`
- **播放组** → `media_player.<组名>`,状态取自组内 leader 设备

MusicFlow 里新增/移除设备或播放组时,实体会在下一次刷新时自动增补。

## 服务

除标准 `media_player.*` 服务外,还提供三个专用服务:

| 服务 | 说明 |
|---|---|
| `musicflow.play_content` | 按内容类型播放(song / album / playlist / artist / genre),可指定起始位置、播放模式、是否追加 |
| `musicflow.set_play_mode` | 设置播放模式:`order` 顺序 / `shuffle` 随机 / `one` 单曲循环 / `all` 列表循环 |
| `musicflow.clear_queue` | 清空播放队列 |

示例:

```yaml
service: musicflow.play_content
target:
  entity_id: media_player.living_room_speaker
data:
  content_type: playlist
  content_id: "12"
  play_mode: shuffle
```

## media_content_id 格式

自动化调用 `media_player.play_media` 时支持:

| URI | 行为 |
|---|---|
| `musicflow://song/<id>` | 播放单曲 |
| `musicflow://album/<id>` | 整张专辑入队播放 |
| `musicflow://playlist/<id>` | 歌单入队播放 |
| `musicflow://artist/<id>` | 艺术家全部曲目入队播放 |
| `musicflow://genre/<名称>` | 该流派曲目入队播放 |

也可以直接用 `media_content_type: playlist` + `media_content_id: "12"` 这种形式。
带 `enqueue: add` 参数时为追加到队列尾部而非立即替换。

## 架构

```
HA 仪表盘 (控制面)
  └─ media_player.<peer>
       └─ 集成 (Python, 本仓库)
            ├─ REST  → /rest/api/v1/...   (peer 状态 / 队列 / 播放控制)
            ├─ REST  → /rest/...          (OpenSubsonic 曲库浏览与封面)
            └─ WS    ← /ws                (播放状态实时推送)
                          └─ SOAP → DLNA 设备 (真正发声)
```

HA 只充当**远程控制器**,音频流始终在 MusicFlow 后端 ↔ DLNA 设备之间,不经过 HA。

## 版本要求

- Home Assistant **2024.12.0** 及以上
- MusicFlow 服务端 **v1.0.4** 及以上,推荐 **v1.0.5**
  - v1.0.3 起 `/rest/api/v1/play` 支持 `song` 类型
  - v1.0.4 起设置页才能生成 API Key(更早的版本没有生成入口,配置流程走不下去)
  - v1.0.5 起用户管理页可为任意用户签发 Key,设置页也会显示服务端真实版本

不确定自己跑的是哪个版本?打开 `http://<MusicFlow地址>/ping`,v1.0.5 起会返回
`{"status":"ok","version":"1.0.5"}`;若 `version` 缺失或为 `1.0.0`,说明镜像还是旧的。

集成不引入任何额外 Python 依赖,WebSocket 复用 HA 自带的 aiohttp。

## 相关仓库

- MusicFlow 服务端:https://github.com/ray5378/MusicFlow
- HA 加载项仓库:https://github.com/ray5378/hassio-addons
