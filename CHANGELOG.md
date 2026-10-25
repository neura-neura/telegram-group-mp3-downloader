# Changelog

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

- Scheduled Telegram jobs stay quiet when there are no changes.
- Final summaries are compact; skipped-file details are available on request.
- Telegram downloads use dated folders and maintain an `All Tracks.m3u` with relative paths.
- Music progress edits a single message until completion.
- Runtime paths and owner IDs are configurable; host-specific settings are excluded from source.

Spanish command aliases remain available for existing users. Mornye upgrades require reviewing the pinned compatibility patches.
