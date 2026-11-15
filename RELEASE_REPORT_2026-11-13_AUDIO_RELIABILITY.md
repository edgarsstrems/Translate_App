# Audio reliability release — November 13, 2026

The implementation adds output identity/format diagnostics, safe route changes,
bounded duration correction, per-language monitoring and local Yoruba readiness
improvements. The church shared-output silence has **not been reproduced or
explained conclusively**. This release must still be checked with the actual
church interface, console USB returns and speakers. Accepted PCM writes do not
establish physical sound or pronunciation quality.

## Changes and confirmed findings

- Output selections now resolve Windows default/mapper aliases before grouping
  languages and freeze the resolved index for the session. Shared output probes
  native-rate float32 stereo, then mono on the same endpoint if stereo is rejected.
  No format failure silently selects another endpoint. Identity, backend, rate,
  channels, first-channel routing, headroom, volumes, nonzero PCM, underflows and
  errors are recorded in the Activity Log and local speech-audit events.
- A real additional backend defect was reproduced: the single-language player
  failed on this PC's WASAPI output with `Invalid sample rate` for 16/24 kHz
  source voices, while the shared mixer succeeded at native 48 kHz stereo.
  The single-output path now falls back to the probed native format on that
  same endpoint and converts subsequent playback PCM to the open format.
  Invalid format requests are not retried unchanged. Real single/shared checks
  then passed for both MME and WASAPI. This does **not** establish the cause of
  the church's reported shared-only silence.
- The existing independent synthesis/admission workers were retained. Shared
  output still uses one stream with per-language FIFO lanes, 1/N headroom and
  independent volume. Changed mixer volumes ramp over up to 5 ms. Empty/failed
  lanes do not block other lanes. A format/open failure before any audio is
  submitted gets three bounded attempts on the same endpoint. A write failure
  never automatically replays potentially partial speech.
- Confirmed lifecycle defect: an invalid output selection/initialization error
  during startup could leave a recording lease and workers open. Startup now
  performs cleanup. Player Stop is idempotent.
- Capture can be paused to change the existing output dropdowns. A background
  worker drains affected outputs before regrouping shared/separate streams.
  Per-language routing locks protect admission; unchanged devices keep running.
  Failed/partial output requires End Session and explicit retained-audio review.
- Confirmed backlog mechanism: playing every generated second at a fixed rate
  accumulates delay when generated speech exceeds its available source time.
  Previous code warned but provided no duration correction. A per-language
  deterministic controller now applies pitch-preserving FFmpeg `atempo` to the
  playback derivative, with 2.5 percentage-point steps between chunks, a default
  maximum of 1.15x and a 20-second release cooldown. No transcript/translation
  edits, dropped sentences, source-audio changes or paid resynthesis occur.
  Original encoded TTS audio remains in the durable playback queue until drain.
  Filter failures play the original and report the failure. Short tails remain
  at natural speed. A 60 ms padded tail protects the final filter window.
- Defaults: natural speed without backlog; correction begins at 2 seconds of
  pending audio, becomes stronger at 6 seconds and reaches the configured cap
  at 12 seconds. These are conservative tested engineering limits, not a claim
  of listener-validated intelligibility. Excess duration at the configured limit
  and a substantial backlog produces a warning; content remains retained.
- The compact tab-corner display now shows each language's latest output-start
  estimate and pending speech. Warning indicators and the tooltip distinguish
  TTS slowness/backlog, playback backlog, duration limits and stopped output.
  Average/P95 and rate appear in the tooltip. Monitoring refreshes at 1 Hz using
  memory-only snapshots; CPU and working-set samples are logged at most every
  30 seconds on the existing diagnostics worker. Mixer pending duration now
  decreases as PCM is submitted rather than counting already-submitted speech.
- A single-output timing inconsistency was fixed: capture-to-output latency now
  uses the same buffered start estimate as its detailed start timestamp.
- Yoruba already cached its loaded model; no per-sentence reload defect was
  found. It now warms on its own worker before the first text in both Yoruba-only
  and bilingual configurations, defaults to two
  CPU inference threads (configurable 1–4), and logs load time/thread count.
  Ready status requires pinned file integrity and discoverable runtime modules;
  successful inference still validates actual loading. Verified unchanged files
  are not repeatedly hashed/downloaded. Missing packaged runtime is distinguished
  from missing model files. The installed button says Installed / Ready.
- Existing Latvian STT, Gemini translation, saved model/voice choices, source
  segmentation and privacy/consent rules are preserved. Dynamic still has a
  six-second soft target at a real pause and a ten-second hard maximum. The
  existing translation prompt already requests natural spoken language without
  explanations while preserving meaning/repetition; no accuracy-risking prompt
  shortening or provider switch was introduced.

FFmpeg documents pitch-preserving tempo adjustment in its
[atempo filter](https://ffmpeg.org/ffmpeg-filters.html#atempo). The installed VITS
implementation and [upstream VITS documentation](https://huggingface.co/docs/transformers/main/model_doc/vits)
provide duration controls, but an unbenchmarked model/runtime/quantization
replacement was deliberately not introduced. Saved voices and rates remain in use.

## Before/after evidence and its boundaries

No live STT, Gemini or Cloud TTS request was made. No private church recording
or sermon was uploaded or used. Natural-service latency, physical audible latency
and Yoruba inference performance on the church PC therefore remain unmeasured.

| Metric in a real service | Before this work | After this work |
|---|---|---|
| English transcript latency | Not measured here | Not measured here; STT unchanged |
| English audio-ready latency | Not measured here | Not measured here |
| English audible playback latency | Not measured here | Physical audio unverified |
| Yoruba synthesis latency | Not measured here | Model unavailable locally; not measured |
| Yoruba audible playback latency | Not measured here | Physical audio unverified |
| English / Yoruba service backlog | Not measured here | Pending PCM is now observed per lane |
| Shared-device playback | Prior simulated tests are not church acceptance | Nonzero PCM accepted on this PC; acoustic output unverified |
| Single-language WASAPI format | Baseline rejects 16/24 kHz source rates | Same endpoint accepts native 48 kHz playback derivatives |
| CPU / RAM | Offline comparator below | Offline comparator below; not local-model inference |

The deterministic timing model separately compares disabled/enabled correction
using identical source cadence and provider-time assumptions. These are modeled
results, **not measured provider or physical audible latencies**. It uses measured
durations of real PCM passed through the production tempo filter. The 60-minute,
six-second-segment case injects 1.8 s STT, 1 s translation, English TTS around
1.8 s, Yoruba TTS 3.2 s, 50 ms output latency, generated/source ratios 1.059/1.04,
occasional STT delays and a one-second pause after each fourteen segments.

| Timing-model metric | Correction disabled | Correction enabled |
|---|---:|---:|
| Shared STT P95 | 7.80 s | 7.80 s |
| English audio-ready P95 | 10.90 s | 10.90 s |
| English output-start P95 | 198.10 s | 13.24 s |
| Yoruba output-start P95 | 135.32 s | 14.77 s |
| Maximum English playback backlog | 196.87 s | 3.06 s |
| Maximum Yoruba playback backlog | 129.68 s | 2.85 s |

The same model covers 5/30/60/120 minutes, ten-second continuous segments and a
slow Yoruba worker. Repeated ten-second segments exceed the 15-second target
under these assumptions even with correction. A 7.2-second Yoruba synthesis
step cannot sustain six-second arrivals; its modeled delay grows while English
results remain identical to the normal-Yoruba case. Speed correction cannot fix
an inference throughput deficit. A 15-second guarantee is not claimed.

Ignored reproducible evidence: `build/overhaul/duration.json`,
`mixer-before.json`, `mixer-after.json`, `shared-mixer.json`, `output-real.json`,
`capture-*.json`, `dynamic-two-hour.json`, `unit-tests.txt`, `source-smoke.json`
and packaged verification reports under that directory.

## Verification and packaging

Source validation includes **420 passing tests (15 added)** with external
sockets and Google token refresh blocked, and production PCM integration for each
single language, shared/separate outputs, pause/resume and End Session. Additional
tests cover ordered shared → separate → shared changes, unchanged-device reuse,
mute/unmute and volume changes, bad decoded PCM, initialization retries, partial
write retention, original-byte preservation and Yoruba integrity/runtime status.
The final suite passed after the native-format fix.

- Two-hour mixer: 2,400 completions, all 115,200,000 overlapping output frames,
  6,000 distinct-sentence PCM checks, one stream and zero ready-to-ready lane
  gaps. Real SQLite/decoding/resampling/mixing; virtual output clock/device.
- Thirty-minute capture/journal/request/full-pipeline checks for all three modes
  preserve all 28,800,000 original samples per mode and drain all queues. Dynamic
  produced 203 segments (10 s maximum), Balanced 314 (6 s maximum), Low Latency
  457 (4 s maximum). Providers and
  physical devices are mocked; this establishes byte/queue continuity, not WER.
- Two-hour Dynamic partition check: all 115,200,000 input samples preserved in
  order, 810 segments, 10 s maximum and no errors.
- Real output: English alone, Yoruba alone and shared output accepted nonzero
  fixed tones on MME (44.1 kHz shared stereo) and WASAPI (48 kHz stereo after
  format negotiation). No acoustic loopback/listening measurement was made.
- Rendered UI stress: 1,000 synthetic segments per panel, selection retained,
  manually scrolled viewport pixel-identical, no removed document blocks and
  GUI-only updates. The original four tabs and corner status remain.
- Sixty-minute English/Yoruba text-pipeline simulation: all 450 source rows
  translated/admitted in 150 linked batches, five injected malformed responses
  repaired, queues drained, summary prompt covered all source rows and saved
  summary history reopened successfully. This uses mocked providers/audio and
  does not measure natural translation accuracy or audible playback.

The offline two-hour PCM comparator used fresh processes and baseline git commit
`9508f91b5fb8e36cbbf9447a37df64ed4076ec37`. Baseline CPU was **40.31 process CPU
seconds**, with **81.65 MiB peak working set**; the updated run used **55.88 CPU
seconds**, with **81.32 MiB peak working set**. These accelerated verifier runs
had different concurrent host load and include fixtures, SQLite and diagnostics;
they are not an isolated performance comparison or a claim of CPU reduction.
They exclude Yoruba inference and do not establish older-PC resource usage.

Packaged EXE smoke tests passed at **100%, 150% and 200% Qt scaling**, including
640–1280 logical-pixel navigation, saved settings/restart, three mocked Start/Stop
cycles, SDK imports and the actual bundled FFmpeg duration filter. The packaged
WASAPI output test passed for English, Yoruba and shared output, with no API
requests and no acoustic verification. **All 37 application modules match final
source**. The release ZIP is checked for source parity, blank template keys,
known credential leakage and forbidden private paths before promotion; its
machine-readable final audit is `build/improvement-release-audit.json`.

Eight existing configuration/glossary/credential files are protected by
before/after SHA-256 checks. Promotion replaces generated binaries/resources,
preserves installed local configuration/data in place and retains prior native
binaries/archive under `build/overhaul/rollback/`. Offline success is not
acceptance for a real church service.

Final release layout:

- `C:\Users\edgki\Downloads\My Projects\Translate_App-main\dist-app\ChurchTranslator\ChurchTranslator.exe`
- `C:\Users\edgki\Downloads\My Projects\Translate_App-main\dist-app\ChurchTranslator-Windows.zip`

The EXE sits beside `_internal`; the ZIP has blank keys and excludes installed
credentials and local session data. No user's running application is terminated.

## Settings and no-cost diagnostics

New optional environment settings, shown in `.env.example`:
`PLAYBACK_ADAPTIVE_ENABLED`, `PLAYBACK_MAX_SPEED`,
`PLAYBACK_BACKLOG_WARNING_SECONDS`, and `YORUBA_CPU_THREADS`. Existing `.env`,
credentials, glossary and saved settings are preserved. Set
`PLAYBACK_ADAPTIVE_ENABLED=false` to retain natural-speed playback with warnings.

The packaged app supports an offline output test without Python/developer tools:

```powershell
& '.\ChurchTranslator.exe' --output-test '.\output-test.json' --audible
```

It sends short quiet 440/660 Hz tones for English alone, Yoruba alone and shared
output. Without `--audible`, it writes zeros. `--device N` selects an enumerated
PortAudio endpoint; default selection is recorded in the JSON report. The test
never records the microphone/other applications or contacts an API. A passing
report means software/device-write checks passed; a person still needs to confirm
that the intended speakers can be heard. USB console output uses the first
channel or first stereo pair, which must be routed through the console to the
intended speakers/headphones. No claim is made that BEHRINGER WING-USB was the
church's selected output: only its use as input was confirmed by the user.

## Remaining church-computer acceptance

Check the exact output endpoint/backend and console return routing, listener
audibility for single/shared/separate outputs, local Yoruba initialization/CPU
throughput/pronunciation, sustained natural sermon accuracy and output delay,
physical unplug/replug behavior, and Windows 10. Run the diagnostic at safe
speaker levels first. Check microphone gain and the actual saved voices/rates.
The new activity/audit diagnostics should distinguish zero PCM, muted lanes,
format/stream failure, software backlog and a downstream console/speaker route.
Keep the prior release available until this acceptance succeeds.

Paid API calls: **zero**. Paid AI usage: **€0 / $0**. No new service, model
download, credential change, OpenAI translation fallback or billing change.
