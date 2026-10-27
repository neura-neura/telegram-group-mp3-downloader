# Mornye integration

The Telegram bot launches a separate CLI worker. Playlist metadata and audio downloads go through Mornye and its configured extensions; the dedicated Spotify web session is used only to discover the current personalized daylist share link. Browser cookies and account tokens are never passed to Mornye or its audio providers. The official account API is not used for daylist metadata.

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

## Automatically find the current daylist

A shared daylist URL can refer to a particular capture. Reusing yesterday's URL does not reliably select the current list. `/daylist` therefore opens Spotify's fixed daylist entry in a dedicated browser, uses **Share → Copy playlist link** to resolve the current personalized URL, and passes that public link to Mornye. It compares visible Spotify track IDs with Mornye's result before downloading. It never falls back to the saved URL when discovery fails.

Install Chromium/Chrome and Xvfb. Adapt and enable `systemd/mornye-spotify-display.service` and `systemd/mornye-spotify-browser.service` for your service user, executable path, and runtime directory. Create `runtime/mornye/state/spotify-browser` with mode `0700`. Keep the browser CDP endpoint bound to loopback. `MORNYE_SPOTIFY_BROWSER_URL` defaults to `http://127.0.0.1:9223`.

Sign in once in this dedicated server browser using a secured remote desktop over an SSH tunnel. The browser uses display `:100`. Any VNC listener must bind to loopback and require authentication; never expose VNC or CDP to the internet. Enter your password and any verification codes directly into Spotify, never into Telegram, an environment file, or this repository. The browser profile contains the session and must remain private. After discovery, the tab returns to a blank page to reduce idle memory use.

Run a discovery check from the repository root:

```bash
runtime/mornye/.venv/bin/python integrations/mornye/daylist_discovery.py
```

Then send `/daylist` in Telegram. Every request resolves the current URL again and creates a dated M3U. No Client ID or official Spotify API authorization is needed for this flow. If Spotify expires the browser session, sign in again; the bot reports the problem rather than relabeling a stale capture. `/daylist <playlist URL>` remains an explicit capture of that specific URL for that request, and does not disable automatic discovery on the next plain `/daylist`.

The older `spotify_login.py` PKCE helper remains available for independent API experiments. Its token is not used by the daylist path. Spotify page changes, provider availability, and account restrictions can still require maintenance; this integration does not guarantee account safety or uninterrupted service.

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

A single-track album and its individual song share one static M3U when the exact track ID and recording metadata match. The first valid export is retained; existing duplicate song/album exports are consolidated without moving or deleting audio. Daylist captures and user playlists remain separate.

## Provider browser recovery

The shared gateway starts its browser only when a user request needs it. After fifteen minutes without requests, the next request replaces the old browser before contacting a provider. A local renderer check also detects unresponsive tabs. Timed-out signed requests invalidate the connection and are not replayed automatically.

Provider authentication is attempted once per shared session, even when several audio extensions use it. Transport failures are reported separately from pending verification; provider `Retry-After` and authentication safety pauses remain enforced. Restarting the gateway alone does not authenticate, fetch a daylist, or download audio. After updating the integration files, rerun `install.py` to update the gateway module and restart its service.
