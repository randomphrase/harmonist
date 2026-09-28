# Configuration

Most settings can be changed on the **Settings** page, which saves them and
applies them straight away. This page is the full list, for anyone managing
Harmonist from a config file or environment variables.

Harmonist reads `harmonist.toml` from its config folder (`/config` in Docker,
otherwise `~/.config/harmonist/`) when it starts. Environment variables override
the file. Every setting is optional.

## `[paths]`

| Key | Env | Default | |
|---|---|---|---|
| `music_dir` | `HARMONIST_MUSIC_DIR` | `/music` in Docker | Your music library. Use an absolute path. |
| | `HARMONIST_CONFIG_DIR` | `/config` in Docker | Where `harmonist.toml`, the Bandcamp cookies, `ignores.txt`, the activity history and kept artwork live. |

Needs a restart.

## `[bandcamp]`

| Key | Env | Default | |
|---|---|---|---|
| `download_format` | `HARMONIST_DOWNLOAD_FORMAT` | `"flac"` | Format to download: `flac`, `alac`, `mp3-320`, `mp3-v0`, `aac-hi` or `vorbis`. |
| `max_downloads_per_sync` | `HARMONIST_MAX_DOWNLOADS_PER_SYNC` | `5` | New purchases downloaded per sync; the rest wait for the next one. `0` pauses downloads. |

Both are on the Settings page. Bandcamp's `wav` and `aiff` downloads can't be
tagged, so Harmonist won't see them.

## `[musicbrainz]`

| Key | Env | Default | |
|---|---|---|---|
| `user_agent` | | `"Harmonist/1.0 ( … )"` | Identifies you to MusicBrainz. Use `Name/Version ( your-email )`. On the Settings page. |
| `cache_ttl_seconds` | | `3600` | How long a release fetched from MusicBrainz is reused before asking again. Each album's page has a refresh button to ask now. |

## `[cover_art]`

| Key | Env | Default | |
|---|---|---|---|
| `cache_ttl_seconds` | | `604800` (a week) | How long a Cover Art Archive answer is reused. Each album's page has a refresh button. |
| `image_cache_max_bytes` | | `1073741824` (1 GB) | Disk space for the archive's images, kept under `artwork/caa` in the config folder so album pages can show them. The least recently viewed go first, and any that are dropped are downloaded again when needed, so the folder is safe to delete. `0` keeps none, and album pages then can't show or offer the archive's images. |

## `[artwork_store]`

Copies of artwork Harmonist has replaced, so you can undo the change.

| Key | Env | Default | |
|---|---|---|---|
| `keep_per_album` | | `5` | Artwork changes kept per album. |
| `max_bytes` | | `524288000` (500 MB) | Total size limit; the oldest copies go first. `0` keeps nothing, and Harmonist then won't replace any artwork, since it couldn't undo it. |

## `[tagging]`

| Key | Env | Default | |
|---|---|---|---|
| `folder_cover` | `HARMONIST_TAGGING_FOLDER_COVER` | `"never"` | `"if_missing"` creates a `cover.jpg` for albums without one. An existing cover is still offered a better image either way. |
| `transforms` | `HARMONIST_TAGGING_TRANSFORMS` | `[]` | Optional changes to what's written. The only one is `"album_disambiguation"`, which adds MusicBrainz's edition note to the album title, as Picard can: *Selected Ambient Works Volume II (expanded edition)*. |

Both are on the Settings page under **Tagging**. A transform applies to future
taggings; each album offers the change on its own page rather than all at once.

## `[gardener]`

| Key | Env | Default | |
|---|---|---|---|
| `level` | `HARMONIST_GARDENER_LEVEL` | `"off"` | `"review"` checks your library against MusicBrainz in the background and reports updates. It never writes to files. On the Settings page as **Update checks**. |

## `[library]`

| Key | Env | Default | |
|---|---|---|---|
| `watch_settle_seconds` | `HARMONIST_WATCH_SETTLE_SECONDS` | `5` | After files change, how long to wait for quiet before rescanning. Only applies on a local disk; network shares are rescanned hourly. |

## `[server]`

| Key | Env | Default | |
|---|---|---|---|
| `host` | `HARMONIST_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Address to listen on. |
| `port` | `HARMONIST_PORT` | `8000` | Port to listen on. |
| `allowed_hosts` | `HARMONIST_ALLOWED_HOSTS` | `["*"]` | Hostnames Harmonist answers to. The env var is comma-separated. See [security](security.md#limit-the-hostnames-it-answers-to). |

Needs a restart.

## `[auth]`

| Key | Env | Default | |
|---|---|---|---|
| `enabled` | `HARMONIST_AUTH_ENABLED` | `false` | Turns on the built-in password. |
| `username` | `HARMONIST_AUTH_USERNAME` | | |
| `password_hash` | `HARMONIST_AUTH_PASSWORD_HASH` | | See [security](security.md#built-in-password) to generate it. |

Needs a restart.

## Other

| Key | Env | Default | |
|---|---|---|---|
| `log_level` | `HARMONIST_LOG_LEVEL` | `"info"` | `debug`, `info`, `warning` or `error`. On the Settings page. |
| `demo_mode` | `HARMONIST_DEMO_MODE` | `false` | Runs the sample library with simulated services. Your music folder is never touched. |

## Example

```toml
[bandcamp]
download_format = "flac"
max_downloads_per_sync = 10

[musicbrainz]
user_agent = "Harmonist/1.0 ( you@example.com )"

[gardener]
level = "review"

[server]
allowed_hosts = ["harmonist.example.com"]
```
