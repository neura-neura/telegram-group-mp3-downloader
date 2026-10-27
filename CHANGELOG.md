# Changelog

## Provider connection recovery

- Lazily start the provider browser and replace idle or unresponsive renderers before new requests.
- Invalidate timed-out signed requests without replaying them; distinguish connection failures from pending verification.
- Verify each shared provider session once per request and preserve provider retry pauses.
- Update the installed gateway when rerunning the compatibility installer.

## Unreleased

### Added

- Optional Mornye CLI integration for music links, Spotify daylists, and organized library exports.
- Spotify PKCE login helper, provider verification helper, and a private shared-browser gateway.
- Both Mornye and SpotiFLAC extension registries, with a version-checked compatibility patch installer.
- MP3 320 kbps and core-tag validation, temporary FLAC cleanup, and relative M3U exports.
- Cross-daylist skipping by exact Spotify track ID, leaving existing audio in place.
- Completed-request checks, specific concurrent-request responses, and storage/timeout errors.
- Descriptive collection M3U names and a new date-and-time-stamped M3U for each playable daylist request.
- English command names, menus, responses, setup guides, service templates, and offline workflow tests.

### Changed

- Plain daylist requests resolve the current personalized share URL using a dedicated Spotify browser, validate visible track IDs against Mornye, and never fall back to a stale saved URL. Metadata and downloads stay on Mornye.

- Equivalent song and single-track album exports share one validated static M3U, without modifying audio or daylist captures.

- Scheduled Telegram jobs stay quiet when there are no changes.
- Final summaries are compact; skipped-file details are available on request.
- Telegram downloads use dated folders and maintain an `All Tracks.m3u` with relative paths.
- Music progress edits a single message until completion.
- Runtime paths and owner IDs are configurable; host-specific settings are excluded from source.

Spanish command aliases remain available for existing users. Mornye upgrades require reviewing the pinned compatibility patches.
