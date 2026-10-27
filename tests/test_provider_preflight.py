"""Provider preflight uses fake sessions; it cannot contact an audio service."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location('worker_preflight', Path(__file__).resolve().parents[1]/'integrations/mornye/music_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class PreflightTests(unittest.TestCase):
    def check_failure(self, error, retry_after, expected):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            services = ['ext:first', 'ext:second']
            for name in ('first', 'second'):
                path = root/'mornye-data/extensions'/name/'manifest.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'signedSession':{'baseUrl':'https://provider.invalid','namespace':'same'}}))
            client = SimpleNamespace(authenticated=False, _client=None, authenticate_with_turnstile=AsyncMock(side_effect=error))
            module = ModuleType('mornye.core.signed_session_mobile')
            module.client_from_manifest = Mock(return_value=client)
            module.auth_backoff_remaining = Mock(return_value=0)
            module._clear_auth_backoff = Mock()
            module._record_auth_failure = Mock()
            module._retry_after_from = Mock(return_value=retry_after)
            with patch.dict(sys.modules, {'mornye':ModuleType('mornye'), 'mornye.core':ModuleType('mornye.core'), module.__name__:module}), patch.object(worker, 'ROOT', root), patch.dict(worker.os.environ):
                self.assertEqual(worker.prepare_sources(services), [expected])
            client.authenticate_with_turnstile.assert_awaited_once()
            module._record_auth_failure.assert_called_once_with(client, retry_after)

    def test_renderer_timeout_is_distinct_from_provider_verification(self):
        self.check_failure(RuntimeError('Mornye provider browser stopped responding'), 0, 'browser_unavailable')

    def test_provider_retry_after_is_respected(self):
        self.check_failure(RuntimeError('Retry later'), 3600, 'provider_wait')
