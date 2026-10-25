# Project map

Preserve the existing four-tab PySide6 UI, configuration, durable audio queues,
and provider consent rules. Source audio is Latvian. Never commit `.env`, local
credentials, `.appdata`, captured audio, or session databases.

- `audio.py`: capture, energy segmentation, DynamicBoundaryPolicy, ordered playback.
- `engine.py`: ordered durable processing, session IDs, synthesis and device routing.
- `services.py`: local/OpenAI STT, Gemini/web/OpenAI translation, Cloud/web TTS.
- `sermon_context.py`: per-document DOCX body/table parsing, local section/topic indexing,
  bounded recognition vocabulary and current-source translation guidance; no APIs.
- Supported OpenAI STT uses `audio.transcriptions.prompt` with prepared vocabulary
  and bounded previous recognized Latvian. Sparse prompted output is checked once
  without references; disagreement retains audio for review. Nonoverlapping audio
  must never trigger transcript deduplication. Whisper uses a 224-byte prompt cap.
- `main.py`, `ui_compat.py`: main UI, settings/lifecycle, dark palette and scrolling.
- `dialogs.py`, `summary_workspace.py`: summary worker lifecycle, history and report UI.
- `session_manager.py`: SQLite sessions, transcript rows and summary metadata.
- `voice_dialog.py`, `languages.py`: catalog discovery, per-language voices and locales.
- `translation_quality.py`: conservative empty/malformed/copied-response checks;
  one same-provider repair attempt, preserving paid-fallback consent.
- Translation requests isolate `current_source` from `reference_only`. Context
  may disambiguate words; never translate reference material. Only one bounded
  prior source segment is supplied; generated translations are never fed back.
  Failed quality checks retry once with reference material removed.
- Capture profiles: Balanced 4–5s, Low Latency 2–3s, Dynamic natural pauses with
  a 6s soft target and 8s hard limit (DYNAMIC_SOFT_SECONDS/DYNAMIC_HARD_SECONDS).
  Stop/Pause may flush a shorter final tail. Explicit context
  applied while idle is staged for one session; Stop still clears it.
- `session_manager.py` stores explicit translation batch links; readable exports
  align those groups, while JSON preserves every source row and its group ID.
- End Session saves completion/queue counts and remembers the completed session
  for summaries after Stop/restart. Summary content is saved speech, not the draft.
  Summary language is session-specific (`summary_preferences`); new sessions
  default to Latvian independently of legacy global settings.
- Uncertain OpenAI recognition retains raw/selected text and reconciliation with
  audio in the durable review queue. A failed sparse-output check must hold the
  unverified candidate for review rather than speaking it.
- `scripts/evaluate_sermon.py`: offline PDF/DOCX extraction (optional document
  analysis dependencies); `verify_sermon_regressions.py` replays the curated
  `verification/translation-quality-fixtures.json` synthetic cases without APIs.
  Private sermon cases/analysis stay in ignored `private-audit/`; pass that
  dataset explicitly with `--dataset`. Historical English is
  evidence, never a translation target; prepared text is not spoken ground truth.
- `scripts/verify_service_simulation.py`: accelerated 60-minute text/queue soak
  with simulated providers and audio; separate from natural-service acceptance.
- `scripts/verify_live_ui.py <report>`: 1,000 synthetic segments per live panel,
  real queued Qt signals, resizing, selection and pixel-stable manual reading.
  Transcript panels retain all text; only the activity log rolls over. Status
  label geometry must stay stable so incoming log messages cannot resize panels.
- Navigation/status stay outside page scrolling; tab pages use their own scroll
  areas. Header smoke checks cover 640–1280 logical pixels and 100/125/150/200%
  Qt scaling. Zero/unavailable ADC timestamps are not evidence of audio loss.
- `scripts/verify_spoken_latency.py <report> --live`: one fixed synthetic service
  chain and two muted real-device output segments, including Iapetus at 0.95.
  Does not change saved voices or prove audible quality; uses configured API usage.
- `scripts/compare_sermon_context.py <build/report> --docx <path> --cases <json> --live`:
  bounded same-audio A/B; reports may contain sermon content and stay ignored.
  Natural WER requires checked_reference, never the prepared DOCX or old ASR.
- `scripts/verify_completed_summary.py <report> --live`: real Qt/Gemini generation,
  regeneration and History, with synthetic saved speech and simulated capture drain.

Run `.venv\Scripts\python.exe -m unittest discover -s tests -q`. In a sandbox,
set TEMP/TMP and CHURCH_TRANSLATOR_APPDATA to directories under `build` first.
Use `run.py --smoke-test build\source-smoke.json` for isolated Qt/audio checks.
The smoke command never calls external providers or changes real user settings.
`scripts/verify_long_capture.py build\soak.json --dynamic` checks a two-hour
synthetic sample stream. Real service checks are separate, require network
access and permission to send fixed fixtures, and may incur usage charges.
Use `--mode balanced`, `--mode low_latency`, or `--mode dynamic` to check each
actual capture profile. `scripts/verify_context_capture.py <report> --live` tests
fixed synthetic context canaries and real TTS/STT; it never uploads private sermons
or microphone capture and preserves existing paid translation consent.

Build with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
scripts\package_windows_app.ps1 -SkipDependencies`. Then run the packaged EXE
with `--smoke-test`, check 100/150/200% scaling, and run
`scripts/audit_windows_release.py`. Rebuild after the final application edit.
The public ZIP must contain blank keys and no installed local data.
If existing packaged native files deny replacement, use
`-DistDirectory dist-app/<release-name>` for packaging and package verification,
and `--dist-directory dist-app/<release-name>` for the release audit. Do not
change file permissions or terminate a user's running application to rebuild.

Do not equate synthetic tests with natural sermon, audible quality, speaker
diarization, Windows 10, or external-provider acceptance. See the latest release
report for the tested boundary.
