# Church Sermon Live Translator

Windows desktop application for Latvian microphone audio → OpenAI transcription → Gemini English translation → Google Cloud Text-to-Speech → selected output. The existing four-tab design is preserved. Russian output and local Whisper remain available.

## Run and build

Run `run.bat`, or open `dist-app\ChurchTranslator\ChurchTranslator.exe`. Keep the executable beside its `_internal`, `assets`, and editable configuration files; this is a standalone folder distribution, not a single-file executable.

`build.bat` installs dependencies and creates a clean PyInstaller build. Developers with the tested dependencies installed can run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package_windows_app.ps1 -Public -SkipDependencies
```

Build cleanup removes generated binaries/work directories only. An existing installation's `.env`, credentials and glossary are preserved. The ZIP is always public: it contains a blank-key configuration template and no local credentials.

## Configure before a service

Use the existing AI Models & Keys tab for OpenAI, Gemini, and Google Cloud service account credentials. Select **gpt-4o-transcribe** for recommended Latvian recognition. New installations default to it; previously saved model choices remain intact. Choose microphone and English output in Audio Routing, then set language/volume. Keys are masked in dialogs. API usage and quotas depend on your provider account.

Settings are stored in `%LOCALAPPDATA%\ChurchTranslator\settings.json`, with an atomic replacement and a backup. Device matching includes host API, name and channel capabilities, rather than only a mutable Windows index. Disconnected preferences are retained. Names/capabilities are not hardware serial numbers; identically named interfaces may still need manual selection.

Advanced configuration stays in `.env`; `.env.example` explains the settings. TTS voices remain configurable through `TTS_ENGLISH_VOICE` and `TTS_RUSSIAN_VOICE`. Existing web translation/TTS alternatives remain for installations without cloud configuration. Selecting Gemini explicitly reports Gemini failure rather than silently changing providers. Configured Cloud TTS failures do not permanently disable Cloud TTS.

## Continuous recording

The PortAudio callback copies audio into a queue. A separate segmentation worker analyzes 20 ms frames and writes completed segments to a SQLite backlog. Network requests never run on the capture callback or segmentation worker. Separate ordered workers handle transcription, translation, synthesis, and each output.

Pauses of about 0.45 seconds normally end a segment after its minimum duration. Short breaths stay within a segment. Near the default 5-second ceiling, the segmenter prefers a recent low-energy pause in the last 35% of the segment. Otherwise it splits exactly at the ceiling. Only the prefix is emitted; the entire remainder stays in the next segment. There is no overlap. Pre-roll protects speech onset, and quiet onsets above the noise gate are retained. Idle device noise ages out of pre-roll and is checked again before gain/recognition. Energy detection estimates phrase boundaries; it does not identify linguistic sentence endings perfectly.

Forced-cut metadata and recent source/translation context help Gemini continue split sentences. Context is limited to eight stored entries, two included entries, 1,200 characters per field, and a 2,500-character context budget. The prompt forbids invented completions, commentary, theological rewriting, and repeating earlier context. The live pipeline preserves ASR text rather than applying phonetic replacements or deleting similar/repeated sentences.

`glossary.json` supplies local Whisper recognition vocabulary and translation guidance. Cloud OpenAI transcription deliberately receives no prompt or glossary terms, so ambiguous audio cannot echo seeded sermon vocabulary. Both `translation_terms` and the older `theological_terms` name are supported.

## Stop and failure recovery

**Stop ends capture, flushes the final audio, then drains pending speech.** The window remains responsive and Start stays disabled until the session finishes. Closing the window follows the same drain sequence. A large backlog takes time to drain; keeping every segment cannot also guarantee low latency when services are persistently slower than the speaker.

Transient OpenAI/TTS requests receive bounded retries. Permanent failures pause the affected stage and preserve its current item and subsequent work. Capture continues independently until Stop. Inspect the Activity Log for the session directory under `%LOCALAPPDATA%\ChurchTranslator\backlog`.

To export unfinished audio/text without modifying or replaying it:

```powershell
.\.venv\Scripts\python.exe scripts/export_backlog.py <session-folder> <export-folder>
```

Review the manifest before replay: the first failed playback item may have been partially audible. Recovery is an explicit export workflow, not automatic live replay. A process crash between separate stage commits can also leave a duplicate recoverable item. The application does not claim crash-proof exactly-once speech. Successful queue files are removed. Unfinished files remain until reviewed. They contain sermon audio/text and consume disk space. Debug recordings, when enabled, retain the newest 120 WAV files. GUI transcript/log histories are bounded.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
.\.venv\Scripts\python.exe scripts/verify_long_capture.py build/verification/two-hour-capture.json
.\.venv\Scripts\python.exe run.py --smoke-test build/verification/source-smoke.json
dist-app\ChurchTranslator\ChurchTranslator.exe --smoke-test build/verification/exe-smoke.json
```

Smoke mode uses isolated settings, exercises all tabs and settings restoration, checks actual audio enumeration/input/silent output, imports packaged libraries, and tests Start/Stop UI wiring with mocked network services. It never uploads microphone audio. An optional real API check synthesizes a small Latvian fixture:

```powershell
.\.venv\Scripts\python.exe scripts/verify_live_services.py dist-app/ChurchTranslator build/verification/live-services.json
```

That command uses the specified installation's credentials and can incur API usage. See `AUDIT_REPORT.md` for results and remaining limits. Synthetic continuity tests prove sample handling, not perfect Latvian word recognition or acoustic quality in a reverberant church. Hardware disconnections and exhausted disk space cannot be recovered into audio that the device never delivered.


## Live translation and Gemini free-tier limits

The recommended/default setting is **Automatic Flash-Lite**. At every session start the app lists the key's current Gemini catalog in the background, keeps only stable text `Flash-Lite` endpoints that support `generateContent`, and prefers verified `gemini-3.5-flash-lite`, followed by `gemini-3.1-flash-lite`. Newly discovered stable Flash-Lite models are additional fallbacks. Both preferred models accepted live requests with the configured project on September 20, 2026. Changing `latest` aliases, heavy reasoning models, previews, image/audio variants, and the unavailable-to-this-project 2.5 model are excluded. A catalog failure uses the last verified built-in order; a generation failure immediately tries the next eligible model.

Google's dashboard for this project shows 15 requests/minute and 500/day for 3.5 Flash-Lite. Successful translation requests are spaced by at least 4.2 seconds; pending adjacent text can be combined without waiting for a batch. Both target languages share one request in free-tier mode. Default audio timing is 2.5–5 seconds with a 0.45-second pause flush (short complete utterances can flush earlier after a longer pause). Translation and TTS run as soon as their inputs are ready. The displayed latency includes translation/synthesis delay rather than only recognition.

429/503/timeouts try another eligible Flash-Lite model. A working alternative remains selected for the session. Cooldowns honor provider retry timing; daily exhaustion waits instead of flooding the API. When all choices are temporarily unavailable, pending text remains in order and the worker retries automatically. Stop interrupts provider recovery and retains unfinished text on disk. Authentication/project bans still require account action. No software can guarantee uninterrupted translation after all available quotas are exhausted; 500 requests at one per five seconds is approximately 40 minutes, before other usage.

Every non-zero captured input sample is treated as potentially meaningful speech. The segmenter uses energy only to choose phrase boundaries and does not discard quiet words. Before recognition, DC offset is removed and quiet audio is raised toward a 0.075 RMS target, up to 12×, while peak headroom prevents clipping. The original capture remains intact in the durable queue until recognition succeeds. OpenAI receives no text prompt at all—not earlier speech and not glossary vocabulary—reducing invented or repeated words on ambiguous audio. Actual repeated spoken words are preserved; no text-similarity deletion is applied.

The OpenAI upload is mono 16 kHz, 16-bit lossless FLAC at moderate compression. Exact-zero outer silence is trimmed with 250 ms guard padding, while every non-zero sample is retained. Short tails are padded to the API-safe 0.5-second minimum. This reduces network bytes and may reduce audio tokens when true outer silence is removed; FLAC compression itself does not reduce token-based transcription charges.

The application is designed so it does not intentionally drop captured speech: the callback copies every input block, segmentation consumes exact non-overlapping prefixes, Stop flushes the tail, each durable stage acknowledges work only after success, and low volume is amplified before Whisper. Recognition remains dependent on the selected microphone, the audio reaching Windows, and the external recognition model, so no application can mathematically guarantee that an ASR service will spell every spoken word correctly. Keep the dashboard meter moving and avoid a muted or clipping mixer input.

References: [Google models](https://ai.google.dev/gemini-api/docs/models), [pricing/free tier](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [thinking settings](https://ai.google.dev/gemini-api/docs/thinking).
