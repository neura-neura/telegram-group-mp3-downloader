"""Offline lifecycle tests: no real browser, provider calls, or downloads."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('gateway_test_package.gateway_under_test', Path(__file__).resolve().parents[1]/'integrations/mornye/server_browser_network.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def make_browser(self):
        browser = gateway.GatewayBrowser.__new__(gateway.GatewayBrowser)
        browser.origin = 'https://api.zarz.moe'
        browser.browser = object()
        browser.last_used = time.monotonic()
        browser.lock = asyncio.Lock()
        browser._stop = AsyncMock(side_effect=lambda: setattr(browser, 'browser', None))
        browser._start = AsyncMock(side_effect=lambda: setattr(browser, 'browser', object()))
        browser.evaluate = AsyncMock(return_value=browser.origin)
        return browser

    def solver(self):
        module = ModuleType('gateway_test_package.solver')
        module._js_value = lambda result: result
        return patch.dict(sys.modules, {'gateway_test_package.solver':module})

    async def test_idle_browser_replaced_before_request(self):
        browser = self.make_browser()
        browser.last_used -= 901
        await browser._ensure_browser()
        browser._stop.assert_awaited_once()
        browser._start.assert_awaited_once()
        browser.evaluate.assert_not_awaited()

    async def test_unresponsive_renderer_replaced_before_request(self):
        browser = self.make_browser()
        browser.evaluate.side_effect = TimeoutError('local renderer')
        await browser._ensure_browser()
        browser._stop.assert_awaited_once()
        browser._start.assert_awaited_once()

    async def test_healthy_browser_kept(self):
        browser = self.make_browser()
        with self.solver():
            await browser._ensure_browser()
        browser._stop.assert_not_awaited()
        browser._start.assert_not_awaited()

    async def test_failed_signed_post_is_not_replayed(self):
        browser = self.make_browser()
        browser._ensure_browser = AsyncMock()
        browser.evaluate.side_effect = TimeoutError('signed request')
        with self.assertRaisesRegex(RuntimeError, 'connection was reset'):
            await browser.fetch('POST', browser.origin+'/v2/tickets', {}, '{}', 1)
        browser.evaluate.assert_awaited_once()
        browser._stop.assert_awaited_once()

    async def test_inspection_does_not_start_browser_or_contact_provider(self):
        browser = self.make_browser()
        browser.browser = None
        self.assertEqual(await browser.inspect(), {'running':False})
        browser._start.assert_not_awaited()
        browser.evaluate.assert_not_awaited()
