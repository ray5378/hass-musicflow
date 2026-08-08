# MusicFlow

自托管音乐库播放器 [MusicFlow](https://github.com/ray5378/MusicFlow) 的 Home Assistant 集成。

把 MusicFlow 管理的 **DLNA 设备**和**播放组**暴露为 HA `media_player` 实体:

- 播放 / 暂停 / 上下一首 / 进度 / 音量 / 循环 / 随机 / 清空队列
- 在 HA 媒体浏览器里浏览歌单、专辑、艺术家、流派(带封面)
- WebSocket 实时状态推送,Zeroconf 自动发现

安装后前往 **设置 → 设备与服务 → 添加集成 → MusicFlow**,填写服务器地址与 API Key
(若 MusicFlow 已在同一网络运行,通常会被自动发现)。

详见 [README](https://github.com/ray5378/hass-musicflow#readme)。
