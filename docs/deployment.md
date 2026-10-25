# Deployment and troubleshooting

## Persistent service account

The templates use `/opt/telegram-group-mp3-downloader` and an unprivileged `musicbot` account. Adapt paths and the account to your installation. Give that account write access to the configured library, `data/`, and `runtime/` directories.

```bash
sudo useradd --system --create-home --home-dir /var/lib/musicbot musicbot
sudo mkdir -p /opt/telegram-group-mp3-downloader
# Put the checkout here, then assign ownership before installing its environments.
sudo chown -R musicbot:musicbot /opt/telegram-group-mp3-downloader
```

Create the bot environment and optional Mornye environment as that user. Copy `.env.example` to `.env`, enter your own values, and restrict its permissions:

```bash
chmod 600 .env
sudo cp systemd/telegram-group-mp3-downloader.service /etc/systemd/system/
# Only when the optional Mornye integration is installed:
sudo cp systemd/mornye-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mornye-gateway
sudo systemctl enable --now telegram-group-mp3-downloader
```

For Telegram-only deployments, omit the gateway steps. If you change `MORNYE_HOME`, update the gateway executable, socket, and data directory in its service file to match. The templates intentionally contain example paths, not a particular server's mount, host, or credentials.

## Inspect and restart

```bash
systemctl status telegram-group-mp3-downloader mornye-gateway
journalctl -u telegram-group-mp3-downloader -f
journalctl -u mornye-gateway -f
```

Restart the bot after configuration or code changes. Wait for downloads to finish first. Do not start another bot polling process with the same Telegram token.

## Back up

Back up the SQLite database, library, and private runtime state securely. Stop the bot first or use SQLite's backup API for a consistent catalog. OAuth refresh tokens and provider sessions are credentials: never put these backups in Git or an issue attachment. Keep earlier daylist folders because newer M3Us may reference their audio.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No group audio appears | Bot membership, Privacy Mode, permissions, and whether the file was posted after the bot joined. |
| Music commands are denied | `MORNYE_ENABLED`, `MUSIC_ALLOWED_USER_IDS`, and general Telegram allowlists. |
| Provider verification is pending | Gateway service, browser path, Node.js, available memory, provider state, and reported backoff. |
| Spotify authorization fails | Your app's Client ID, exact loopback Redirect URI, SSH tunnel, OAuth callback, and developer-app access restrictions. |
| A repeated request downloads again | Its previous report was incomplete, the collection changed, or its files are missing or invalid. |
| A daylist M3U has missing paths | An older audio folder was removed or the relative folder structure changed. Retry to recover missing files. |
| A job times out or returns partial results | Completed files remain. Check private logs and retry pending tracks. |
| Storage error | Available disk space, mount health, ownership, and write permissions. |

Mornye diagnostics are stored under `MORNYE_HOME/logs/`. Treat logs as private because provider diagnostics can contain URLs or request details. Share only a redacted error excerpt.
