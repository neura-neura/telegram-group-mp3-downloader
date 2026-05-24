<p align="center">
  <img src="assets/icon.png" width="180" alt="Telegram Group Audio Downloader icon">
</p>

# Telegram Group Audio Downloader

A Telegram bot that catalogs audio files posted in a group and downloads the eligible files to a server folder.

It tracks `.mp3`, `.m4a`, `.flac`, and `.wav` files, reports totals by type and size, and downloads only the file types that are safe to fetch through the public Telegram Bot API.

## What It Does

- Catalogs audio files seen by the bot in Telegram groups.
- Reports total file count and total size, both overall and by extension.
- Downloads missing `.mp3` and `.m4a` files up to the configured size limit.
- Skips `.flac`, `.wav`, and oversized files with a clear skipped-file summary.
- Edits one Telegram message repeatedly while downloads are running.
- Supports group-specific persistent download folders.
- Supports persistent scheduled scans and downloads through SQLite.
- Lets you manage a group from a private chat with the bot.
- Runs as a `systemd` service and starts automatically after reboot.

## Telegram Limitations

With a normal bot token, Telegram does not let bots read the full historical chat log. The bot can only catalog files it receives as updates after it has been added to the group and is allowed to read messages.

For best results, add the bot as an admin or disable Privacy Mode in BotFather and re-add it to the group.

The public Telegram Bot API `getFile` endpoint can download files up to 20 MB. This project therefore downloads only `.mp3` and `.m4a` files up to `MAX_DOWNLOAD_BYTES`. Other tracked files remain visible in scans and are listed as skipped during downloads.

## Commands

### General

- `/ayuda` or `/help` - show commands.
- `/whoami` - show your Telegram user ID and current chat ID.
- `/info_grupo` - show the selected group information.
- `/grupos` - list groups known by the bot.
- `/usar_grupo <id>` - choose which group private-chat commands should target.

### Scan And Download

- `/scan` or `/resumen` - show known audio totals.
- `/descargar [folder]` - download missing eligible files.
- `/sincronizar [folder]` - scan and download missing eligible files.

If no folder is provided, the bot uses the group-specific persistent folder. If none is set, it uses `DEFAULT_DOWNLOAD_DIR`. If that is not configured either, it creates a folder under `DOWNLOAD_BASE`.

### Folders

- `/carpeta` - show the active download folder.
- `/carpeta <folder>` - persist a download folder for the selected group.
- `/carpeta reset` - remove the group-specific folder and fall back to the global default.

### Scheduling

- `/programar <minutes>` - schedule a recurring scan.
- `/programar <minutes> descargar [folder]` - schedule a recurring scan and download.
- `/programaciones` - list active schedules.
- `/cancelar_programacion <id>` - cancel a schedule.

Schedules are stored in SQLite and survive service restarts and server reboots.

## Private Chat Workflow

You can manage a group from a private chat with the bot.

1. Add the bot to the target group.
2. Make it admin, or disable Privacy Mode in BotFather.
3. Send any message or command in the group once so the bot registers it.
4. In private chat, run `/grupos`.
5. Select the group with `/usar_grupo <id>`.
6. Use `/info_grupo`, `/scan`, `/descargar`, `/sincronizar`, and scheduling commands privately.

If the bot knows only one group, it selects that group automatically.

## Configuration

Copy `.env.example` to `.env` and configure:

```env
BOT_TOKEN=put-your-token-here
DB_PATH=./data/bot.sqlite3
DOWNLOAD_BASE=./downloads
DEFAULT_DOWNLOAD_DIR=
ALLOWED_USER_IDS=
ALLOWED_CHAT_IDS=
ALLOW_ABSOLUTE_DOWNLOAD_PATHS=false
MAX_DOWNLOAD_BYTES=20971520
BOT_API_BASE_URL=https://api.telegram.org/bot
BOT_API_FILE_BASE_URL=https://api.telegram.org/file/bot
APP_TIMEZONE=America/Mexico_City
PROGRESS_EDIT_SECONDS=2.5
SCHEDULER_POLL_SECONDS=30
```

Important variables:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `DOWNLOAD_BASE`: base folder for relative download paths.
- `DEFAULT_DOWNLOAD_DIR`: global default target for downloads and syncs.
- `ALLOWED_USER_IDS`: optional comma-separated allowlist of Telegram user IDs.
- `ALLOWED_CHAT_IDS`: optional comma-separated allowlist of group IDs.
- `ALLOW_ABSOLUTE_DOWNLOAD_PATHS`: set to `true` to allow absolute download paths.
- `MAX_DOWNLOAD_BYTES`: per-file download limit. The public Bot API limit is 20 MB.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m telegram_group_mp3_downloader.bot
```

## systemd

The included service file expects the app at:

```text
/telegram-bots/telegram-group-mp3-downloader
```

Install it with:

```bash
cp systemd/telegram-group-mp3-downloader.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable telegram-group-mp3-downloader.service
systemctl start telegram-group-mp3-downloader.service
```

Check logs:

```bash
journalctl -u telegram-group-mp3-downloader.service -f
```
