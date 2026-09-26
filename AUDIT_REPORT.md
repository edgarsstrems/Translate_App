# Application audit and implementation report

Date: 20 September 2026. Workspace: `C:\Users\edgki\Downloads\Translate_App-main`.

The existing layout, colors, typography, navigation and four tabs are preserved. This work changes the recording/processing internals and fixes functional defects. Existing staged changes were retained; no commit or reset was performed.

## 1. Inspection scope

Reviewed the application modules (`audio`, `engine`, `services`, `config`, `glossary`, `main`), tests, configuration template, glossary data, launcher, batch files, all PowerShell build/setup/uninstall scripts, assets and packaging layout. Traced capture through playback and all four tabs: Dashboard, Audio Routing, AI Models & Keys, and Audio Levels & Languages. Inspected credential handling without printing secrets. Generated dependency files were treated as build output, not application source.

## 2. Files changed

- `church_translator/audio.py`: independent capture/segmentation and durable ordered playback; device identity.
- `church_translator/engine.py`: ordered disk-backed stages, draining shutdown, continuation metadata, raw transcript fidelity.
- `church_translator/services.py`: transcription defaults/requests, conservative Gemini instructions/context, bounded TTS retries and timeouts.
- `church_translator/config.py`: atomic settings/backup, validation, defaults, credential unlink behavior.
- `church_translator/glossary.py`: bounded contextual hints and compatibility with both glossary field names.
- `church_translator/main.py`: persistent routing, asynchronous Stop, safe widget access, masked credentials, functional fixes with the design retained.
- New `church_translator/backlog.py`, `reliability.py`, `smoke.py`: persistent FIFO, retry/redaction helpers, packaged diagnostics.
- `run.py`: early startup diagnostics for explicit smoke runs.
- `scripts/package_windows_app.ps1`, `package_app.ps1`, `uninstall.ps1`: safe cleanup boundaries and packaging fixes.
- New `scripts/public_zip.py`, `export_backlog.py`, `verify_long_capture.py`, `verify_live_services.py`.
- `tests/test_translation_pipeline.py` and new `tests/test_continuity.py`.
- `.env.example`, `README.md`, this report. The ignored source `.env` model default was updated without exposing its contents.

## 3. Requested improvements implemented

Continuous capture independent of services; lossless active-segment partitioning; recent-pause boundary selection; strict 10-second ceiling; carry-over; bounded contextual translation; conservative ASR handling; recommended GPT-4o Transcribe; FIFO synthesis/playback; persistent backlog; atomic settings and device restoration; bounded retries; draining Start/Stop; debug retention; safer credential display; fresh Windows packaging and executable diagnostics.

## 4. Additional audit findings fixed

Old queues deliberately dropped speech when full or old. Identical/near-identical transcript suppression removed legitimate sermon repetitions. Regex replacement rules could change literal wine into a pronoun and alter Latvian verb forms. A second VAD could trim quiet audio after capture. Timer-driven sentence stitching added up to 16+ seconds and created ordering races. Stop cleared pending speech before it could drain. Output routing could match an unrelated device reusing an old index. Cloud TTS permanently switched off after one transient error. Credential dialogs displayed full keys. An explicit credential unlink could be undone by automatic file discovery. Background workers read Qt widgets directly. Packaging deleted editable configuration and could bundle secrets. Most significantly, PyInstaller resolved a conflicting ICU DLL from an unrelated Poppler installation on PATH, preventing QtCore from loading in the EXE.

## 5–6. Continuous capture and independence from APIs/TTS

The PortAudio callback copies audio into a handoff queue. A dedicated segmentation worker handles frames and writes completed segments to SQLite. Transcription, translation and synthesis have separate FIFO workers; each enabled language has its own ordered playback worker. No API call, disk write or playback runs on the microphone callback. Slow services accumulate pending work on disk instead of dropping it. Each queue decodes only its current item into memory.

This is an architectural guarantee against application-induced pauses caused by API/TTS work, not a guarantee against hardware/OS input overflow, device loss, process termination or exhausted storage. Those conditions are reported where detected.

## 7–10. Segmentation, ceiling, carry-over and boundary integrity

Energy is analyzed in 20 ms frames. A roughly 0.7-second pause normally flushes after the configured minimum duration. Short breaths do not immediately flush; long silence can flush a short utterance. Near the maximum, a recent low-energy pause in the last 35% of the segment is preferred. If none exists, the prefix ends at exactly the configured ceiling, clamped to 10 seconds.

Every split consumes one exact prefix and keeps the entire remainder. No overlapping audio is introduced. A 0.4-second rolling pre-roll protects onset; Stop flushes even a very short final tail. The engine no longer applies a second destructive VAD. Quiet nonzero audio is retained rather than treated as disposable noise. Idle near-digital silence can age out of pre-roll; the system does not claim to archive every silent sample. Energy boundaries approximate phrases, not grammatical sentence completion.

## 11–12. Latvian recognition and default model

OpenAI receives mono 16 kHz PCM audio in an in-memory lossless file, explicit `language="lv"`, JSON response format and temperature 0. GPT-4o Transcribe is the new configuration/template default and is labeled recommended in the existing selector. Whisper-1, GPT-4o Mini Transcribe and local Whisper remain selectable. Existing saved user choices are preserved.

The prompt uses a concise Latvian instruction, limited glossary vocabulary and at most 25 recent words/300 context characters. Automatic deletion of repeated sentences, prompt-like phrases and suspected English words is removed from the live path. Suspected recognition-language issues are marked uncertain. Empty ASR results retain their audio in an `unrecognized` backlog for inspection.

Preprocessing removes DC offset and uses peak-limited gain capped at 3x. A waveform test verifies DC removal, no clipping and correlation above 0.99999; this is not a measured improvement in real church word-error rate. No unnecessary resampling is performed: capture requests 16 kHz directly. Devices that cannot open this format report an error rather than silently mislabeling samples.

## 13–16. Gemini context, split sentences and fidelity

Gemini receives recent Latvian/translated segments, glossary hints and continuation information. Forced boundaries mark possible continuation even if ASR inserted punctuation. Context is stored for eight entries, includes up to six, limits fields to 1,200 characters, and caps the context prompt at 10,000 characters. It does not grow with sermon length.

The actual Gemini system instruction requires faithful translation of only the current segment; it forbids answering spoken questions, commentary, summaries, invented theology, invented sentence endings and repeating old context. Corrections require both close Latvian phonetics and strong immediate context. Literal wine and non-divine pronouns must be preserved when appropriate. These constraints reduce risk but cannot guarantee that a generative model never makes an error.

Fragments are translated promptly with context instead of waiting for a long sentence-stitch timer. This reduces delay but cannot eliminate all audible awkwardness across forced linguistic boundaries.

## 17. TTS

Google Cloud TTS remains the intended configured speech provider. Synthesis has its own queue, so playback or TTS does not hold up capture or Gemini. Each output consumes generated audio in FIFO order, drains the device before acknowledging completion, and preserves failed/current/later items. Empty output is an error. TTS network calls have a 15-second per-request timeout and bounded transient retries. A transient failure no longer permanently disables Cloud TTS. Playback is never automatically retried after a write failure because some audio may already have been heard.

Existing web alternatives remain for configurations that intentionally lack Cloud credentials; they are not used as a silent replacement for failed configured Cloud TTS.

## 18–19. Routing and settings

Routing preferences include host API, name and channel capabilities, with legacy name/index migration. Reordered indices restore correctly. Missing devices retain a disconnected preference instead of silently becoming another device. Missing inputs require reconnection/selection; missing outputs use the existing explicit default-output fallback message. Public PortAudio identities are not Windows endpoint GUIDs, so identical devices can remain ambiguous.

Model choices, languages, volumes and routing persist in the existing settings file. Atomic replacement, fsync and a previous-valid backup reduce corruption risk; malformed field types are ignored/clamped. Configuration and voice settings remain in `.env`; glossary remains editable beside the executable. API-key writes are atomic, and credential unlinking stays unlinked. Model choices and settings are not overwritten merely to apply new defaults.

## 20–23. Reliability, retries, latency and architecture

Removed stale-age expiry and full-queue drop behavior. SQLite queues require acknowledgement after a successful stage. Each stage owns its mutable service/context state. Clients are reused and closed. The GUI remains responsive during draining, and a new session cannot start while the previous session is still draining. Normal completed queue files are removed. Pending work stays for explicit recovery; debug recording retains the newest 120 WAV files; transcript/log widgets remain bounded.

OpenAI and Cloud TTS retry transient failures up to three attempts with backoff/jitter; authentication/configuration errors fail promptly. Gemini retains bounded model failover and request timeouts, without silently switching away from explicitly selected Gemini. Persistent failures pause the affected stage and report retained work. Capture can continue while a downstream stage is paused. No unbounded automatic retry loop is used.

Latency improves through separate stages and removal of the long stitch timer. No captured work expires to manufacture a low latency figure. Sustained processing slower than the sermon necessarily builds delay; a large backlog also makes Stop take longer to drain.

## 24–26. Tests and continuity results

| Check | Result |
|---|---|
| Original baseline suite | 24 passed before changes |
| Final automated suite | 37 passed |
| Forced segmentation with irregular 733-sample input blocks | Exact array equality, including final 73-sample tail |
| Breath-boundary carry-over | Exact concatenated sample equality |
| Very short/quiet speech and mixed pauses | All nonzero input samples retained in order |
| Accelerated two-hour capture | 7,200 simulated seconds; 776 segments; max 10.0 seconds; no errors |
| Two-hour streaming sample hash | Input/output nonzero-sample SHA-256 identical |
| Slow-service pipeline | 20 queued segments retained and played in order while STT was blocked |
| Persistent FIFO reopen | 100 items retained; unacknowledged item recovered first |
| Playback device failure simulation | One write attempt; current and next item retained; no automatic duplicate playback |
| Atomic settings recovery | Previous valid backup loaded after simulated corruption |
| Retry classification | Transient retry succeeds; authentication failure attempted once |
| Context stress | 1,000 large entries remain within configured bounds |
| Source GUI smoke | Four tabs, settings restart, device index/disconnect restoration, three Start/Stop UI cycles passed |
| Hardware smoke | 34 inputs and 52 outputs enumerated; real microphone frames received; silent output stream written |
| Dependency consistency | `pip check`: no broken requirements |
| Live Gemini | Correct English rendering of the short Latvian fixture |
| Live Google Cloud TTS | 4.88375 seconds of valid English audio generated |
| Live GPT-4o Transcribe | Synthetic Latvian fixture transcribed; one verb-tense difference (`runājam` → `runājām`); names retained |

The two-hour test is accelerated synthetic sample handling, not a real-time two-hour church recording or a two-hour cloud-service endurance test. Start/Stop UI cycles use mocked services; worker ordering/draining is separately tested. The real-service check is small and uses synthetic Latvian audio, not a ground-truth sermon corpus. Physical unplug/replug and a PC reboot were not performed.

Evidence files are in `build/verification`: `two-hour-capture.json`, `live-services.json`, `source-smoke.json`/PNG, `exe-smoke.json`/PNG, `windows-build.log`, and the installed dependency-version snapshot.

## 27–28. Windows build and executable

The first packaged launch exposed QtCore DLL error 0xc0000139. Diagnostics traced it to Poppler's incompatible `icuuc.dll` collected from the external shell PATH. The build script now restricts dependency resolution to Python and Windows paths. Removing the conflicting generated DLL confirmed the diagnosis: the packaged smoke test then exited 0 with all checks passed. The final clean rebuild and smoke results are recorded below.

Executable: `C:\Users\edgki\Downloads\Translate_App-main\dist-app\ChurchTranslator\ChurchTranslator.exe`

Public archive: `C:\Users\edgki\Downloads\Translate_App-main\dist-app\ChurchTranslator-Windows.zip`

Final clean-build verification: **PASS**. The rebuilt EXE exited 0. All four tabs, settings restart, routing reindex/disconnect restoration, real microphone capture (7,904 frames), silent output, packaged dependency imports and three Start/Stop UI cycles passed. The final dependency manifest contains no Poppler/Codex-runtime paths and no conflicting bundled `icuuc.dll`. No source changes were needed after this final binary was built.

## 29. Remaining limitations and recovery

- ASR/translation can still misrecognize or mistranslate words. The live fixture already showed one tense error. Real Latvian sermon recordings are needed for meaningful accuracy comparisons.
- Energy detection cannot perfectly identify sentences or distinguish music/noise from speech. Preserving quiet input can increase API usage and unrecognized-audio storage.
- Audio not delivered by disconnected/overflowing hardware cannot be reconstructed. Such conditions are surfaced; physical recovery may require Stop/reconnect/Start.
- Finite disk space and OS scheduling still matter. Pending backlogs contain sermon audio/text and must be reviewed/removed when no longer needed.
- Persistent API failures retain work but require intervention. `scripts/export_backlog.py` exports it without replaying it. There is no automatic live resume/replay UI.
- Cross-stage persistence is not one database transaction. An abrupt crash between successful enqueue and acknowledgement can leave duplicate recoverable work. A playback failure can leave a partially heard current item. Review before replaying; crash-proof exactly-once output is not claimed.
- Stop intentionally drains captured work. With a large backlog or a local model download, completion can take time.
- Device identities use PortAudio metadata, not immutable hardware GUIDs. Indistinguishable devices may require manual routing selection.
- Optional CUDA DLLs are not installed/bundled; local Whisper retains CPU fallback. A fresh local Whisper model download/full offline transcription was not exercised.

## API references checked

- [OpenAI transcription request parameters](https://developers.openai.com/api/reference/cli/resources/audio/subresources/transcriptions/methods/create)
- [Gemini generateContent and system instructions](https://ai.google.dev/api/generate-content)
- [Google Cloud TTS Python client timeouts and retries](https://docs.cloud.google.com/python/docs/reference/texttospeech/latest/google.cloud.texttospeech_v1.services.text_to_speech.TextToSpeechClient)

## Follow-up: project access denial and short Stop tail (2026-09-20)

Direct Gemini REST verification reproduced HTTP 403: "Your project has been denied access. Please contact support." This is a Google project restriction, independent of SDK and app; changing model presets cannot repair it. The owner must resolve access with Google. No alternate provider is silently substituted.

The app now checks Gemini access before microphone capture, explains project denial without retrying other models, and offers an asynchronous Test API Key button. A configured key is no longer labeled verified/active. Short final OpenAI audio requests retain all samples and append silence to reach 0.5 seconds. A recognition error after capture has started is no longer mislabeled a startup error.

Regression suite: 40 tests passed, including 80 ms audio preservation/padding, project denial with exactly one API attempt, and access checks that do not add sermon history. Live Gemini translation remains blocked by the provider restriction.


## Latest follow-up: latency, pause noise and free-tier recovery (2026-09-20)

This follow-up supersedes the earlier capture-threshold, context-budget and retry descriptions above. Default chunks are now 2.5–5 seconds with 0.45-second pause flush. Low-level idle noise is gated before onset/auto-gain/recognition; genuine repeated words remain untouched. The recognizer prompt no longer includes the previous transcript. Gemini retains two context entries within 2,500 characters.

Only stable 3.5 Flash-Lite (recommended) and 3.1 Flash-Lite are offered. Both passed live compatibility checks, at 0.84s and 0.88s on one synthetic sentence. Official documentation still lists 2.5 Flash-Lite, but the configured project returned 404/no longer available to new users, so it was excluded. Gemini 3 uses minimal thinking rather than unsupported budget=0/retry-with-default-reasoning. Request timeout is 10s: an initial 6s probe was rejected because Google's minimum is 10s. SDK retries are disabled; one application layer owns retries/failover.

Successful requests are paced at 4.2s for the project's displayed 15 RPM limit. Rate limits and unavailable services cause cooldown/failover, stick to a working alternative and automatically resume when eligible. Daily/model exhaustion does not clear cooldowns. Adjacent queued text is coalesced transactionally, preserving all text and crash recovery. Recovery waits can be interrupted by Stop, retaining backlog. The UI shows automatic recovery; its latency is no longer overwritten by fast STT while translation is behind.

Live serial synthetic fixture: OpenAI transcription 3.27s, Gemini 0.64s, Google TTS 0.41s; 4.31s from completed capture to audio ready. Transcript and translation matched the fixture. This does not include recording time or prove whole-service reliability. Evidence: build/verification/flash-lite-benchmark.json and live-services-low-latency.json. Free quotas remain a hard external limit; keys within one project share quotas.

The final default is `Automatic Flash-Lite`. On every Start, a background catalog request checks the key's current model list without spending a content-generation request. The filter accepts only stable Gemini 3.1-or-newer text Flash-Lite endpoints advertising `generateContent`; previews, latest aliases, image/audio variants, heavy Flash/Pro models, and 2.5 are excluded. Verified 3.5 and 3.1 models are preferred to protect free-tier throughput, with newly discovered stable Flash-Lite endpoints available afterward. Catalog lookup never blocks microphone capture and falls back to the verified order if unavailable.

## Quiet-input follow-up (2026-09-20)

Amplitude is no longer used to discard a captured segment or suppress onset. Every non-zero input frame can start capture, with rolling pre-roll before it. The pre-recognition stage removes DC and applies shape-preserving gain toward 0.075 RMS, capped at 12× and by 0.92 peak headroom. The durable queue continues to store original audio; only the upload copy is amplified. A live synthetic Latvian fixture reduced to 0.000315 RMS was boosted 12× and OpenAI returned every fixture word exactly (evidence: `build/verification/quiet-speech-live.json`). This establishes the tested behavior, not a universal recognition guarantee for inaudible, muted, clipped, corrupted, or provider-misrecognized input.

Uploads use mono 16 kHz PCM16 inside lossless FLAC with 0.65 compression. Only exact-zero leading/trailing samples beyond a 250 ms guard are removed; all non-zero samples remain, and very short requests retain the 0.5-second compatibility padding. This reduces upload size without lossy speech damage. OpenAI documents FLAC support and audio-token pricing, so file compression is treated as a bandwidth/latency optimization rather than a claim of lower billing by byte count.
