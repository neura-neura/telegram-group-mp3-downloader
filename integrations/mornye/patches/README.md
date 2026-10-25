# Mornye 1.0.4 compatibility patches

These unified diffs apply to the unmodified `mornye` package from upstream tag `v1.0.4`. Use `../install.py` from the separate Mornye environment; it performs dry runs before modifying installed files and records hashes for repeat-install checks.

- `download_engine.patch`: resume tagging and transcoding of existing source audio, and include already-present tracks in CSV JSON reports.
- `signed_session_mobile.patch`: provider-supported mobile callback flow, bounded verification recovery, and optional shared browser transport.
- `solver.patch`: strict provider grant capture and verification cancellation.
- `../server_browser_network.py`: shared provider-origin browser requests, private socket transport, and cancellation when the caller disconnects.

The patch installer copies the gateway module into `mornye/core/`. These changes preserve recording-identity validation and TLS verification. Provider changes can still break compatibility.

Upstream: https://github.com/mornyeapp/Mornye-Python

Copyright and permission notice: [LICENSE-Mornye](../LICENSE-Mornye).
