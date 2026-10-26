"""Telegram entry points; downloads run in an isolated Python 3.12 process."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import signal
import re
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes

PROJECT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv('MORNYE_HOME', str(PROJECT / 'runtime/mornye'))).expanduser().resolve()
WORKER = PROJECT / 'integrations/mornye/music_worker.py'
PYTHON = Path(os.getenv('MORNYE_PYTHON', str(ROOT / '.venv/bin/python')))
URL_RE = re.compile(r'https?://[^\s<>]+', re.I)
LOGGER = logging.getLogger(__name__)

async def music_guard(update, context):
    from .bot import guard
    if not await guard(update, context):
        return False
    if os.getenv('MORNYE_ENABLED', 'false').lower() not in {'1', 'true', 'yes'}:
        await update.effective_message.reply_text('Mornye is not configured. See docs/mornye.md for setup.')
        return False
    owners = [int(value.strip()) for value in (os.getenv('MUSIC_ALLOWED_USER_IDS') or os.getenv('ALLOWED_USER_IDS', '')).split(',') if value.strip()]
    if not update.effective_user or update.effective_user.id not in owners:
        await update.effective_message.reply_text('Music downloads are restricted to configured owner user IDs.')
        return False
    return True

async def spotify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .bot import guard
    if not await music_guard(update, context):
        return
    await update.effective_message.reply_text(
        '/daylist locates your current personalized daylist automatically.\n'
        'Mornye reads its songs and downloads the audio. A dedicated Spotify web session is used only to find the current share link.\n'
        'Sign in once in that server browser; see docs/mornye.md. If the session expires, sign in again.\n'
        '/daylist <playlist URL> captures that specific link for this request.\n'
        'Do not send passwords or cookies to the bot.')

def progress_text(state, elapsed):
    lines = ['🎵 ' + state.get('stage', 'Preparing download…')]
    total = state.get('total', 0)
    downloaded = state.get('downloaded', 0)
    skipped = state.get('skipped', 0)
    if total:
        done = min(total, downloaded + skipped)
        lines.append(f'{done}/{total} ready · {int(100 * done / total)}%')
    if downloaded or skipped:
        lines.append(f'{downloaded} new · {skipped} skipped (already exist)')
    if state.get('current'):
        lines.append('Last processed: ' + state['current'])
    minutes, seconds = divmod(int(elapsed), 60)
    lines.append(f'Elapsed: {minutes}:{seconds:02d}')
    return '\n'.join(lines)

async def worker_result(proc, request, message):
    from .bot import safe_edit
    request = {**request, 'stream_progress': True}
    proc.stdin.write(json.dumps(request).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    loop = asyncio.get_running_loop()
    started = last_edit = loop.time()
    state = {'stage': 'Preparing download…'}
    result = None
    reading = asyncio.create_task(proc.stdout.readline())
    try:
        while True:
            ready, _ = await asyncio.wait({reading}, timeout=3)
            if reading in ready:
                line = reading.result()
                if not line:
                    break
                event = json.loads(line)
                if event.get('event') == 'progress':
                    state = event
                else:
                    result = event
                reading = asyncio.create_task(proc.stdout.readline())
            now = loop.time()
            if result is None and now - last_edit >= 3:
                await safe_edit(message, progress_text(state, now - started))
                last_edit = loop.time()
        await proc.wait()
        if result is None:
            raise ValueError('Worker did not return a result')
        return result
    finally:
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)

def request_identity(request):
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    url = request.get('url')
    if not url and request.get('action') == 'daylist':
        try:
            url = json.loads((ROOT / 'state/settings.json').read_text()).get('daylist_url')
        except (OSError, ValueError):
            pass
    if not url:
        return request.get('action', 'download')
    parsed = urlparse(url)
    path = re.sub(r'^/intl-[a-z]+/', '/', parsed.path).rstrip('/')
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query) if k in {'v', 'list', 'i'}))
    return urlunparse(parsed._replace(scheme='https', netloc=parsed.netloc.lower(), path=path, query=query, fragment=''))

async def run_request(update: Update, context: ContextTypes.DEFAULT_TYPE, request: dict) -> None:
    from .bot import guard, safe_edit
    if not await music_guard(update, context):
        return
    # A single worker protects registry setup and the library from simultaneous writes.
    data = context.application.bot_data
    identity = request_identity(request)
    lock = data.setdefault('music_lock', asyncio.Lock())
    if lock.locked():
        same = data.get('music_active_request') == identity
        await update.effective_message.reply_text(
            'This download is already running. Follow its progress in the earlier message.' if same else
            'Another music download is running. Wait for it to finish, then send this link again.')
        return
    async with lock:
        data['music_active_request'] = identity
        proc = None
        message = None
        try:
            message = await update.effective_message.reply_text('🎵 Preparing download…')
            proc = await asyncio.create_subprocess_exec(str(PYTHON), str(WORKER),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                env={key: value for key, value in os.environ.items()
                     if key.startswith('MORNYE_') or key in {'PATH', 'LANG', 'HOME', 'MUSIC_ROOT', 'APP_TIMEZONE', 'CHROME_PATH', 'DISPLAY'}})
            result = await asyncio.wait_for(worker_result(proc, request, message), timeout=21600)
            if result.get('error'):
                await safe_edit(message, result['error'])
                return
            summary = result['summary']
            lines = ['🎵 ' + result['name']]
            if result.get('already_downloaded'):
                lines.append('✅ Already downloaded. No files were downloaded again.')
            lines += [
                f"{summary.get('downloaded', 0)} new · {summary.get('skipped', 0)} skipped (already exist) · {summary.get('failed', 0)} errors",
                'Folder: ' + result['folder']]
            if result.get('m3u'):
                lines.append('Playlist: ' + result['m3u'])
            if result.get('notice'):
                lines.append(result['notice'])
            await safe_edit(message, '\n'.join(lines))
        except asyncio.TimeoutError:
            await safe_edit(message, 'Download timed out. Completed files are retained; you can retry.')
        except Exception:
            LOGGER.exception('Music worker failed')
            await safe_edit(message, 'Could not complete the download. Check /spotify or retry. Details were logged on the server.')
        finally:
            data.pop('music_active_request', None)
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()

async def daylist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .bot import guard
    if not await music_guard(update, context):
        return
    if context.args and update.effective_chat.type != 'private':
        await update.effective_message.reply_text('Save your daylist URL in private chat with /daylist <URL>.')
        return
    await run_request(update, context, {'action': 'daylist', 'url': context.args[0] if context.args else None})

async def music_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from urllib.parse import urlparse
    from .bot import is_command_allowed, get_config
    if not is_command_allowed(update, get_config(context))[0]:
        return
    if os.getenv('MORNYE_ENABLED', 'false').lower() not in {'1', 'true', 'yes'}:
        return
    owners = [int(value.strip()) for value in (os.getenv('MUSIC_ALLOWED_USER_IDS') or os.getenv('ALLOWED_USER_IDS', '')).split(',') if value.strip()]
    if not update.effective_user or update.effective_user.id not in owners:
        return
    urls = URL_RE.findall(update.effective_message.text or '')
    supported = {'open.spotify.com', 'spotify.link', 'spoti.fi', 'tidal.com', 'listen.tidal.com',
                 'www.tidal.com', 'soundcloud.com', 'www.soundcloud.com', 'on.soundcloud.com',
                 'music.apple.com', 'www.qobuz.com', 'open.qobuz.com', 'www.deezer.com', 'deezer.com',
                 'link.deezer.com', 'music.youtube.com', 'youtube.com', 'www.youtube.com', 'youtu.be'}
    for url in urls[:1]:
        if urlparse(url).hostname in supported:
            await run_request(update, context, {'action': 'url', 'url': url.rstrip('.,)')})
