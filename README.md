# MusicFlow for Home Assistant

[![HACS Custom][hacs-badge]][hacs-link]
[![Release][release-badge]][release-link]
[![Home Assistant][ha-badge]][ha-link]

Home Assistant custom integration for [MusicFlow][musicflow], a self-hosted music
library server with DLNA / UPnP playback and multi-room sync groups.

Every DLNA renderer and every sync group managed by MusicFlow becomes a native
Home Assistant `media_player` entity, with real-time state over WebSocket.

> Chinese documentation is available in [README.zh-CN.md](README.zh-CN.md).

[![Open your Home Assistant instance and open this repository inside HACS.][my-badge]][my-link]

---

## Highlights

| Capability | What you get |
|---|---|
| Media player entities | One entity per DLNA device, one per sync group |
| Real-time state | WebSocket push (`local_push`), auto-reconnect, 30s polling fallback |
| Media library | Browse playlists / albums / artists / genres with cover art |
| Search | In-library search from the media browser (HA 2025.5+) |
| Media source | MusicFlow shows up in the global **Media** tab, playable on ANY HA player |
| Sync groups (read-only) | MusicFlow groups appear as normal players; edit them on the server |
| Volume + mute | Independent per-device volume and mute, two-way synced |
| Announce / TTS | Interrupt playback, speak, then resume the original track and position |
| Source select | Move the current queue and playback position to another speaker |
| Power | Soft power on/off for devices without a real power switch |
| Cover art proxy | Artwork is proxied through HA, so it works from outside your LAN |
| WAN proxy for the card | The MusicFlow dashboard card falls back to HA (REST + events) when it cannot reach the backend directly, e.g. outside your LAN |
| Zeroconf | Auto-discovery of the MusicFlow server, IP changes are picked up |
| Re-auth | If the API key is revoked, HA prompts you to enter a new one |

Home Assistant only acts as a remote control. Audio always streams directly from
the MusicFlow backend to the DLNA device and never passes through HA.

---

## Requirements

| Integration | MusicFlow server | Home Assistant |
|---|---|---|
| **1.3.0** | **1.1.7** or newer | 2024.12 or newer |
| 1.2.x | 1.1.7 or newer | 2024.12 or newer |

Notes:

- The in-library **search box** needs Home Assistant **2025.5** or newer. On older
  cores everything else still works, only the search field is hidden.
- Mute, announce (TTS), source select and the richer track metadata need
  MusicFlow server **1.1.7** or newer.
- Not sure which server version you run? Open `http://<musicflow-host>/ping`.
  It returns `{"status":"ok","version":"1.1.7"}`.

This integration pulls in **no extra Python dependencies**. The WebSocket client
reuses the aiohttp session that Home Assistant already ships.

---

## Installation

### Option A - HACS (recommended)

1. Install [HACS](https://hacs.xyz/) if you do not have it yet.
2. Open **HACS** and use the top-right menu -> **Custom repositories**.
3. Add `https://github.com/ray5378/hass-musicflow` with category **Integration**.
4. Search for **MusicFlow** and download it.
5. **Restart Home Assistant.**
6. Go to **Settings -> Devices & Services -> Add Integration -> MusicFlow**.

Or just click the badge at the top of this page to jump straight there.

### Option B - Manual

Copy `custom_components/musicflow/` into your Home Assistant
`config/custom_components/musicflow/` directory and restart Home Assistant.

---

## Configuration

### 1. Create an API key

The integration authenticates with an API key. Login tokens expire after 24
hours, so they are not suitable for a long-lived client.

- **For yourself:** MusicFlow web UI -> **Settings** -> **API Key** -> **Generate** -> **Copy**
- **For another user (admin):** **Admin -> User management** -> pick the user ->
  **API Key** -> **Generate** -> **Copy**

Tip: create a dedicated `homeassistant` account and issue the key from there.
Revoking it later will then not affect your own daily login.

> Changing a user password invalidates that user's keys, so **change the password
> first, then generate the key.**

### 2. Add the integration

If MusicFlow is already running on the same network (add-on or standalone Docker),
Zeroconf discovery fills in the address for you and you only need the API key.

When configuring manually:

| Field | Description |
|---|---|
| URL | MusicFlow address, e.g. `http://192.168.1.10:46400`. Missing scheme/port default to `http` and `46400`. |
| API Key | The key you copied, it looks like `mf_xxxxxxxx...` |
| Verify SSL certificate | Only uncheck this when using https with a self-signed certificate |

If you regenerate or revoke the key in MusicFlow, the old one stops working
immediately and Home Assistant raises a re-authentication prompt.

---

## Entities and devices

The integration registers one gateway device (the MusicFlow server itself) and
attaches every player entity to it:

- **DLNA device** -> `media_player.<device_name>`
- **Sync group** -> `media_player.<group_name>`, state derived from the group leader

Adding or removing devices and groups in MusicFlow is picked up automatically on
the next refresh, no restart required.

### Sync groups and multi-room

A MusicFlow sync group shows up as one normal media player entity, just like a
DLNA renderer. Play, pause, queue and volume controls act on the whole group.

The integration is **read-only** about group membership: create groups and
add / remove devices on the MusicFlow server, and the HA entities mirror that
automatically on the next refresh. There is no join / unjoin UI, so the HA
card can never modify your server-side group configuration.

Volume stays **per device**, so you can still balance individual speakers.

### Media source

Because the integration registers a media source, your MusicFlow library also
appears under the global **Media** tab. From there you can send a track to
**any** Home Assistant player, not just MusicFlow entities. Direct stream links
are authenticated with the same API key.

---

## Services

Standard `media_player.*` services all work. Three extra services are provided:

| Service | Description |
|---|---|
| `musicflow.play_content` | Play by content type (song / album / playlist / artist / genre), with optional start index, play mode and enqueue |
| `musicflow.set_play_mode` | Set play mode: `order`, `shuffle`, `one` (repeat one), `all` (repeat all) |
| `musicflow.clear_queue` | Clear the playback queue |

```yaml
service: musicflow.play_content
target:
  entity_id: media_player.living_room_speaker
data:
  content_type: playlist
  content_id: "12"
  play_mode: shuffle
```

### Announce / TTS

`announce: true` interrupts the current track, plays the message, then resumes
the original track at the original position.

```yaml
service: media_player.play_media
target:
  entity_id: media_player.living_room_speaker
data:
  media_content_id: media-source://tts/tts.google_translate_say?message=Dinner+is+ready
  media_content_type: music
  announce: true
  extra:
    volume: 0.6
```

---

## media_content_id formats

When calling `media_player.play_media` from an automation:

| URI | Behaviour |
|---|---|
| `musicflow://song/<id>` | Play a single track |
| `musicflow://album/<id>` | Queue and play a whole album |
| `musicflow://playlist/<id>` | Queue and play a playlist |
| `musicflow://artist/<id>` | Queue and play every track by the artist |
| `musicflow://genre/<name>` | Queue and play every track in the genre |

The shorter form `media_content_type: playlist` + `media_content_id: "12"` also
works. Pass `enqueue: add` to append to the queue instead of replacing it.

---

## Architecture

```
HA dashboard (control plane)
  |
  +-- media_player.<peer>
        |
        +-- this integration (Python)
              |
              +-- REST  -> /rest/api/v1/...  peer state, queue, transport control
              +-- REST  -> /rest/...         OpenSubsonic browsing and cover art
              +-- WS    <- /ws               real-time playback state push
                              |
                              +-- SOAP -> DLNA device (actual audio output)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No entities after setup | Confirm MusicFlow has at least one online DLNA device or group |
| Re-auth prompt keeps coming back | The API key was revoked or the password changed, generate a new key |
| No cover art when away from home | Requires integration 1.2.0+, artwork is proxied through HA |
| Search field missing | Requires Home Assistant 2025.5 or newer |
| Mute / announce do nothing | Requires MusicFlow server 1.1.7 or newer |

Enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.musicflow: debug
```

---

## Related repositories

| Repository | Purpose |
|---|---|
| [MusicFlow][musicflow] | The music server itself (backend + web UI) |
| [hassio-addons](https://github.com/ray5378/hassio-addons) | Home Assistant add-on, runs MusicFlow under Supervisor |
| hass-musicflow (this repo) | The HACS custom integration |
| [hass-musicflow-card](https://github.com/ray5378/hass-musicflow-card) | Lovelace card: favorite, add-to-playlist, scrolling lyrics, output switch (needs integration 1.2.6+) |

## License

Released under the [MIT License](LICENSE).

[musicflow]: https://github.com/ray5378/MusicFlow
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-link]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/ray5378/hass-musicflow?display_name=tag
[release-link]: https://github.com/ray5378/hass-musicflow/releases
[ha-badge]: https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5.svg
[ha-link]: https://www.home-assistant.io/
[my-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[my-link]: https://my.home-assistant.io/redirect/hacs_repository/?owner=ray5378&repository=hass-musicflow&category=integration
