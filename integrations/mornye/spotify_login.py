#!/usr/bin/env python3
"""Spotify Authorization Code with PKCE; password stays in Spotify's browser UI."""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import httpx

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / '.env')

REDIRECT = 'http://127.0.0.1:8765/callback'
STATE_DIR = Path(os.getenv('MORNYE_HOME', str(Path(__file__).resolve().parents[2] / 'runtime/mornye'))).expanduser().resolve() / 'state'

def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or not sys.argv[1].isalnum():
        raise SystemExit('Usage: spotify_login.py CLIENT_ID\nRedirect URI: ' + REDIRECT)
    client_id = sys.argv[1]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != '/callback' or query.get('state') != [state]:
                self.send_response(400)
                self.end_headers()
                return
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('Authorization received. Check the terminal to confirm the connection.'.encode())
    with HTTPServer(('127.0.0.1', 8765), Handler) as server:
        server.timeout = 5
        url = 'https://accounts.spotify.com/authorize?' + urlencode({
            'client_id': client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
            'scope': 'playlist-read-private playlist-read-collaborative',
            'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})
        print('Open this URL in the browser on the computer running the SSH tunnel:\n\n' + url + '\n', flush=True)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get('code'):
        raise SystemExit('Authorization was not completed. Run spotify_login.py again.')
    response = httpx.post('https://accounts.spotify.com/api/token', data={
        'client_id': client_id, 'grant_type': 'authorization_code', 'code': result['code'],
        'redirect_uri': REDIRECT, 'code_verifier': verifier}, timeout=30)
    if response.status_code != 200:
        raise SystemExit('Spotify rejected the authorization exchange. Check the Client ID and Redirect URI.')
    data = response.json()
    data.update(client_id=client_id, expires_at=time.time() + data['expires_in'])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    temp = STATE_DIR / 'spotify_token.tmp'
    temp.write_text(json.dumps(data))
    temp.chmod(0o600)
    temp.replace(STATE_DIR / 'spotify_token.json')
    print('Spotify connected. Send /daylist <daylist URL> to the bot in private chat.')
if __name__ == '__main__':
    main()
