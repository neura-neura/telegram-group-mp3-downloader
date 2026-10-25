"""Offline workflow tests; no Telegram, Spotify, or provider credentials required."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram_group_mp3_downloader import bot, music

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('music_worker', PROJECT / 'integrations/mornye/music_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_schedule_and_compact_summary(self):
        state = bot.DownloadState(1200, 0, Path('/music/Telegram'), existing_files=1200)
        self.assertIn('Up to date', bot.progress_text(state, final=True))
        self.assertNotIn('0 B/s', bot.progress_text(state, final=True))
        app = SimpleNamespace(bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(title='Music')), send_message=AsyncMock()))
        store = SimpleNamespace(mark_schedule_ran=AsyncMock())
        row = {'id':1, 'chat_id':-1, 'interval_minutes':1440, 'notify_chat_id':1, 'do_download':1, 'target_dir':'/music/Telegram'}
        with patch.object(bot, 'get_store', return_value=store), patch.object(bot, 'create_scan_snapshot', new=AsyncMock(return_value=({'total_count':1200}, 0))), patch.object(bot, 'download_missing_for_chat', new=AsyncMock(return_value=state)):
            await bot.run_scheduled_job(app, row)
        app.bot.send_message.assert_not_called()

    async def test_duplicate_and_competing_requests(self):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={}))
        def update():
            return SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock(return_value=object())))
        first, duplicate, other = update(), update(), update()
        started, release = asyncio.Event(), asyncio.Event()
        async def result(*args):
            started.set()
            await release.wait()
            return {'name':'Test', 'folder':'Spotify/Singles', 'summary':{'downloaded':0, 'skipped':1, 'failed':0}, 'already_downloaded':True}
        spawn = AsyncMock(return_value=SimpleNamespace(returncode=0))
        with patch.object(music, 'music_guard', new=AsyncMock(return_value=True)), patch.object(music, 'worker_result', side_effect=result), patch.object(music.asyncio, 'create_subprocess_exec', new=spawn), patch.object(bot, 'safe_edit', new=AsyncMock()):
            task = asyncio.create_task(music.run_request(first, context, {'action':'url', 'url':'https://open.spotify.com/album/test?si=first'}))
            await asyncio.wait_for(started.wait(), 2)
            await music.run_request(duplicate, context, {'action':'url', 'url':'https://open.spotify.com/intl-en/album/test?si=second'})
            await music.run_request(other, context, {'action':'url', 'url':'https://open.spotify.com/track/other'})
            self.assertIn('This download is already running', duplicate.effective_message.reply_text.await_args.args[0])
            self.assertIn('Another music download', other.effective_message.reply_text.await_args.args[0])
            self.assertEqual(spawn.await_count, 1)
            release.set()
            await task
        self.assertFalse(context.application.bot_data['music_lock'].locked())
        self.assertNotIn('music_active_request', context.application.bot_data)

    async def test_music_is_opt_in_and_owner_only(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()), effective_user=SimpleNamespace(id=1))
        with patch.object(bot, 'guard', new=AsyncMock(return_value=True)), patch.dict(os.environ, {'MORNYE_ENABLED':'false'}):
            self.assertFalse(await music.music_guard(update, None))
        with patch.object(bot, 'guard', new=AsyncMock(return_value=True)), patch.dict(os.environ, {'MORNYE_ENABLED':'true', 'MUSIC_ALLOWED_USER_IDS':'', 'ALLOWED_USER_IDS':''}):
            self.assertFalse(await music.music_guard(update, None))


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required for real MP3 fixtures')
class MusicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.settings = patch.multiple(worker, ROOT=root, MUSIC=root/'music', STATE=root/'state')
        self.settings.start()
        self.addCleanup(self.settings.stop)
        worker.STATE.mkdir()
        self.source = root/'source.mp3'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libmp3lame','-b:a','320k','-metadata','title=Track','-metadata','artist=Artist','-metadata','album=Album',str(self.source)], check=True)
        self.song = {'id':'track-id', 'name':'Track', 'artists':[{'name':'Artist'}], 'album':{'name':'Album'}}

    def test_daylists_skip_original_files_and_create_dated_m3us_each_time(self):
        old = worker.MUSIC/'Spotify/Daylists/2000-01-01/Original'
        old.mkdir(parents=True)
        original = old/'Track.mp3'
        shutil.copy2(self.source, original)
        row = {'id':'track-id', 'title':'Track', 'artists':'Artist', 'album':'Album', 'status':'downloaded', 'file_path':str(original)}
        worker.save(old/'report-original.json', {'tracks':[row]})
        with patch.object(worker, 'snapshot', return_value=({'name':'New daylist', 'snapshot_id':'new'}, [self.song])), patch.object(worker, 'prepare_sources', side_effect=AssertionError('Provider called for existing audio')), patch.object(worker, 'run_cli', side_effect=AssertionError('Downloader called for existing audio')):
            first = worker.run({'action':'daylist', 'url':'https://open.spotify.com/playlist/example'})
            second = worker.run({'action':'daylist'})
        self.assertTrue(first['already_downloaded'])
        self.assertNotEqual(first['m3u'], second['m3u'])
        folder = worker.MUSIC/first['folder']
        self.assertRegex(first['m3u'], r'^Daylist - \d{4}-\d{2}-\d{2} ')
        self.assertIn('New daylist', first['m3u'])
        self.assertFalse(list(folder.glob('*.mp3')))
        for result in (first, second):
            paths = [line for line in (folder/result['m3u']).read_text().splitlines() if line and not line.startswith('#')]
            self.assertEqual([(folder/path).resolve() for path in paths], [original])
        original.unlink()
        self.assertFalse(worker.existing_daylist_tracks(['track-id']))

    def test_completed_urls_skip_and_missing_files_retry(self):
        calls = []
        def download(command, env, folder, *args):
            calls.append(command)
            target = folder/'Track.mp3'
            shutil.copy2(self.source, target)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({'schema_version':1, 'summary':{}, 'tracks':[{'id':'track-id', 'title':'Track', 'artists':'Artist', 'album':'Album', 'status':'downloaded', 'file_path':str(target)}]}))
        with patch.object(worker, 'run_cli', side_effect=download), patch.object(worker, 'snapshot', return_value=({'name':'Collection'}, [self.song])), patch.object(worker, 'collection_order', return_value=['track-id']), patch.object(worker, 'prepare_sources'), patch.object(worker, 'provider_pause', return_value=0):
            for kind in ('track', 'album', 'playlist', 'artist'):
                url = f'https://open.spotify.com/{kind}/example'
                first = worker.run({'action':'url', 'url':url+'?si=first'})
                count = len(calls)
                second = worker.run({'action':'url', 'url':url+'?si=second'})
                self.assertTrue(second['already_downloaded'])
                self.assertEqual(len(calls), count)
                self.assertFalse(second['m3u'].startswith('Playlist-' ))
                report = next((worker.MUSIC/second['folder']).glob('report-*.json'))
                self.assertFalse(worker.completed_request(report, ['changed-recording']))
                (worker.MUSIC/second['folder']/'Track.mp3').unlink()
                worker.run({'action':'url', 'url':url})
                self.assertEqual(len(calls), count+1)

    def test_failed_job_cleans_its_flac_only(self):
        folder = worker.MUSIC/'Spotify/Albums'
        folder.mkdir(parents=True)
        own = folder/'unfinished.flac'
        original = folder/'original.flac'
        original.write_bytes(b'preexisting file')
        def fail(*args, **kwargs):
            own.write_bytes(b'temporary source')
            raise subprocess.TimeoutExpired('mornye', 1)
        with patch.object(worker.subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.run_cli(['mornye'], {}, folder, 'example', 1, 0)
        self.assertFalse(own.exists())
        self.assertTrue(original.exists())

    def test_telegram_playlist_is_relative_and_deduplicated(self):
        target = worker.MUSIC/'Telegram'
        target.mkdir(parents=True)
        path = target/'2026/01/01/Track.mp3'
        bot.append_download_to_playlist(path, target)
        bot.append_download_to_playlist(path, target)
        lines = (target/'All Tracks.m3u').read_text().splitlines()
        self.assertEqual(lines.count('2026/01/01/Track.mp3'), 1)
        self.assertEqual(lines[0], '#EXTM3U')


if __name__ == '__main__':
    unittest.main()
