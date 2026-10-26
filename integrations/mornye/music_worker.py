#!/usr/bin/env python3
"""Private CLI bridge. stdin/stdout JSON; provider diagnostics stay on disk."""
import csv
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import threading
import tempfile
import signal
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo
import httpx

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / '.env')

ROOT = Path(os.getenv('MORNYE_HOME', str(Path(__file__).resolve().parents[2] / 'runtime/mornye'))).expanduser().resolve()
STATE = ROOT / 'state'
MUSIC = Path(os.getenv('MUSIC_ROOT', str(Path.cwd() / 'downloads/music'))).expanduser().resolve()
REGISTRY = 'https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry.json'

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)

STREAM_PROGRESS = False
COLLECTION_NAMES = {}

def progress(stage, **fields):
    if STREAM_PROGRESS:
        print(json.dumps({'event': 'progress', 'stage': stage, **fields}, ensure_ascii=False), flush=True)

def valid_mp3(path):
    from mutagen.mp3 import MP3
    try:
        audio = MP3(path)
        return (path.suffix.lower() == '.mp3' and abs(audio.info.bitrate - 320000) <= 2000
                and audio.tags and all(tag in audio.tags for tag in ('TIT2', 'TPE1', 'TALB')))
    except Exception:
        return False

def existing_daylist_tracks(ids):
    # IDs come from completed Spotify daylist reports, never a title-only match.
    library = MUSIC / 'Spotify/Daylists'
    found = {}
    wanted = set(ids)
    for report_path in sorted(library.glob('*/*/report-*.json')):
        try:
            rows = json.loads(report_path.read_text()).get('tracks', [])
        except (OSError, ValueError):
            continue
        for row in rows:
            track_id = row.get('id')
            if track_id not in wanted or track_id in found or row.get('status') not in {'downloaded', 'skipped'}:
                continue
            path = Path(row.get('file_path') or '')
            if not path.resolve().is_relative_to(library.resolve()) or not valid_mp3(path):
                continue
            found[track_id] = {**row, 'status': 'skipped', 'provider': 'existing', 'error': None}
    return found

def completed_request(path, expected_ids):
    try:
        report = json.loads(path.read_text())
        rows = report['tracks']
        if not rows or report['summary'].get('failed', 0):
            return {}
        if expected_ids and {str(row.get('id')) for row in rows} != set(map(str, expected_ids)):
            return {}
        if not all(row.get('status') in {'downloaded', 'skipped'} and valid_mp3(Path(row.get('file_path') or '')) for row in rows):
            return {}
        return {row['id']: {**row, 'status': 'skipped', 'provider': 'existing', 'error': None} for row in rows}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}

def playlist_path(folder, daylist, name, category, entries, job_id, captured=None):
    first = next((row for row in entries if row.get('status') in {'downloaded', 'skipped'}), entries[0] if entries else {})
    artist = str(first.get('artists') or 'Unknown artist')
    album = str(first.get('album') or first.get('title') or 'Unknown album')
    title = str(first.get('title') or 'Unknown track')
    if daylist:
        stamp = (captured or datetime.now()).strftime('%Y-%m-%d %H-%M-%S')
        base = safe(f'Daylist - {stamp} - {name}')
    elif category == 'Albums':
        base = safe(f'Album - {artist} - {album}')
    elif category == 'Singles':
        base = safe(f'Song - {artist} - {title}')
    elif category == 'Artists':
        base = safe(f'Artist - {artist}')
    else:
        base = safe(f'Playlist - {name}')
    if not daylist:
        base = base[:120] + f' [{job_id}]'
    result = folder / (base + '.m3u')
    # Each daylist request gets its own capture, including repeated snapshots.
    suffix = 2
    while daylist and result.exists():
        result = folder / (base + f' - {suffix}.m3u')
        suffix += 1
    return result

def equivalent_single_track_playlist(category, entries):
    # A one-track release and its song should expose only one static playlist.
    # Daylists, user playlists, and multi-track releases keep separate exports.
    if category not in {'Albums', 'Singles'} or len(entries) != 1:
        return None
    current = entries[0]
    if current.get('status') not in {'downloaded', 'skipped'} or not current.get('id'):
        return None
    def identity(row):
        return tuple(str(row.get(key) or '').strip().casefold()
                     for key in ('id', 'title', 'artists', 'album', 'isrc'))
    root = MUSIC / 'Spotify'
    for kind in ('Albums', 'Singles'):
        folder = root / kind
        for report_path in sorted(folder.glob('report-*.json')):
            try:
                report = json.loads(report_path.read_text())
                rows = report['tracks']
                if (len(rows) != 1 or report.get('summary', {}).get('failed', 0)
                        or rows[0].get('status') not in {'downloaded', 'skipped'}
                        or identity(rows[0]) != identity(current)):
                    continue
                audio = Path(rows[0].get('file_path') or '')
                if not audio.resolve().is_relative_to(root.resolve()) or not valid_mp3(audio):
                    continue
                job = report_path.stem.removeprefix('report-')
                for playlist in sorted(folder.glob('*.m3u')):
                    if not playlist.stem.endswith(' [' + job + ']'):
                        continue
                    paths = [line for line in playlist.read_text(encoding='utf-8').splitlines()
                             if line and not line.startswith('#')]
                    if len(paths) == 1 and (playlist.parent / paths[0]).resolve() == audio.resolve():
                        return playlist
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return None

def cleanup_flacs(folder, before):
    # Only this run's intermediates or a verified MP3's leftover source.
    for path in folder.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == '.flac':
            if path not in before or valid_mp3(path.with_suffix('.mp3')):
                path.unlink(missing_ok=True)

def run_cli(command, env, folder, job_id, total, skipped):
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True, mode=0o700)
    before = {p for p in folder.rglob('*') if p.suffix.lower() == '.flac'}
    with tempfile.TemporaryDirectory(prefix='music-job-', dir=STATE) as temp:
        status = Path(temp) / 'progress.json'
        env = dict(env, MORNYE_PROGRESS_PATH=str(status), PYTHONPATH=str(Path(__file__).resolve().parent))
        command = [*command, '--post-hook', 'music_progress:on_track']
        stopped = threading.Event()
        def monitor():
            while not stopped.wait(2):
                try:
                    counts = json.loads(status.read_text())
                except (OSError, ValueError):
                    counts = {}
                progress('Downloading and converting to MP3', total=total,
                         downloaded=counts.get('downloaded', 0),
                         skipped=skipped + counts.get('skipped', 0),
                         attempts_failed=counts.get('failed', 0),
                         current=counts.get('current', ''))
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            with (logs / (datetime.now().strftime('%Y%m%d-%H%M%S-') + job_id + '.log')).open('w') as err:
                return subprocess.run(command, stdout=subprocess.PIPE, stderr=err, env=env, text=True, timeout=21000)
        finally:
            stopped.set()
            thread.join()
            cleanup_flacs(folder, before)

def safe(value):
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(value)).strip('. ')[:140] or 'Playlist'

def spotify_playlist(url):
    match = re.fullmatch(r'https://open\.spotify\.com/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]+)(?:\?[^\s]*)?', url)
    if not match:
        raise ValueError('Use the full daylist URL: https://open.spotify.com/playlist/…')
    return match.group(1)

def token():
    path = STATE / 'spotify_token.json'
    if not path.exists():
        raise ValueError('Spotify is not connected. Use /spotify for setup instructions.')
    data = json.loads(path.read_text())
    if data.get('expires_at', 0) <= time.time() + 60:
        response = httpx.post('https://accounts.spotify.com/api/token', data={
            'grant_type': 'refresh_token', 'refresh_token': data['refresh_token'], 'client_id': data['client_id']}, timeout=30)
        if response.status_code != 200:
            raise ValueError('The Spotify session expired. Run spotify_login.py again.')
        data.update(response.json())
        data['expires_at'] = time.time() + data['expires_in']
        save(path, data)
    return data['access_token']

def public_snapshot(url):
    # Read the playlist's shared public view; private playlists still require OAuth.
    import asyncio
    import hashlib
    os.environ['MORNYE_DATA_DIR'] = str(ROOT / 'mornye-data')
    os.environ['PATH'] = str(ROOT / '.venv/bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin')
    from mornye import AsyncMornye
    async def fetch():
        async with AsyncMornye(output_dir=str(ROOT / 'probe'), services=['ext:tidal-web']) as client:
            return await client.get_playlist(url)
    try:
        info, tracks = asyncio.run(fetch())
    except Exception:
        raise ValueError('Could not read the daylist through the API or shared URL. Save a copy as your own playlist, then send /daylist <copy URL>.')
    if not tracks:
        raise ValueError('The shared daylist returned no tracks. Save a copy as your own playlist.')
    songs = [{'id': track.id, 'type': 'track', 'name': track.title,
              'artists': [{'name': artist} for artist in (track.artist_names or [track.artists])],
              'album': {'name': track.album}, 'duration_ms': track.duration_ms,
              'external_urls': {'spotify': track.external_url}} for track in tracks]
    info['snapshot_id'] = hashlib.sha256('|'.join(song['id'] for song in songs).encode()).hexdigest()
    info['access_method'] = 'shared_link'
    return info, songs

def snapshot(url, prefer_account=False):
    playlist_id = spotify_playlist(url)
    if not prefer_account or not (STATE / 'spotify_token.json').exists():
        try:
            return public_snapshot(url)
        except ValueError:
            pass
    with httpx.Client(headers={'Authorization': 'Bearer ' + token(), 'Cache-Control': 'no-cache'}, timeout=45) as client:
        response = client.get('https://api.spotify.com/v1/playlists/' + playlist_id)
        if response.status_code in (403, 404):
            return public_snapshot(url)
        response.raise_for_status()
        info = response.json()
        info['access_method'] = 'spotify_oauth'
        tracks = info.get('tracks') or info.get('items')
        if not isinstance(tracks, dict):
            response = client.get(f'https://api.spotify.com/v1/playlists/{playlist_id}/items')
            response.raise_for_status()
            tracks = response.json()
        items = list(tracks.get('items', []))
        while tracks.get('next'):
            next_url = tracks['next']
            if urlparse(next_url).hostname != 'api.spotify.com':
                raise ValueError('Spotify returned an unexpected pagination URL.')
            response = client.get(next_url)
            response.raise_for_status()
            tracks = response.json()
            items.extend(tracks.get('items', []))
        songs = [row.get('track') or row.get('item') for row in items]
        songs = [song for song in songs if song and song.get('type') == 'track' and song.get('id')]
        if not songs:
            raise ValueError('This playlist has no tracks accessible to your account.')
        return info, songs

def validate_daylist_capture(url, info, settings, explicit_url, now):
    """Shared personalized URLs can stay frozen even as the account's daylist changes."""
    if info.get('access_method') != 'shared_link':
        return
    # Spotify's shared personalized daylist IDs differ from an owned playlist copy.
    if not spotify_playlist(url).startswith('37i9dQZF1Fb'):
        return
    weekdays = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                'friday': 4, 'saturday': 5, 'sunday': 6,
                'lunes': 0, 'martes': 1, 'miércoles': 2, 'miercoles': 2,
                'jueves': 3, 'viernes': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6}
    words = re.findall(r'[^\W\d_]+', str(info.get('name', '')).casefold())
    named_days = {weekdays[word] for word in words if word in weekdays}
    if named_days and now.weekday() not in named_days:
        raise ValueError('The saved shared daylist belongs to another day. Spotify may give each update a new URL. Send /daylist <current URL> from Spotify. Your Spotify API authorization cannot resolve this personalized daylist automatically.')
    selected_date = settings.get('daylist_url_selected_date')
    if not explicit_url and (not selected_date or selected_date != now.date().isoformat()):
        raise ValueError('The saved shared daylist belongs to another day. Spotify may give each update a new URL. Send /daylist <current URL> from Spotify. Your Spotify API authorization cannot resolve this personalized daylist automatically.')


def canonical_url(url):
    allowed = {'open.spotify.com', 'spotify.link', 'spoti.fi', 'tidal.com', 'listen.tidal.com', 'www.tidal.com',
        'soundcloud.com', 'www.soundcloud.com', 'on.soundcloud.com', 'music.apple.com', 'www.qobuz.com',
        'open.qobuz.com', 'www.deezer.com', 'deezer.com', 'link.deezer.com', 'music.youtube.com',
        'youtube.com', 'www.youtube.com', 'youtu.be'}
    for _ in range(6):
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError('This URL is not from a supported music service.')
        if parsed.hostname not in {'spotify.link', 'spoti.fi', 'on.soundcloud.com', 'link.deezer.com'}:
            return url
        response = httpx.get(url, follow_redirects=False, timeout=30)
        if response.status_code not in (301, 302, 303, 307, 308):
            raise ValueError('Could not resolve the short URL. Send the full URL.')
        url = response.headers['location']
    raise ValueError('The URL has too many redirects.')

def provider_pause(services):
    os.environ['MORNYE_DATA_DIR'] = str(ROOT / 'mornye-data')
    from mornye.core.signed_session_mobile import client_from_manifest, auth_backoff_remaining
    waits = []
    for service in services:
        manifest = ROOT / 'mornye-data/extensions' / service.removeprefix('ext:') / 'manifest.json'
        if not manifest.exists():
            return 0
        config = json.loads(manifest.read_text()).get('signedSession')
        if not config:
            return 0
        client = client_from_manifest(config)
        if client.authenticated:
            return 0
        remaining = auth_backoff_remaining(client)
        if remaining <= 0:
            return 0
        waits.append(remaining)
    return min(waits) if waits else 0

def prepare_sources(services):
    # Verify before optional metadata lookups impose their shorter timeouts.
    # Serialize verification so a small server only needs one browser at a time.
    import asyncio
    os.environ.update(MORNYE_BROWSER_NETWORK='1', MORNYE_BROWSER_SOCKET=str(STATE / 'gateway.sock'), MORNYE_DATA_DIR=str(ROOT / 'mornye-data'),
        CHROME_PATH=os.getenv('CHROME_PATH', '/usr/bin/google-chrome'), TS_DEBUG_VISIBLE='1',
        TS_MAX_CONCURRENT_BROWSERS='1',
        PATH=str(ROOT / '.venv/bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'))
    from mornye.core.signed_session_mobile import (client_from_manifest,
        auth_backoff_remaining, _clear_auth_backoff, _record_auth_failure, _retry_after_from)
    async def verify():
        for service in services:
            manifest = ROOT / 'mornye-data/extensions' / service.removeprefix('ext:') / 'manifest.json'
            if not manifest.exists():
                continue
            config = json.loads(manifest.read_text()).get('signedSession')
            if not config:
                continue
            client = client_from_manifest(config)
            try:
                if not client.authenticated and auth_backoff_remaining(client) <= 0:
                    try:
                        await client.authenticate_with_turnstile(timeout=90)
                        _clear_auth_backoff(client)
                    except Exception as error:
                        _record_auth_failure(client, _retry_after_from(error))
            finally:
                if client._client:
                    await client._client.aclose()
    asyncio.run(verify())

def stable_url(url):
    from urllib.parse import parse_qsl, urlencode, urlunparse
    parsed = urlparse(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key in {'v', 'list', 'i'}])
    return urlunparse(parsed._replace(query=query, fragment=''))

def write_playlist_csv(folder, songs):
    source = folder / 'playlist.csv'
    with source.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Track URI', 'Track Name', 'Artist Name(s)', 'Album Name'])
        for song in songs:
            writer.writerow(['spotify:track:' + song['id'], song['name'], ', '.join(a['name'] for a in song['artists']), song['album']['name']])
    return source

def collection_order(url, services):
    import asyncio
    from mornye import AsyncMornye
    async def fetch():
        async with AsyncMornye(output_dir=str(ROOT / 'probe'), services=services) as client:
            info, tracks = await client.get_playlist(url)
            return info, [str(track.id) for track in tracks]
    try:
        info, ids = asyncio.run(fetch())
        COLLECTION_NAMES[stable_url(url)] = info.get('name') or info.get('title')
    except Exception as error:
        raise ValueError('Mornye could not read this collection order. Please retry.') from error
    if not ids:
        raise ValueError('This collection returned no accessible tracks.')
    return ids

def run(request):
    STATE.mkdir(parents=True, exist_ok=True)
    progress('Reading the collection')
    settings_path = STATE / 'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    action = request.get('action')
    daylist = action == 'daylist'
    order_ids = []
    category = None
    services = ['ext:tidal-web', 'ext:qobuz-web', 'ext:deezer', 'ext:amazon']
    if daylist:
        url = request.get('url') or settings.get('daylist_url')
        if not url:
            raise ValueError('First send /daylist followed by your Spotify daylist URL. Use /spotify to connect your account.')
        spotify_playlist(url)
        info, songs = snapshot(url, prefer_account=True)
        date = datetime.now(ZoneInfo(os.getenv('APP_TIMEZONE', 'UTC')))
        validate_daylist_capture(url, info, settings, bool(request.get('url')), date)
        settings['daylist_url'] = url
        if request.get('url'):
            settings['daylist_url_selected_date'] = date.date().isoformat()
        save(settings_path, settings)
        name = safe(info['name'])
        # Snapshot id keeps retries in the same directory and distinguishes daylist refreshes.
        folder = MUSIC / 'Spotify' / 'Daylists' / date.strftime('%Y-%m-%d') / (name + ' - ' + safe(info.get('snapshot_id', date.strftime('%H-%M')))[:12])
        folder.mkdir(parents=True, exist_ok=True)
        save(folder / 'snapshot.json', {'captured_at': date.isoformat(), 'playlist': info, 'tracks': songs})
        source = write_playlist_csv(folder, songs)
        order_ids = [song['id'] for song in songs]
        args = ['--csv', str(source), str(folder), '--no-playlist-subfolders', '--use-track-numbers']
    else:
        url = canonical_url(request.get('url', ''))
        host = urlparse(url).hostname
        if host in {'soundcloud.com', 'www.soundcloud.com'}:
            services = ['ext:soundcloud']
        elif host in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}:
            services = ['ext:ytmusic-spotiflac']
        elif host in {'www.deezer.com', 'deezer.com'}:
            services = ['ext:deezer', 'ext:tidal-web', 'ext:qobuz-web', 'ext:amazon']
        path = urlparse(url).path
        if host == 'music.apple.com' and parse_qs(urlparse(url).query).get('i'):
            category = 'Singles'
        elif re.search(r'/album[s]?/', path):
            category = 'Albums'
        elif re.search(r'/artist[s]?/', path):
            category = 'Artists'
        elif re.search(r'/playlist[s]?/|/sets/', path) or 'list=' in url:
            category = 'Playlists'
        else:
            category = 'Singles'
        name = category
        # Stable URL identity prevents unrelated playlists from sharing an M3U.
        import hashlib
        folder = MUSIC / 'Spotify' / category
        if category == 'Playlists':
            playlist_match = re.search(r'/playlists?/([A-Za-z0-9]+)', path)
            playlist_id = playlist_match.group(1) if playlist_match else hashlib.sha256(stable_url(url).encode()).hexdigest()[:12]
            folder = folder / playlist_id
        args = [url, str(folder), '--no-playlist-subfolders']
        if category != 'Playlists':
            args += ['--use-artist-subfolders', '--use-album-subfolders']
        if category in {'Albums', 'Artists'}:
            args += ['--use-album-track-numbers', '--use-track-numbers']
    folder.mkdir(parents=True, exist_ok=True)
    import hashlib
    job_id = hashlib.sha256(stable_url(url).encode()).hexdigest()[:12]
    m3u = folder / (('Daylist' if daylist else 'Playlist-' + job_id) + '.m3u')
    if not daylist and category in {'Albums', 'Playlists', 'Artists'}:
        if category == 'Playlists' and urlparse(url).hostname == 'open.spotify.com':
            info, songs = snapshot(url)
            name = safe(info.get('name', name))
            order_ids = [song['id'] for song in songs]
            source = write_playlist_csv(folder, songs)
            save(folder / 'snapshot.json', {'playlist': info, 'tracks': songs})
            args = ['--csv', str(source), str(folder), '--no-playlist-subfolders', '--use-track-numbers']
        else:
            order_ids = collection_order(url, services)
            name = safe(COLLECTION_NAMES.get(stable_url(url)) or name)
    progress('Checking existing tracks', total=len(order_ids))
    existing = (existing_daylist_tracks(order_ids) if daylist else
                completed_request(folder / ('report-' + job_id + '.json'), order_ids))
    pending = [song for song in songs if song['id'] not in existing] if daylist else None
    progress('Checking providers', total=len(order_ids), skipped=len(existing))
    command = [str(ROOT / '.venv/bin/mornye'), *args, '--m3u', 'none', '--mp3', '--transcode-bitrate', '320k',
        '--max-concurrent', '2', '--timeout', '180', '--retries', '0', '--no-resume', '--json',
        '--service', *services]
    env = dict(os.environ, MORNYE_BROWSER_NETWORK='1', MORNYE_BROWSER_SOCKET=str(STATE / 'gateway.sock'), TS_DEBUG_VISIBLE='1', TS_MAX_CONCURRENT_BROWSERS='1', CHROME_PATH=os.getenv('CHROME_PATH', '/usr/bin/google-chrome'), MORNYE_REGISTRIES=REGISTRY, MORNYE_MIN_TRUST='checksum-only', MORNYE_DATA_DIR=str(ROOT / 'mornye-data'), PATH=str(ROOT / '.venv/bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'))
    if existing and (not daylist or not pending):
        process = subprocess.CompletedProcess(command, 0, stdout=json.dumps({'schema_version': 1, 'tracks': [], 'summary': {}}))
        cleanup_flacs(folder, {p for p in folder.rglob('*') if p.suffix.lower() == '.flac'})
    else:
        prepare_sources(services)
        pause = provider_pause(services)
        if pause > 0:
            import math
            raise ValueError(f'Mornye audio provider verification is pending. Retry in {math.ceil(pause / 60)} min. Audio downloads do not use your Spotify session.')
        # Exclude existing IDs before Mornye can contact any audio provider.
        with tempfile.TemporaryDirectory(prefix='music-csv-', dir=STATE) as temp:
            if daylist:
                command[command.index('--csv') + 1] = str(write_playlist_csv(Path(temp), pending))
            process = run_cli(command, env, folder, job_id, len(order_ids), len(existing))
    progress('Verifying files and building the playlist', total=len(order_ids))
    try:
        report = json.loads(process.stdout)
    except ValueError:
        # A provider may print a diagnostic before the CLI's JSON document.
        report = None
        decoder = json.JSONDecoder()
        for match in re.finditer(r'\{', process.stdout):
            try:
                candidate, _ = decoder.raw_decode(process.stdout[match.start():])
                if isinstance(candidate, dict) and candidate.get('schema_version') == 1 and 'summary' in candidate:
                    report = candidate
            except ValueError:
                continue
    if report is None:
        raise ValueError('Mornye could not resolve or download this URL. Diagnostics were logged on the server.')
    # The built-in --write-m3u omits skipped tracks; rebuild from all successful
    # results so a retry still produces the complete playlist.
    # Mornye emits a result for each provider attempt. Count each recording once.
    merged = dict(existing)
    rank = {'downloaded': 3, 'skipped': 2, 'failed': 1}
    for entry in report.get('tracks', []):
        key = entry.get('id') or (entry.get('title'), entry.get('artists'))
        previous = merged.get(key)
        if previous is None or rank.get(entry.get('status'), 0) > rank.get(previous.get('status'), 0):
            merged[key] = entry
    entries = list(merged.values())
    report['tracks'] = entries
    from mutagen.mp3 import MP3
    for entry in entries:
        if entry.get('status') not in {'downloaded', 'skipped'} or not entry.get('file_path'):
            continue
        audio_path = Path(entry['file_path'])
        try:
            audio = MP3(audio_path)
            if audio_path.suffix.lower() != '.mp3' or abs(audio.info.bitrate - 320000) > 2000:
                raise ValueError('Unexpected audio format or bitrate')
            if not audio.tags or not all(tag in audio.tags for tag in ('TIT2', 'TPE1', 'TALB')):
                raise ValueError('Missing core metadata')
        except Exception:
            entry['status'] = 'failed'
            entry['error'] = 'The file failed MP3 320 kbps and metadata validation.'
    if order_ids:
        by_id = {str(entry['id']): entry for entry in entries if entry.get('id')}
        ordered = [by_id[str(track_id)] for track_id in order_ids if str(track_id) in by_id]
    else:
        ordered = entries
    m3u = playlist_path(folder, daylist, name, category, entries, job_id, date if daylist else None)
    own_m3u = m3u
    equivalent = equivalent_single_track_playlist(category, ordered) if not daylist and (not order_ids or len(ordered) == len(order_ids)) else None
    if equivalent is not None:
        m3u = equivalent
    lines = ['#EXTM3U']
    for entry in ordered:
        if entry.get('status') not in {'downloaded', 'skipped'} or not entry.get('file_path'):
            continue
        audio_path = Path(entry['file_path'])
        if not audio_path.is_file():
            entry['status'] = 'failed'
            continue
        label = (str(entry.get('artists', '')) + ' - ' + str(entry.get('title', audio_path.stem))).replace('\n', ' ').replace('\r', ' ')
        duration = int(entry.get('duration_ms', 0) or 0) // 1000
        lines.extend([f'#EXTINF:{duration},{label}', os.path.relpath(audio_path, m3u.parent)])
    if equivalent is not None:
        # Preserve the canonical export's paths and the existing audio locations.
        if own_m3u != m3u and own_m3u.exists():
            paths = [line for line in own_m3u.read_text(encoding='utf-8').splitlines() if line and not line.startswith('#')]
            if len(paths) == 1 and (own_m3u.parent / paths[0]).resolve() == Path(ordered[0]['file_path']).resolve():
                own_m3u.unlink()
    elif len(lines) > 1:
        temp_m3u = m3u.with_suffix('.tmp')
        temp_m3u.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        temp_m3u.replace(m3u)
    elif m3u.exists():
        m3u.unlink()
    report['summary'] = {'total': len(entries), **{status: sum(entry.get('status') == status for entry in entries) for status in ('downloaded', 'skipped', 'failed')}}
    save(folder / ('report-' + job_id + '.json'), report)
    summary = report.get('summary', {})
    if not summary.get('total'):
        raise ValueError('No downloadable tracks found. Check the service extension.')
    if not daylist and category in {'Albums', 'Singles', 'Artists'}:
        name = m3u.stem
    already_downloaded = summary.get('skipped') == summary.get('total')
    return {'summary': summary, 'already_downloaded': already_downloaded, 'name': name, 'folder': str(folder.relative_to(MUSIC)),
        'm3u': os.path.relpath(m3u, folder) if m3u.exists() else None,
        'notice': 'Mornye providers returned no audio. No empty M3U was created. Check /spotify or retry later.' if summary.get('failed') == summary.get('total') else None}

if __name__ == '__main__':
    os.umask(0o022)
    # Let run_cli's finally remove intermediate FLACs on bot cancellation.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    try:
        # Also serializes requests made from a terminal.
        ROOT.mkdir(exist_ok=True)
        with (ROOT / 'worker.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            request = json.load(sys.stdin)
            STREAM_PROGRESS = request.pop('stream_progress', False) is True
            result = run(request)
    except BlockingIOError:
        result = {'error': 'A music download is already running on the server. Retry when it finishes.'}
    except ValueError as exc:
        result = {'error': str(exc)}
    except subprocess.TimeoutExpired:
        result = {'error': 'Mornye timed out. Completed MP3s are retained; retry to finish pending tracks.'}
    except OSError as exc:
        import errno
        messages = {errno.ENOSPC: 'The destination disk is full. Free some space and retry.',
                    errno.EROFS: 'The destination disk is read-only.',
                    errno.EACCES: 'Permission denied when writing to the destination folder.'}
        result = {'error': messages.get(exc.errno, 'Could not access download files. Check server storage.')}
    except Exception:
        import traceback
        with (ROOT / 'worker-error.log').open('a') as log:
            traceback.print_exc(file=log)
        result = {'error': 'Download failed. Diagnostics were logged on the server; you can retry.'}
    print(json.dumps(result, ensure_ascii=False))
