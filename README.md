<p align="center">
  <img src="assets/icon.png" width="160" alt="Telegram Group Audio Downloader icon">
</p>

# Telegram Group Audio Downloader

Download group audio to your server and optionally send music links to a [Mornye](https://github.com/mornyeapp/Mornye-Python) CLI bridge. Capture your Spotify daylist, keep organized MP3 files and M3U playlists, and follow progress in one edited Telegram message.

## Features

- Catalog group audio in SQLite and download missing eligible files.
- Keep scheduled checks quiet when nothing changes; send concise summaries for new files or errors.
- Request skipped-file details with `/skipped` instead of receiving a long daily list.
- Organize Telegram downloads by year/month/day and maintain a relative `All Tracks.m3u`.
- Optionally handle supported song, album, playlist, and artist links through Mornye extensions.
- Capture the current Spotify daylist with `/daylist`, using a new dated M3U for every successful request.
- Convert provider audio to MP3 at 320 kbps, verify core tags, and remove intermediate FLAC files.
- Skip tracks already present in earlier daylists without copying, moving, or linking the audio into the new folder. The new M3U points to the original file.
- Detect completed requests, reject concurrent music jobs, and preserve completed files for retries.
- Run under `systemd`, using configurable paths and owner allowlists.

## Quick start: Telegram audio

Use Python 3.12 or newer:

```bash
git clone https://github.com/neura-neura/telegram-group-mp3-downloader.git
cd telegram-group-mp3-downloader
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set `BOT_TOKEN` and your `ALLOWED_USER_IDS`. Obtain IDs with `/whoami` during initial setup, then restrict access before leaving the bot online. Add the bot to the group as an admin, or disable Privacy Mode in BotFather and re-add it.

```bash
.venv/bin/python -m telegram_group_mp3_downloader.bot
```

Mornye is disabled by default and uses a separate environment. Follow **[Mornye installation and Spotify setup](docs/mornye.md)** to enable it.

## Commands

| Command | Purpose |
| --- | --- |
| `/help` | Show commands. |
| `/whoami` | Show your Telegram user ID and chat ID. |
| `/groups`, `/use_group <id>`, `/group` | Select and inspect a group from private chat. |
| `/scan` | Show known audio totals by file type. |
| `/download [folder]` | Download missing eligible Telegram files. |
| `/sync [folder]` | Scan and download missing Telegram files. |
| `/folder [path]`, `/folder reset` | Show, save, or clear a group-specific target. |
| `/schedule <minutes> [download] [folder]` | Schedule a scan or scan-and-download. |
| `/schedules`, `/cancel_schedule <id>` | List or cancel persistent schedules. |
| `/skipped` | Download a detailed skipped-file report on request. |
| `/spotify` | Show Spotify connection status and setup guidance. |
| `/daylist [Spotify playlist URL]` | Capture the current daylist; save a new URL in private chat. |
| A supported music URL | Start a Mornye download for the configured owner. |

Existing Spanish command aliases remain supported. Bot responses and the published command menu are in English.

## Library layout

```text
music/
├── Telegram/
│   ├── YYYY/MM/DD/
│   └── All Tracks.m3u
└── Spotify/
    ├── Daylists/YYYY-MM-DD/Daylist name - snapshot/
    ├── Albums/Artist/Album/
    ├── Singles/Artist/Album/
    ├── Playlists/Identifier/
    └── Artists/
```

Set `DOWNLOAD_BASE` to the Telegram directory and `MUSIC_ROOT` to its parent. Mornye collection playlists live at their category or collection root and reference audio by relative path.

M3U filenames describe their content:

```text
Album - Artist - Album title [identifier].m3u
Song - Artist - Song title [identifier].m3u
Playlist - Playlist name [identifier].m3u
Daylist - 2026-01-15 09-30-00 - Daylist name.m3u
```

Each `/daylist` request creates a new playlist file even if the snapshot and audio are unchanged. Requests in the same second receive a numeric suffix. Unavailable tracks are excluded; no empty M3U is created when every track fails. Keep the original audio folders when moving a collection of daylists: later M3Us can reference earlier folders.

## Repeat requests and progress

Music jobs edit one Telegram message approximately every three seconds, then replace it with the final summary. While a job runs, the same request points you back to its progress; another request asks you to wait and resend it afterward. There is no background queue. A process-level lock also prevents a terminal job from racing a bot job.

Completed requests are skipped only after their MP3 files pass format and core-tag checks. Missing files, changed collections, and incomplete reports allow another attempt. Daylists are always read again because their contents change. Daylist deduplication uses exact Spotify track IDs from previous successful reports, not titles alone.

## Configuration

See [`.env.example`](.env.example) for all settings.

| Setting | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram token from BotFather. Keep it private. |
| `ALLOWED_USER_IDS`, `ALLOWED_CHAT_IDS` | Telegram access restrictions. Empty lists leave the corresponding general Telegram restriction open. |
| `DB_PATH` | Persistent SQLite catalog and schedules. |
| `DOWNLOAD_BASE`, `DEFAULT_DOWNLOAD_DIR` | Base and optional default Telegram destination. Group-specific `/folder` settings take precedence. |
| `ALLOW_ABSOLUTE_DOWNLOAD_PATHS` | Allow destinations outside the configured base. Defaults to `false`. |
| `MAX_DOWNLOAD_BYTES` | Per-file Telegram download limit. |
| `BOT_API_BASE_URL`, `BOT_API_FILE_BASE_URL` | Telegram API endpoints; configurable for a local Bot API server. |
| `APP_TIMEZONE` | Time zone for dated folders, captures, and displayed schedules. Defaults to `UTC`. |
| `PROGRESS_EDIT_SECONDS`, `SCHEDULER_POLL_SECONDS` | Telegram progress interval and scheduling poll interval. |
| `MORNYE_ENABLED` | Enable the optional music bridge after installation. |
| `MUSIC_ALLOWED_USER_IDS` | Music owners; falls back to `ALLOWED_USER_IDS` when blank. Music access is denied if both are empty. |
| `MUSIC_ROOT`, `MORNYE_HOME`, `MORNYE_PYTHON` | Music library, private Mornye runtime, and worker interpreter. |
| `CHROME_PATH` | Chrome/Chromium executable used by provider verification. |

## Telegram limits

A bot cannot read an entire group's historical chat log. It catalogs updates it receives after joining and being allowed to read messages. The default public Bot API download limit is 20 MB: the bot tracks `.mp3`, `.m4a`, `.flac`, and `.wav`, but only downloads eligible `.mp3` and `.m4a` files through this route. Mornye provider downloads use a separate pipeline.

## Services, maintenance, and development

- [Deployment and troubleshooting](docs/deployment.md)
- [Mornye integration, registries, and OAuth](docs/mornye.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security and private data](SECURITY.md)

Offline tests use generated audio and mocked services. No account credentials are required:

```bash
.venv/bin/python -m pip install mutagen
# Install ffmpeg with your system package manager.
.venv/bin/python -m unittest discover -s tests -v
```

The bundled Mornye compatibility patches target version 1.0.4. Do not replace that environment with a newer release without reviewing the patches. Upstream Mornye code is MIT-licensed; its notice is included in [LICENSE-Mornye](integrations/mornye/LICENSE-Mornye).
