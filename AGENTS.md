# Project map

Preserve the existing four-tab PySide6 UI, configuration, durable audio queues,
and provider consent rules. Source audio is Latvian. Never commit `.env`, local
credentials, `.appdata`, captured audio, or session databases.

- `audio.py`: capture, energy segmentation, DynamicBoundaryPolicy, ordered playback.
- `engine.py`: ordered durable processing, session IDs, synthesis and device routing.
- `services.py`: local/OpenAI STT, Gemini/web/OpenAI translation, Cloud/web TTS.
- `sermon_context.py`: conservative current-segment terminology matching; no APIs.
- `main.py`, `ui_compat.py`: main UI, settings/lifecycle, dark palette and scrolling.
- `dialogs.py`, `summary_workspace.py`: summary worker lifecycle, history and report UI.
- `session_manager.py`: SQLite sessions, transcript rows and summary metadata.
- `voice_dialog.py`, `languages.py`: catalog discovery, per-language voices and locales.

Run `.venv\Scripts\python.exe -m unittest discover -s tests -q`. In a sandbox,
set TEMP/TMP and CHURCH_TRANSLATOR_APPDATA to directories under `build` first.
Use `run.py --smoke-test build\source-smoke.json` for isolated Qt/audio checks.
The smoke command never calls external providers or changes real user settings.
`scripts/verify_long_capture.py build\soak.json --dynamic` checks a two-hour
synthetic sample stream. Real service checks are separate, require network
access and permission to send fixed fixtures, and may incur usage charges.

Build with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
scripts\package_windows_app.ps1 -SkipDependencies`. Then run the packaged EXE
with `--smoke-test`, check 100/150/200% scaling, and run
`scripts/audit_windows_release.py`. Rebuild after the final application edit.
The public ZIP must contain blank keys and no installed local data.

Do not equate synthetic tests with natural sermon, audible quality, speaker
diarization, Windows 10, or external-provider acceptance. See the latest release
report for the tested boundary.
