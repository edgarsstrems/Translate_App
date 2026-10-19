# Church Sermon Translator — October 17, 2026

## Implemented

Only the AI Sermon Summary window received a visual redesign. The main dashboard, navigation, audio routing, and existing settings styles were retained. Functional additions are the Dynamic option, paid fallback authorization, and a Cloud Voice Settings button/dialog.

The summary was redesigned again to follow the supplied report reference within the existing dark navy/teal design. A tinted executive overview, numbered core points and amber-marked speech highlights replace the previous eight-section form. The generation prompt describes what the speaker actually said, using only the transcript; it does not add Bible references, advice or a closing reflection. Empty optional sections stay hidden. Older saved summaries display their overview, core points and existing illustrations without rewriting their database record just by opening it.

The report is selectable/read-only until **Edit** is enabled. Inline fields wrap and grow inside one outer scroll; point addition/removal appears only during editing. **Copy**, **Export** (TXT/Markdown/DOCX/PDF), **Save**, English/Latviešu, and **Close & Return to Dashboard** remain readily available. The Edit menu offers raw-text and transcript correction. Language changes save edits in the previous language before loading the selected language's saved report. Provider/model metadata is retained for newly generated summaries. The supplied Apple-design guidance informed hierarchy, spacing, typography and immediate copy/save feedback; the main application design remains intact.

Summary generation validates structured JSON, rejects missing central messages and wrong field types, and makes at most one repair request. Short inputs are explicitly limited; empty source text is rejected. Long inputs are processed in ordered sections and combined, including the last section. Unsupported historical Gemini candidates were removed. Discovery is restricted to supported, documented free-tier-eligible text endpoints; account billing still applies. Translation, summary and recovery generation share request pacing. Cancellation never permits a second worker to overlap the unfinished request; closing/Escape cannot destroy a running worker.

Source recognition is saved to SQLite before translation. Transcripts therefore survive Gemini/OpenAI failures. Retries do not duplicate saved segment IDs; coalescing preserves the original source rows. Timestamp conversion now uses wall time for history rather than interpreting a monotonic clock as a date. Pause/resume uses sermon-wide segment numbering. Session storage can initialize a session without deadlocking.

## Dynamic speech segmentation

Git history commits `211d1d1` and `6675925` contained the prior adaptive/pre-roll implementation. The upgrade reuses the current recorder's exact prefix consumption, carry-over, pre-roll and independent capture worker.

Dynamic analyzes 20 ms energy windows and tracks speaker level. Sustained phrase pauses vary approximately from 0.44 to 0.78 seconds based on phrase length and learned cadence. Short mid-phrase pauses stay together; quiet speech can lower the relative energy threshold. A hard 8-second cap applies even if a caller supplies a longer duration. Forced cuts retain continuation metadata for transcription/translation context. There is no duplicated overlap and no extra model call for boundary decisions. Silence is a practical phrase-boundary signal, not perfect grammatical sentence detection.

Capture continues during transcription, translation, synthesis and playback. Ordered workers use disk-backed queues; backlog is reported. Callback memory is bounded to approximately one minute of frames, with an explicit stop/error if storage stalls. Shutdown cannot deadlock on a full callback queue whose consumer has already failed. Failed synthesis retains its translation rather than acknowledging empty audio. Late audio remains in chronological playback order. Repeated recognized phrases are flagged for review with audio retained, because genuine speakers may repeat themselves.

## Gemini and paid OpenAI fallback

Gemini remains primary. Eligible candidates are `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`, verified against the account's model catalog and restricted to text generation. Working selection, cooldowns, alternatives and recovery probes remain in place. Model listing alone does not prove permission to generate or free billing status.

Paid live translation fallback defaults OFF. Legacy autosaved ON values require a fresh explicit opt-in; the new consent marker then persists. Enabling fallback during a recovery wait takes effect without restarting the sermon. Authorized fallback uses the existing key and selected model, tracks approximate usage/cost, and retains the existing warning/limit controls. Output budget scales with text and target-language count. Multilingual failure does not trigger duplicate individual paid submissions. Summary fallback always asks separately for that generation, even when live translation fallback is authorized. No real paid OpenAI translation or summary fallback was used during verification.

**Live Gemini blocker:** model discovery succeeds, but generation on both eligible endpoints returns `403 PERMISSION_DENIED`: “Your project has been denied access. Please contact support.” The application reports this as a project-access restriction. The user must resolve the associated Google AI Studio project's restriction or configure an authorized project/key. Changing models did not resolve it. No credentials or account permissions were changed.

## Cloud voices

The configured Google Cloud account returned 69 compatible English, 31 Lithuanian and 18 Russian voice IDs. Model-only names that cannot be synthesized through the existing voice-name API are filtered out. Examples actually present in the catalog:

| Language | Existing default preserved | Additional discovered examples |
| --- | --- | --- |
| English | `en-US-Standard-J` | `en-US-Wavenet-J`, `en-US-Neural2-J`, `en-US-Chirp3-HD-Aoede` |
| Lithuanian | `lt-LT-Standard-B` | `lt-LT-Chirp3-HD-Aoede` and other Chirp 3 HD voices |
| Russian | `ru-RU-Standard-D` | `ru-RU-Chirp3-HD-Aoede` and other Standard/Wavenet voices |

Each language retains its own voice, speed, supported pitch and volume gain. Pacing presets are Clear & Natural, Calm Speaker, and Fast & Clear. Unsupported pitch/style controls are not exposed; Chirp 3 HD has pace control and no pitch control in this integration. New voices are selected from the live catalog; premium changes require confirmation. Preview synthesizes a short language-specific sample through the configured output/volume and is blocked during an active sermon.

Google's published prices checked for this release, after applicable free allowances: Standard/WaveNet $4, Neural2 $16, Chirp 3 HD $30, Studio $160 per million characters. These are provider list prices, not a calculation of this account's bill. Existing defaults were not upgraded automatically. Premium synthesis quality and real room listening remain unverified.

Sources: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Cloud voice catalog](https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types), [Chirp 3 HD controls](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd), [Cloud TTS pricing](https://cloud.google.com/text-to-speech/pricing).

## Verification evidence

- Automated API behavior is mocked. Current full test results: `build/summary-report-all-tests.log`.
- Dynamic two-hour accelerated soak: 973 ordered segments, maximum 8.0 seconds, every nonzero sample preserved in order by SHA-256 comparison, no errors. Evidence: `build/dynamic-two-hour-soak.json`.
- Source Qt/audio smoke passed: four tabs, real microphone frames, silent output, routing reindex/disconnect handling, settings restart, three Start/Stop wiring cycles with fake network services, summary editing/saving/resizing, voice dialog and dependency imports.
- Summary report checks cover 640×560, 800×800 and 860×720 logical windows, including English, Latvian and edit mode. Qt-generated screenshots are in `build/`.
- Live services: Google model discovery and all three voice catalogs succeeded; configured Standard Cloud TTS synthesis succeeded; `gpt-4o-transcribe` recognized a synthetic Latvian speech fixture correctly. Evidence: `build/provider-catalog-2026-10-17.json` and `build/live-services-2026-10-17.json`.
- Live Gemini translation, summaries and the complete live serial chain remain blocked by Google's project denial. Fallback consent, routing/recovery, summary success/errors/cancellation, preview playback, ordering and persistence were verified with mocks.
- Desktop capture returned unrelated foreground content, so it was not used as visual acceptance evidence. Layout checks use actual Qt-rendered widgets and automated interactions; physical mouse/device listening and a full natural sermon were not verified.

## Windows distribution

The established PyInstaller folder build and C# launcher are retained. Executables:

- `dist-app/ChurchTranslator/ChurchTranslator.exe` — standalone application; keep its entire adjacent folder.
- `ChurchTranslator.exe` — rebuilt source/bootstrap launcher.
- `dist-app/ChurchTranslator-Windows.zip` — distributable folder archive.

There is no installer in the existing packaging workflow. Local `.env`, credentials, glossary and user data are preserved. The public archive uses an explicit root allowlist and a blank-key `.env` template, excluding credentials and local session/settings directories. Final packaged smoke and archive audit evidence is recorded below after the final build completes.

Optional local Whisper CUDA acceleration still requires compatible NVIDIA runtime DLLs. The tested cloud STT path and packaged imports do not require those optional GPU DLLs. Service quotas and sustained real-world throughput can still cause a backlog; this upgrade does not promise real-time performance when providers are slower than the speaker.

## Previous distribution verification (before report redesign)

- **164/164 automated tests passed** after the final source change (`build/tests-release-final.log`).
- Final clean PyInstaller build and C# launcher compilation succeeded (`build/package-2026-10-17-final-verified.log`).
- The exact final standalone EXE passed smoke tests at **100%, 150%, and 200% scaling**, each with exit code 0. Evidence: `build/verified-exe-dpi1.json`, `build/verified-exe-dpi15.json`, and `build/verified-exe-dpi2.json`.
- All **18 application modules extracted from the EXE** match the current source's compiled code (`build/packaged-source-parity.json`).
- Public ZIP audit passed: no configured API keys/private key matches, no local credential/session/settings paths, empty template keys, and no pytest dependency. Final sizes/hashes are recorded in `build/public-release-audit.json`.
- Standalone EXE SHA-256: `9034bb1265c3b201a5c29c123232d85a7e2fbeb81d620640c3eb5ce05b9eca69`.
- Packaged smoke checks use mocked services for Start/Stop and summary content; real hardware capture/silent output and Qt rendering are checked separately from live service calls. Physical room listening, premium voice synthesis, natural full-sermon throughput, and successful live Gemini generation remain unverified for the reasons above.

## Report redesign verification

- **174/174 automated tests passed**, including ten new report regressions (`build/summary-report-all-tests.log`). They cover the three-section contract, read/edit mode, point removal and renumbering, copy feedback, export contents, language switching with unsaved edits, legacy data preservation, long-text wrapping and the new generation response format.
- Source Qt smoke passed with actual rendered English and Latvian layouts, compact/default window sizes, and edit/save interactions (`build/summary-report-source.json`). A visual check caught and corrected stale rows during language changes. Screenshots contain fixture summaries, not live generated output.
- New report generation and error/consent paths were tested with mocked providers. This UI revision made no live provider requests. The Google project-access block described above remains unresolved.
- The rebuilt standalone EXE passed smoke at **100%, 150% and 200% display scaling**, including English/Latvian report rendering and edit/save interaction (`build/summary-report-exe-dpi1.json`, `build/summary-report-exe-dpi15.json`, `build/summary-report-exe-dpi2.json`). All **18 modules extracted from that executable match the current compiled source** (`build/summary-report-source-parity.json`).
- Report-redesign EXE SHA-256: `e44df3fefd1028235df7f012ee9ff8093c366a8575f107552dc0ad458a463d76`. The public archive audit records its own hash, credential exclusions and template checks in `build/public-release-audit.json`. Executable hashes in this section predate the output-language update below.

## Selectable audio languages update

The Dashboard's **Selectable Audio Translation** now has a searchable native dropdown with scrolling and 62 alternatives to the fixed US English output. Its list comes from all 63 named-voice locales returned by the configured Cloud account on October 17, 2026. Language/region names, translation prompts, JSON keys, selected-language display, Cloud synthesis, voice catalog requests, saved preferences and voice controls use the same registry. The second audio device/volume remain dedicated to the selected output. Source recognition stays Latvian. The summary language selector remains English/Latvian.

Existing English, Russian and Lithuanian voice choices are preserved. New defaults use catalog-backed Standard voices where present. Croatian, Slovenian and Swahili currently have only premium defaults in this catalog; first use asks for authorization, which persists with the selected voice. Invalid typed search text cannot leave the display showing one language while routing to another. Regional choices use suitable base codes for the existing web fallback, retaining distinct Simplified/Traditional Chinese codes.

**182/182 tests passed** (`build/output-languages-all-tests.log`), including eight new behavior tests covering all language/voice settings, every Cloud voice/locale pairing, Dashboard routing/restart, valid search choices, regional fallback codes and premium voice consent. Source Qt/hardware smoke passed with Japanese fixture text and actual dropdown rendering (`build/output-languages-source.json`). Google catalog listing succeeded; no new translation or speech-synthesis request was made. Catalog listing and mocked routing do not prove natural speech quality in every language. The existing Gemini generation restriction is unchanged.

The rebuilt executable and archive are checked using the existing DPI smoke, extracted-module parity and credential audit workflow. Latest output-language evidence is stored under `build/output-languages-exe-dpi*.json`, `build/output-languages-source-parity.json`, and `build/public-release-audit.json`.
