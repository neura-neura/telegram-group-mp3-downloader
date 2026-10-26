#!/usr/bin/env python3
"""Resolve the current daylist in a dedicated, signed-in Spotify web browser.

Only the public share URL and a small visible-track fingerprint leave this
browser. Cookies and account tokens never enter Mornye or provider requests.
"""
import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse
import httpx
import websockets

DAYLIST_ID = '37i9dQZF1EP6YuccBxUcC1'
DAYLIST_URL = 'https://open.spotify.com/playlist/' + DAYLIST_ID
LOGIN_REQUIRED = 'Sign in to Spotify in the dedicated daylist browser, then retry /daylist.'


def checked_share_url(value):
    parsed = urlparse(str(value))
    match = re.fullmatch(r'/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]{22})/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname != 'open.spotify.com' or not match or parsed.username or parsed.password:
        raise ValueError('The daylist browser did not return a valid Spotify playlist link.')
    if match[1] == DAYLIST_ID:
        raise ValueError('Spotify returned the generic daylist entry instead of its current share link.')
    return 'https://open.spotify.com/playlist/' + match[1]


class Browser:
    def __init__(self, socket):
        self.socket = socket
        self.sequence = 0

    async def call(self, method, **params):
        self.sequence += 1
        ident = self.sequence
        await self.socket.send(json.dumps({'id':ident, 'method':method, 'params':params}))
        while True:
            message = json.loads(await asyncio.wait_for(self.socket.recv(), timeout=25))
            if message.get('id') == ident:
                if 'error' in message:
                    raise ValueError('The daylist browser could not complete its operation.')
                return message.get('result', {})

    async def js(self, expression):
        result = await self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if result.get('exceptionDetails'):
            raise ValueError('Spotify changed its daylist page; browser discovery needs an update.')
        return result.get('result', {}).get('value')


async def resolve():
    endpoint = os.getenv('MORNYE_SPOTIFY_BROWSER_URL', 'http://127.0.0.1:9223').rstrip('/')
    parsed = urlparse(endpoint)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password:
        raise ValueError('The daylist browser endpoint must be on loopback.')
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(endpoint + '/json/list')
            response.raise_for_status()
            pages = [p for p in response.json() if p.get('type') == 'page' and (urlparse(p.get('url', '')).hostname == 'open.spotify.com' or p.get('url') == 'about:blank')]
            if not pages:
                raise ValueError(LOGIN_REQUIRED)
            page = next((p for p in pages if DAYLIST_ID in p.get('url', '')), pages[0])
            websocket = page['webSocketDebuggerUrl']
            if urlparse(websocket).hostname not in {'127.0.0.1', 'localhost', '::1'}:
                raise ValueError('Unexpected daylist browser socket.')
    except (httpx.HTTPError, KeyError):
        raise ValueError('The dedicated daylist browser is unavailable. Start it and sign in to Spotify.') from None
    async with websockets.connect(websocket, max_size=4*1024*1024) as socket:
        browser = Browser(socket)
        await browser.call('Page.enable')
        await browser.call('Network.enable')
        await browser.call('Network.setCacheDisabled', cacheDisabled=True)
        await browser.call('Page.navigate', url=DAYLIST_URL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = await browser.js('''(() => ({
                host:location.hostname,
                login:!!document.querySelector('[data-testid="login-button"]'),
                name:document.querySelector('main h1')?.innerText || '',
                ids:[...document.querySelectorAll('main a[href*="/track/"]')].map(a=>a.getAttribute('href').split('/track/')[1]?.split('?')[0]).filter(Boolean).slice(0,5)
            }))()''') or {}
            if state.get('host') == 'accounts.spotify.com' or state.get('login'):
                raise ValueError(LOGIN_REQUIRED)
            if state.get('name') and state.get('ids'):
                break
            await asyncio.sleep(0.5)
        else:
            raise ValueError('Spotify did not load the current daylist. Retry after checking its browser session.')
        # Capture only this page's Copy link result, never the system clipboard.
        await browser.js(r'''(() => {
            window.__mornyeDaylistShareLink = null;
            const capture = value => {
                if (/https:\/\/open\.spotify\.com\//.test(String(value)))
                    window.__mornyeDaylistShareLink = String(value);
            };
            navigator.clipboard.writeText = async value => {capture(value);};
            navigator.clipboard.write = async items => {
                for (const item of items) {
                    if(item.types.includes('text/plain')) capture(await (await item.getType('text/plain')).text());
                }
            };
            const originalCopy = document.execCommand.bind(document);
            document.execCommand = (command, ...args) => {
                if (command.toLowerCase() === 'copy') {
                    const active = document.activeElement;
                    capture(active?.value || String(window.getSelection()));
                }
                return originalCopy(command, ...args);
            };
            document.addEventListener('copy', event => {
                capture(event.clipboardData?.getData('text/plain') || String(window.getSelection()));
            }, true);
            const buttons = [...document.querySelectorAll('main button[data-testid="more-button"]')];
            if (!buttons.length) throw new Error('Missing playlist actions');
            buttons[0].click();
        })()''')
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            link = await browser.js('window.__mornyeDaylistShareLink')
            if link:
                result = {'url':checked_share_url(link), 'name':state['name'], 'track_ids':state['ids']}
                await browser.call('Page.navigate', url='about:blank')
                return result
            # Spotify translates these labels according to the saved browser locale.
            await browser.js('''(() => {
                const items=[...document.querySelectorAll('[role="menuitem"]')];
                const copy=items.find(x=>/^(copy (song |playlist )?link|copiar (el )?enlace)/i.test(x.innerText.trim()));
                if(copy) {copy.click();return;}
                const share=items.find(x=>/^(share|compartir)$/i.test(x.innerText.trim()));
                if(share) {
                    share.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
                    share.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerType:'mouse'}));
                    share.click();
                }
            })()''')
            await asyncio.sleep(0.4)
        raise ValueError('Spotify did not expose its current daylist share link. No saved snapshot was downloaded.')


def current_daylist():
    return asyncio.run(resolve())


if __name__ == '__main__':
    try:
        print(json.dumps(current_daylist(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':str(error)}, ensure_ascii=False))
        raise SystemExit(1)
