#!/usr/bin/env python3
"""Apply the pinned, reviewable Mornye compatibility patches and optional registries."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
load_dotenv(PROJECT / '.env')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-extensions', action='store_true', help='Install extensions from both documented registries')
    args = parser.parse_args()
    if importlib.metadata.version('mornye') != '1.0.4':
        raise SystemExit('These patches require Mornye 1.0.4. Use the pinned requirements.')
    home = Path(os.getenv('MORNYE_HOME', str(PROJECT / 'runtime/mornye'))).expanduser().resolve()
    state = home / 'state'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    site = Path(sysconfig.get_paths()['purelib'])
    marker = state / 'patches-applied.json'
    patches = sorted((HERE / 'patches').glob('*.patch'))
    targets = [site / 'mornye/application/download_engine.py', site / 'mornye/core/signed_session_mobile.py', site / 'mornye/core/solver.py', site / 'mornye/core/server_browser_network.py']
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    if marker.exists():
        expected = json.loads(marker.read_text())
        if not all(path.exists() and expected.get(path.name) == digest(path) for path in targets):
            raise SystemExit('Installed Mornye differs from the patched version. Recreate its environment before reinstalling.')
        print('Mornye compatibility patches are already applied.')
    else:
        if not shutil.which('patch'):
            raise SystemExit('Install the system patch utility before continuing.')
        # Check every file before mutating any upstream source.
        for patch in patches:
            subprocess.run(['patch', '--dry-run', '--batch', '-p1', '-d', str(site), '-i', str(patch)], check=True)
        backup = home / 'upstream-backup'
        backup.mkdir(exist_ok=True)
        for path in targets[:-1]:
            shutil.copy2(path, backup / path.name)
        for patch in patches:
            subprocess.run(['patch', '--batch', '-p1', '-d', str(site), '-i', str(patch)], check=True)
        shutil.copy2(HERE / 'server_browser_network.py', targets[-1])
        marker.write_text(json.dumps({path.name: digest(path) for path in targets}, indent=2))
        marker.chmod(0o600)
        print('Applied Mornye 1.0.4 compatibility patches.')
    if args.install_extensions:
        os.environ['MORNYE_DATA_DIR'] = str(home / 'mornye-data')
        os.environ['MORNYE_MIN_TRUST'] = 'checksum-only'
        from mornye.extensions import ExtensionManager
        from mornye.extensions.registry_config import add_registry
        urls = ['https://raw.githubusercontent.com/mornyeapp/Mornye-Extension/main/registry.json',
                'https://raw.githubusercontent.com/zarzet/SpotiFLAC-Extension/main/registry.json']
        manager = ExtensionManager(auto_install_downloads=False)
        installed = set()
        for url in urls:
            add_registry(url)
            for entry in manager.fetch_registry(url):
                if entry.id not in installed:
                    extension = manager.install(entry.id, registry_url=url)
                    installed.add(entry.id)
                    print(f'Installed {entry.id} {extension.version}')

if __name__ == '__main__':
    main()
