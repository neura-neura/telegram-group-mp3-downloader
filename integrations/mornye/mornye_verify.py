#!/usr/bin/env python3
"""Verify a Mornye audio provider entirely on this server."""
import asyncio,json,os,sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / '.env')

ROOT=Path(os.getenv('MORNYE_HOME', str(Path(__file__).resolve().parents[2] / 'runtime/mornye'))).expanduser().resolve()
os.environ.update(MORNYE_BROWSER_NETWORK='1', MORNYE_BROWSER_SOCKET=str(ROOT / 'state/gateway.sock'),MORNYE_DATA_DIR=str(ROOT/'mornye-data'),CHROME_PATH=os.getenv('CHROME_PATH','/usr/bin/google-chrome'),TS_DEBUG_VISIBLE='1',PATH=str(ROOT/'.venv/bin')+':'+os.environ.get('PATH','/usr/bin:/bin'))
from mornye.core.signed_session_mobile import client_from_manifest,auth_backoff_remaining,_clear_auth_backoff,_record_auth_failure,_retry_after_from
async def main():
 name=sys.argv[1] if len(sys.argv)>1 else 'tidal-web'
 if name not in {'tidal-web','qobuz-web','deezer','amazon'}:raise SystemExit('Use mornye_verify.py tidal-web, qobuz-web, deezer or amazon')
 config=json.loads((ROOT/'mornye-data/extensions'/name/'manifest.json').read_text())['signedSession']
 client=client_from_manifest(config)
 if client.authenticated:
  print(name+': valid session.');return
 pause=auth_backoff_remaining(client)
 if pause>0:raise SystemExit(f'Verification paused; {round(pause)} seconds remaining.')
 try:
  await client.authenticate_with_turnstile(timeout=90)
  if not client.authenticated:raise RuntimeError('No valid session received')
  _clear_auth_backoff(client)
  print(name+': verification completed and saved.')
 except Exception as error:
  pause=_record_auth_failure(client,_retry_after_from(error))
  raise SystemExit(f'Verification failed ({type(error).__name__}); retry in {round(pause/60)} minutes.')
 finally:
  if client._client:await client._client.aclose()
asyncio.run(main())
