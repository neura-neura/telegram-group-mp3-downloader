# Contributing

Use Python 3.12+, install `requirements.txt`, and install ffmpeg plus mutagen to run the offline tests:

```bash
python -m pip install -r requirements.txt mutagen
python -m unittest discover -s tests -v
python -m compileall -q telegram_group_mp3_downloader integrations/mornye
```

Keep documentation and user-facing messages in English. Preserve existing command aliases when adding or renaming commands. Cover changes to duplicate detection, filesystem cleanup, or concurrency with workflow tests; tests must not require account credentials or contact providers.

Keep secrets, logs, media, session state, personal playlist captures, private IDs, and machine-specific paths out of commits. Use configuration and generated fixtures. The Mornye bridge lives in a separate environment because its dependencies differ from the bot's.

When changing a compatibility patch, verify it against the pinned upstream version, retain its license notice, and document how it changes behavior. Do not weaken recording-identity checks or silently treat failed tracks as successful.
