"""Private Mornye hook; emits only counters to the worker's per-run status file."""
import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_RESULTS = {}

def on_track(result, metadata):
    target = os.environ.get('MORNYE_PROGRESS_PATH')
    if not target:
        return
    status = 'skipped' if result.success and result.skipped else 'downloaded' if result.success else 'failed'
    rank = {'failed': 1, 'skipped': 2, 'downloaded': 3}
    with _LOCK:
        key = str(metadata.id)
        if rank[status] >= rank.get(_RESULTS.get(key), 0):
            _RESULTS[key] = status
        data = {name: sum(value == name for value in _RESULTS.values()) for name in rank}
        data['current'] = str(metadata.title)[:160]
        path = Path(target)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False))
        temp.replace(path)
