# 发版规范(全系列仓库通用)

> 2026-10-08 用户定调。适用:MusicFlow-client / MusicFlow(服务端) / hass-musicflow-card / hass-musicflow / rcloneflow,以及后续新增仓库。

## 1. 版本号格式与上限

- 格式:`vX.Y.Z`——tag 与产物版本一律数字,禁止分支名当版本(既有守卫)。
- **每一段取值范围 0–99**:不允许出现 `v5.1.100` 这类三位数段。
- 进位规则:某段到 99 仍需升级时向上一段进位——
  - patch 到 99:`v5.1.99` 之后 → `v5.2.0`
  - minor 到 99:`v5.99.z` 之后 → `v6.0.0`
- 守卫:三处版本守卫脚本已加入段上限校验(R4),超限的 tag / 版本字段直接判红——
  - MusicFlow-client:`tool/check-version-guard.mjs`
  - MusicFlow(服务端):`backend/scripts/check-release-version.mjs`
  - hass-musicflow-card:`tools/check-version-guard.mjs`

## 2. 升级语义与发版节奏

| 段 | 语义 | 节奏约束 |
|---|---|---|
| Z(patch) | 缺陷修复 / 小质量改进 | 随时可发,不设频率上限 |
| Y(minor) | 功能 / 改进**集合** | **攒批发**:同一天多个功能合入同一个 minor;一天原则上最多发一个 minor,不为单个功能单独跳 minor |
| X(major) | 不兼容重构 / 大版本 | 必须与用户确认后再打 |

背景:2026-10-08 一天内连发 v5.5.0 → v5.6.0 → v5.7.0,版本号消耗过快。此后新功能默认攒批进当前开发批次,minor 以「天」为单位克制推进。

## 3. 各仓发版流程 checklist

- **MusicFlow-client**:发版前预跑 9 项静态守卫 + `flutter analyze lib/`;CHANGELOG(LF)→ push main → `git tag -a vX.Y.Z` → push tag → 8 workflow 全绿 → Release 产物(APK + Windows setup)。
- **MusicFlow(服务端)**:CHANGELOG(CRLF,条目间恰 1 空行)→ tag v* → build.yml(全量测试门禁)→ ghcr + DockerHub 双镜像 → 240 部署(pull → compose up -d → /ping)。
- **hass-musicflow-card**:改 `CARD_VERSION` → tag vX.Y.Z → 卡片发布流程。
- **hass-musicflow / rcloneflow**:沿用既有 tag 发版,遵守第 1、2 节。
- **插件仓(MusicFlow-plugins 等)**:CI 自动打 tag,禁手动打。
