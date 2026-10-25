# Mornye integration

The Telegram bot launches a separate CLI worker. Playlist metadata and audio downloads go through Mornye and its configured extensions; a saved Spotify OAuth token is used only as a fallback for playlist metadata. It is not passed to the audio CLI or provider gateway.

## Requirements

Use Linux, Python 3.12+, Node.js 22+, ffmpeg, Chrome/Chromium, Xvfb, and the `patch` utility. Browser verification can consume substantial memory; keep enough available RAM or swap for your server's workload. This integration does not install a remote desktop or expose a browser port.

On Debian/Ubuntu, install the available system packages, then install Node.js and Chrome/Chromium using your distribution or their official installers:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv ffmpeg xvfb patch
node --version
ffmpeg -version
```

Set `CHROME_PATH` in `.env` to the installed browser executable. The service account must be able to execute it and write to its own home and runtime directories.

## Install the separate environment

Run from the repository root, as the user that will run the services:

```bash
python3 -m venv runtime/mornye/.venv
runtime/mornye/.venv/bin/python -m pip install -r integrations/mornye/requirements.txt
runtime/mornye/.venv/bin/python integrations/mornye/install.py --install-extensions
```

The installer checks for Mornye **1.0.4**, validates all patches before applying them, saves original upstream files locally, and records installed file hashes. Re-running it checks the installed files instead of reapplying patches. An upgrade that changes patched files requires recreating and reviewing the environment.

Review third-party extensions before enabling installation. `--install-extensions` adds both registries and installs unique extension IDs, using Mornye's checksum-only trust policy:

- [Mornye Extension registry](https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry.json)
- [SpotiFLAC Extension registry](https://raw.githubusercontent.com/zarzet/SpotiFLAC-Extension/main/registry.json)

If registries advertise the same ID, the first registry takes precedence. Provider availability and extension behavior can change independently of this repository.

## Enable and start services

Set these values in `.env`:

```env
MORNYE_ENABLED=true
MUSIC_ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
MUSIC_ROOT=./downloads/music
MORNYE_HOME=./runtime/mornye
MORNYE_PYTHON=./runtime/mornye/.venv/bin/python
CHROME_PATH=/usr/bin/google-chrome
```

For `systemd`, install and adapt both templates in `systemd/` as described in [deployment.md](deployment.md). Start `mornye-gateway` before requesting provider verification. Its private Unix socket must match the worker's `MORNYE_HOME/state/gateway.sock`.

For a foreground gateway, export the same environment as the bot and set absolute runtime paths:

```bash
set -a
. ./.env
set +a
export MORNYE_HOME="$(pwd)/runtime/mornye"
export MORNYE_DATA_DIR="$MORNYE_HOME/mornye-data"
export MORNYE_BROWSER_SOCKET="$MORNYE_HOME/state/gateway.sock"
runtime/mornye/.venv/bin/python -m mornye.core.server_browser_network
```

Use another terminal, from the repository root, to verify providers:

```bash
runtime/mornye/.venv/bin/python integrations/mornye/mornye_verify.py tidal-web
runtime/mornye/.venv/bin/python integrations/mornye/mornye_verify.py qobuz-web
runtime/mornye/.venv/bin/python integrations/mornye/mornye_verify.py deezer
runtime/mornye/.venv/bin/python integrations/mornye/mornye_verify.py amazon
```

The verification helper loads `.env`. Respect any reported backoff interval. Providers can require interactive verification or refuse requests; the integration reports pending verification instead of guaranteeing access.

## Connect Spotify with PKCE

In Spotify for Developers, create your own app and enable the Web API. For example:

- App name: `My Music Library Bot`
- Description: `Read my playlists for my private music library.`
- Website: optional.
- Redirect URI: **`http://127.0.0.1:8765/callback`**

Use your app's Client ID. No client secret, password, or Spotify cookies are required.

From your computer, open an SSH tunnel to your server:

```bash
ssh -L 8765:127.0.0.1:8765 USER@YOUR_SERVER
```

In that server session, from the repository root:

```bash
runtime/mornye/.venv/bin/python integrations/mornye/spotify_login.py YOUR_CLIENT_ID
```

Open the printed authorization URL in the browser on the computer running the tunnel. Spotify redirects to the loopback callback. The helper validates OAuth state, exchanges the code using PKCE, and stores the token with file mode `0600` in a `0700` state directory. It expires the login attempt after ten minutes.

In a private Telegram chat, send `/daylist <your full Spotify daylist URL>`. Future `/daylist` commands read the playlist's current contents. If Spotify restricts its personalized shared view, save a copy as a playlist you own and send that URL instead.

## Files and playlists

- MP3 conversion requests 320 kbps and retains the available metadata. Final files are checked for MP3 format, bitrate, title, artist, and album tags.
- A lossy source converted to 320 kbps does not gain the fidelity of a higher-quality source.
- Intermediate FLACs are removed after successful conversion. FLACs created by a failed or normally canceled job are cleaned up; unrelated pre-existing originals are preserved. A forced kill or power loss can interrupt cleanup.
- Daylists retain a snapshot and CSV, plus a report and a newly dated M3U for each request that has playable tracks. Audio already present in earlier daylists stays in its original location.
- Other collections use descriptive M3U filenames with a stable identifier to avoid collisions. Music requests are serialized; the bot does not queue additional jobs.

## Compatibility patches

The reviewable diffs in `integrations/mornye/patches/` cover:

1. Existing source files that still need tagging and conversion.
2. Reporting skipped existing files during CSV synchronization so M3Us remain complete on retries.
3. Provider-supported authorization callbacks, strict grant capture, and bounded authentication recovery.
4. A shared browser transport and cancellation of abandoned gateway requests.

The gateway checks provider origins and uses a private local socket. It does not disable TLS checks or recording-identity validation. There is no guarantee against account suspension, provider restrictions, or changes in service behavior. Keep your Spotify authorization private and review the extensions you install.
