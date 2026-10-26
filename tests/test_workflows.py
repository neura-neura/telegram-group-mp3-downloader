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

    def test_one_track_album_and_song_share_one_static_playlist(self):
        import hashlib
        snapshots = {}
        song_url = 'https://open.spotify.com/track/example'
        album_url = 'https://open.spotify.com/album/example'
        for category, url in [('Albums', album_url), ('Singles', song_url)]:
            folder = worker.MUSIC/'Spotify'/category
            folder.mkdir(parents=True)
            audio = folder/'Track.mp3'
            shutil.copy2(self.source, audio)
            row = {'id':'track-id', 'title':'Track', 'artists':'Artist', 'album':'Album', 'status':'downloaded', 'file_path':str(audio)}
            job = hashlib.sha256(url.encode()).hexdigest()[:12]
            worker.save(folder/f'report-{job}.json', {'summary':{'total':1, 'failed':0}, 'tracks':[row]})
            playlist = worker.playlist_path(folder, False, category, category, [row], job)
            playlist.write_text('#EXTM3U\nTrack.mp3\n')
            snapshots[category] = (audio, playlist, audio.read_bytes())
        with patch.object(worker, 'run_cli', side_effect=AssertionError('Completed audio reached downloader')):
            result = worker.run({'action':'url', 'url':song_url})
            repeated = worker.run({'action':'url', 'url':song_url})
        canonical = snapshots['Albums'][1]
        self.assertEqual((worker.MUSIC/result['folder']/result['m3u']).resolve(), canonical)
        self.assertEqual(result['m3u'], repeated['m3u'])
        self.assertTrue(canonical.exists())
        self.assertFalse(snapshots['Singles'][1].exists())
        for audio, playlist, before in snapshots.values():
            self.assertEqual(audio.read_bytes(), before)
        other = {'id':'other-recording', 'title':'Track', 'artists':'Artist', 'album':'Album', 'status':'downloaded'}
        self.assertIsNone(worker.equivalent_single_track_playlist('Singles', [other]))
        self.assertIsNone(worker.equivalent_single_track_playlist('Albums', [other, other]))
        self.assertIsNone(worker.equivalent_single_track_playlist('Playlists', [other]))

    def test_shared_daylist_freshness_and_new_identifier(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo('UTC'))
        old_url = 'https://open.spotify.com/playlist/37i9dQZF1FbOld'
        new_url = 'https://open.spotify.com/playlist/37i9dQZF1FbNew'
        stale = {'name':'breakcore sunday afternoon', 'access_method':'shared_link'}
        current = {'name':'hard trance monday morning', 'access_method':'shared_link'}
        with self.assertRaisesRegex(ValueError, 'another day'):
            worker.validate_daylist_capture(old_url, stale, {}, True, now)
        with self.assertRaisesRegex(ValueError, 'another day'):
            worker.validate_daylist_capture(new_url, current, {'daylist_url_selected_date':'2026-10-25'}, False, now)
        worker.validate_daylist_capture(new_url, current, {}, True, now)
        worker.validate_daylist_capture(new_url, current, {'daylist_url_selected_date':'2026-10-26'}, False, now)
        # Owned playlist copies and account-authenticated results aren't shared snapshots.
        worker.validate_daylist_capture('https://open.spotify.com/playlist/owned', stale, {}, False, now)
        worker.validate_daylist_capture(new_url, {**stale, 'access_method':'spotify_oauth'}, {}, False, now)
        worker.STATE.joinpath('settings.json').write_text(json.dumps({'daylist_url':old_url, 'daylist_url_selected_date':'2026-10-25'}))
        with patch.object(worker, 'snapshot', return_value=(stale, [self.song])), patch.object(worker, 'prepare_sources', side_effect=AssertionError('Stale snapshot reached providers')):
            with self.assertRaises(ValueError):
                worker.run({'action':'daylist'})
        self.assertFalse(list(worker.MUSIC.rglob('*.m3u')))
        self.assertFalse(list(worker.MUSIC.rglob('snapshot.json')))
        self.assertEqual(json.loads((worker.STATE/'settings.json').read_text())['daylist_url'], old_url)

    def test_daylist_prefers_account_metadata_before_shared_fallback(self):
        import httpx
        worker.STATE.joinpath('spotify_token.json').write_text('{}')
        url = 'https://open.spotify.com/playlist/example'
        info = {'name':'Current account playlist', 'tracks':{'items':[{'track':{**self.song, 'type':'track'}}]}}
        response = httpx.Response(200, json=info, request=httpx.Request('GET', 'https://api.spotify.com/v1/playlists/example'))
        client = SimpleNamespace(get=lambda *args, **kwargs:response)
        from unittest.mock import MagicMock
        manager = MagicMock()
        manager.__enter__.return_value = client
        with patch.object(worker, 'token', return_value='TEST_TOKEN'), patch.object(worker.httpx, 'Client', return_value=manager), patch.object(worker, 'public_snapshot', side_effect=AssertionError('Shared data used before account')):
            result, songs = worker.snapshot(url, prefer_account=True)
        self.assertEqual(result['access_method'], 'spotify_oauth')
        self.assertEqual(songs[0]['id'], self.song['id'])
        response = httpx.Response(404, json={'error':{'status':404}}, request=httpx.Request('GET', 'https://api.spotify.com/v1/playlists/example'))
        with patch.object(worker, 'token', return_value='TEST_TOKEN'), patch.object(worker.httpx, 'Client', return_value=manager), patch.object(worker, 'public_snapshot', return_value=({'name':'Shared fallback'}, [self.song])) as shared:
            result, songs = worker.snapshot(url, prefer_account=True)
            shared.assert_called_once_with(url)
        self.assertEqual(result['name'], 'Shared fallback')

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
