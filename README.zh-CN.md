# MusicFlow Home Assistant 集成

将自托管音乐库服务器 [MusicFlow](https://github.com/ray5378/MusicFlow) 接入 Home Assistant 的自定义集成。

MusicFlow 管理的每个 **DLNA 设备**和每个**播放组**，都会变成一个原生 HA `media_player` 实体，状态通过 WebSocket 实时推送。

> English documentation: [README.md](README.md)
>
> **注意**：HACS 展示仓库说明时会丢弃所有非 ASCII 字符，所以面向 HACS 的主 README 必须是英文。中文文档放在本文件。

[![在 Home Assistant 中打开并添加此仓库][my-badge]][my-link]

---

## 功能概览

| 能力 | 说明 |
|---|---|
| 媒体播放器实体 | 每个 DLNA 设备一个实体，每个播放组一个实体 |
| 实时状态 | WebSocket 长连接推送（`local_push`），断线自动重连，另有 30s 轮询兜底 |
| 媒体浏览 | 浏览歌单 / 专辑 / 艺术家 / 流派，带封面 |
| 搜索 | 媒体浏览器内直接搜索曲库（需 HA 2025.5+） |
| 媒体源 | MusicFlow 出现在全局「媒体」标签页，可推给**任意** HA 播放器 |
| 群组（只读镜像） | MusicFlow 播放组以普通播放器形式出现，组合编辑请在服务器上进行 |
| 音量与静音 | 每设备独立音量与静音，双向实时同步 |
| 播报 / TTS | 打断当前播放 → 播报 → 自动回到原曲原进度 |
| 输出设备切换 | 把当前队列和播放进度整体转移到另一个音箱 |
| 电源 | 为没有物理开关的设备提供软开关 |
| 封面代理 | 封面经 HA 代理下发，外网访问 HA 时也能正常显示 |
| Zeroconf | 自动发现 MusicFlow 服务端，IP 变化自动更新 |
| 自动重认证 | API Key 失效时 HA 会弹出重新认证流程 |

Home Assistant 只充当**远程控制器**，音频流始终在 MusicFlow 后端与 DLNA 设备之间直连，不经过 HA。

---

## 版本要求

| 集成版本 | MusicFlow 服务端 | Home Assistant |
|---|---|---|
| **1.3.2** | **1.1.7** 及以上 | 2024.12 及以上 |
| 1.3.0+ | 1.1.7 及以上 | 2024.12 及以上 |

说明：

- 曲库内**搜索框**需要 Home Assistant **2025.5** 及以上。老核心上其他功能照常，只是不显示搜索框。
- **静音、播报（TTS）、输出设备切换**以及更完整的曲目元数据，需要 MusicFlow 服务端 **1.1.7** 及以上。
- 不确定服务端版本？打开 `http://<MusicFlow地址>/ping`，会返回 `{"status":"ok","version":"1.1.7"}`。

本集成**不引入任何额外 Python 依赖**，WebSocket 复用 HA 自带的 aiohttp。

---

## 安装

### 方式 A：通过 HACS（推荐）

1. 先装好 [HACS](https://hacs.xyz/)
2. HACS → 右上角 **⋮ → 自定义仓库**
3. 填入 `https://github.com/ray5378/hass-musicflow`，类别选 **Integration**
4. 搜索 **MusicFlow** → 下载
5. **重启 Home Assistant**
6. 设置 → 设备与服务 → 添加集成 → **MusicFlow**

也可以直接点上方徽章一键跳转。

### 方式 B：手动安装

把 `custom_components/musicflow/` 复制到 HA 的 `config/custom_components/musicflow/`，然后重启 HA。

---

## 配置

### 第一步：拿到 API Key

集成使用 API Key 作为长期凭据（登录 Token 24 小时过期，不适合常驻客户端）。两个入口任选：

- **给自己**：MusicFlow Web UI → **设置** → **API Key** → **生成** → **复制**
- **给指定用户**（管理员）：**管理 → 用户管理** → 对应用户卡片 → **API Key** → **生成** → **复制**

建议单独建一个 `homeassistant` 账号来发 Key，这样将来撤销时不影响自己日常登录。

> 改密码会自动使该用户的 Key 失效，所以请**先改密码，再生成 Key**。

### 第二步：添加集成

如果 MusicFlow 已在同网络运行（加载项或独立 Docker），集成会通过 Zeroconf 自动发现，地址自动填好，只需补 API Key。

手动配置时填写：

| 字段 | 说明 |
|---|---|
| URL | MusicFlow 地址，如 `http://192.168.1.10:46400`（省略协议和端口时按 `http` / `46400` 补全） |
| API Key | 上一步复制的 Key，形如 `mf_xxxxxxxx...` |
| 校验 SSL 证书 | 仅在使用 https 且证书为自签名时取消勾选 |

在 MusicFlow 里点「重新生成」或「撤销」会让旧 Key 立即失效，HA 侧会弹出重新认证提示，填入新 Key 即可。

---

## 实体与设备

集成会注册一个网关设备（MusicFlow 服务端本身），每个播放器实体挂在它下面：

- **DLNA 设备** → `media_player.<设备名>`
- **播放组** → `media_player.<组名>`，状态取自组内 leader 设备

MusicFlow 里新增或移除设备、播放组时，实体会在下一次刷新时自动增补，无需重启。

### 群组与多房间

MusicFlow 的播放组在 HA 里就是一个普通播放器实体，和 DLNA 渲染器一样，
播放 / 暂停 / 队列 / 音量控制作用于整组。

集成对群组成员关系是**只读**的：创建组、加设备、移除设备都在 MusicFlow
服务器上进行，HA 实体会自动镜像，无需重启。集成不再提供分组 / 退组界面，
因此 HA 卡片永远不会改动你在服务器上配置的群组。

**音量仍然是每设备独立的**，可以单独平衡各个音箱。

### 媒体源（全局「媒体」标签页）

集成注册了 media source，因此 MusicFlow 曲库也会出现在 HA 的全局**「媒体」**标签页。从那里可以把歌曲推给**任意** HA 播放器（Google Cast、Sonos、其他 DLNA 集成等），不局限于 MusicFlow 自己的实体。直链播放同样用 API Key 鉴权。

---

## 服务

除标准 `media_player.*` 服务外，另提供三个专用服务：

| 服务 | 说明 |
|---|---|
| `musicflow.play_content` | 按内容类型播放（song / album / playlist / artist / genre），可指定起始位置、播放模式、是否追加 |
| `musicflow.set_play_mode` | 设置播放模式：`order` 顺序 / `shuffle` 随机 / `one` 单曲循环 / `all` 列表循环 |
| `musicflow.clear_queue` | 清空播放队列 |

```yaml
service: musicflow.play_content
target:
  entity_id: media_player.living_room_speaker
data:
  content_type: playlist
  content_id: "12"
  play_mode: shuffle
```

### 播报 / TTS

`announce: true` 会打断当前播放，播完提示音后**自动回到原曲原进度**：

```yaml
service: media_player.play_media
target:
  entity_id: media_player.living_room_speaker
data:
  media_content_id: media-source://tts/tts.google_translate_say?message=开饭了
  media_content_type: music
  announce: true
  extra:
    volume: 0.6
```

---

## media_content_id 格式

自动化里调用 `media_player.play_media` 时支持：

| URI | 行为 |
|---|---|
| `musicflow://song/<id>` | 播放单曲 |
| `musicflow://album/<id>` | 整张专辑入队播放 |
| `musicflow://playlist/<id>` | 歌单入队播放 |
| `musicflow://artist/<id>` | 艺术家全部曲目入队播放 |
| `musicflow://genre/<名称>` | 该流派曲目入队播放 |

也可以用 `media_content_type: playlist` + `media_content_id: "12"` 这种简写。
带 `enqueue: add` 参数时为追加到队列尾部，而非立即替换。

---

## 架构

```
HA 仪表盘（控制面）
  |
  +-- media_player.<peer>
        |
        +-- 集成（Python，本仓库）
              |
              +-- REST  -> /rest/api/v1/...   peer 状态 / 队列 / 播放控制
              +-- REST  -> /rest/...          OpenSubsonic 曲库浏览与封面
              +-- WS    <- /ws                播放状态实时推送
                              |
                              +-- SOAP -> DLNA 设备（真正发声）
```

---

## 排查

| 现象 | 处理 |
|---|---|
| 配置完没有实体 | 确认 MusicFlow 里至少有一个在线的 DLNA 设备或播放组 |
| 反复要求重新认证 | API Key 被撤销或密码被改过，重新生成一个 Key |
| 外网访问时没有封面 | 需要集成 1.2.0+，封面会经 HA 代理下发 |
| 外网访问时卡片显示异常/变暗 | 需要集成 1.3.0+ 与卡片 v1.6.0+，卡片在直连失败时自动切换为经 HA 中转（REST + 事件订阅） |
| 看不到搜索框 | 需要 Home Assistant 2025.5 及以上 |
| 静音 / 播报无效 | 需要 MusicFlow 服务端 1.1.7 及以上 |

打开调试日志：

```yaml
logger:
  default: warning
  logs:
    custom_components.musicflow: debug
```

---

## 相关仓库

| 仓库 | 作用 |
|---|---|
| [MusicFlow](https://github.com/ray5378/MusicFlow) | 音乐服务端本体（后端 + Web UI） |
| [hassio-addons](https://github.com/ray5378/hassio-addons) | HA 加载项，把 MusicFlow 跑在 Supervisor 下 |
| hass-musicflow（本仓库） | HACS 自定义集成 |
| [hass-musicflow-card](https://github.com/ray5378/hass-musicflow-card) | Lovelace 卡片：喜欢 / 添加到歌单 / 滚动歌词 / 切换输出设备（需集成 1.2.6+） |

## 许可证

基于 [MIT License](LICENSE) 发布。

[my-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[my-link]: https://my.home-assistant.io/redirect/hacs_repository/?owner=ray5378&repository=hass-musicflow&category=integration
