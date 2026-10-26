"""Exercise browser discovery without Spotify sessions or network access."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

spec = importlib.util.spec_from_file_location('daylist_discovery', Path(__file__).resolve().parents[1]/'integrations/mornye/daylist_discovery.py')
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_share_url_is_spotify_playlist_and_not_generic_entry(self):
        url = 'https://open.spotify.com/playlist/' + 'a'*22
        self.assertEqual(discovery.checked_share_url(url+'?si=tracking'), url)
        for invalid in (discovery.DAYLIST_URL, 'https://evil.example/playlist/'+'a'*22, 'https://open.spotify.com@evil.example/playlist/'+'a'*22, 'https://open.spotify.com/track/'+'a'*22):
            with self.assertRaises(ValueError):
                discovery.checked_share_url(invalid)

    async def exercise(self, state):
        response = MagicMock()
        response.json.return_value = [{'type':'page','url':'about:blank','webSocketDebuggerUrl':'ws://127.0.0.1:9223/test'}]
        client = AsyncMock()
        client.get.return_value = response
        http = MagicMock()
        http.return_value.__aenter__ = AsyncMock(return_value=client)
        http.return_value.__aexit__ = AsyncMock(return_value=False)
        websocket = MagicMock()
        websocket.return_value.__aenter__ = AsyncMock()
        websocket.return_value.__aexit__ = AsyncMock(return_value=False)
        public_url = 'https://open.spotify.com/playlist/' + 'b'*22
        with patch.object(discovery.httpx, 'AsyncClient', http), patch.object(discovery.websockets, 'connect', websocket), patch.object(discovery.Browser, 'call', new_callable=AsyncMock) as call, patch.object(discovery.Browser, 'js', new=AsyncMock(side_effect=[state,None,public_url])):
            result = await discovery.resolve()
            call.assert_any_await('Network.setCacheDisabled', cacheDisabled=True)
            call.assert_any_await('Page.navigate', url=discovery.DAYLIST_URL)
            call.assert_any_await('Page.navigate', url='about:blank')
            self.assertEqual(result['url'], public_url)
            self.assertEqual(result['track_ids'], ['current-track'])

    async def test_fresh_resolution_from_idle_browser(self):
        await self.exercise({'host':'open.spotify.com','name':'daylist','ids':['current-track'],'login':False})

    async def test_expired_session_requires_login(self):
        with self.assertRaisesRegex(ValueError, 'Sign in'):
            await self.exercise({'host':'open.spotify.com','login':True})
