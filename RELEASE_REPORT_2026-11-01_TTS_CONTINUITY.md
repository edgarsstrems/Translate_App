# TTS continuity — November 1, 2026

Finalized translations now have bounded pre-synthesis independent of shared-output
admission. Consecutive ready audio uses the active output stream. Original speech,
voice choices, saved settings, source capture and both transcription transports
are preserved.

## Findings from the installed session

The previous release was `07180a7`. Read-only inspection of the latest completed
installed session found four English/Yoruba batches sharing one playback queue.
Only numeric timing metadata was exported into ignored build evidence; no sermon
text, audio, credentials or databases are included in Git or the public package.

- English end-to-end synthesis calls took 1.906, 1.765, 1.813 and 0.860 seconds.
  The first English playback callback followed its translation event by about
  2.071 seconds; playback queue wait was 0.032 seconds. Capture-to-output estimate
  was 15.782 seconds, which also includes capture, recognition and translation.
- Later English playback queue waits were 12.969, 26.546 and 46.875 seconds.
  Those English chunks had already been synthesized. Earlier English and Yoruba
  speech occupied the shared physical output, rather than English waiting for
  synthesis. Yoruba's initial synthesis took 8.407 seconds; later calls took
  1.047–2.390 seconds.
- Playback completion/start audit callbacks were about 0.148–0.173 seconds apart.
  These callbacks and the reported 0.182-second device latency are estimates,
  not recordings of physical sound. They do not prove that every reported silent
  pause was reproduced or rule out embedded silence or driver effects.
- Baseline synthesis measurements include provider, initialization, conversion
  and retries together. They cannot establish pure Google request latency or
  isolate authentication time. No new paid/provider requests were made.

There was no translation-to-synthesis batching timer: the durable synthesis job
was already created before the UI update. Realtime and file recognition converge
on this same ordered translation/synthesis path; their capture/reconnect code
was not changed. Existing durable queues use condition notifications, not polling.

Two avoidable paths were confirmed in source and offline reproduction: output
stop/start per chunk, and a voice worker waiting for shared-output admission
before it could prepare further text. Different voice sample rates also reopened
the device. A deliberately slow five-second restart reproduced 5.020-second gaps
between already-ready chunks in the actual baseline player; this is a stress
reproduction, not a measurement of the user's driver.

## Changes

1. Each language keeps its existing durable synthesis worker. A separate
   admission worker consumes `synthesis-ready-<language>.sqlite`, with at most
   two prepared chunks per language. Encoded audio becomes durable before its
   text job is acknowledged. Waiting for another language does not occupy the
   synthesis worker. Backpressure keeps later text on disk without dropping it.
2. Physical-output admission retains original batch/language order. A required
   failed earlier voice holds later shared-output audio for explicit recovery;
   it is no longer silently bypassed. Independent devices remain independent.
   Optional Yoruba failures retain the established durable deferral policy.
3. Playback writes ready same-format chunks to the existing active stream.
   It drains/stops when no following audio is available, at an unsupported
   format change, on failure or at shutdown. There is no intentional sentence
   pause or polling interval. Pausing capture still allows accepted speech to
   drain, and End Session waits for pending speech as before.
4. Playback FIFO claims stay on disk until a conservative completion estimate
   based on the PortAudio clock, reported latency and final submitted block,
   or a successful idle/final backend drain. Write acceptance alone never
   acknowledges playback. Failed output retains unacknowledged audio, including
   possibly partially played chunks; recovery never automatically replays it.
5. Different mono/stereo sample rates with matching channel counts use the
   existing bundled FFmpeg resampler for a playback-only derivative. This keeps
   duration and pitch, avoids reopening the output between Cloud/local voices,
   and leaves original encoded bytes intact. Channel-count changes still drain
   and reconfigure the device.
6. Session voice workers prepare reusable local Google credentials/channel
   resources before waiting for the first translation. Preparation does not
   synthesize, refresh a token or block capture. Token refresh and network
   establishment can still occur during the first actual provider request.
   Failed preparation falls back to the normal synthesis path.
7. TTS timing writes and playback callbacks use a separate reporting worker.
   Audit SQLite latency and UI latency reporting cannot block the audio writer
   or hold up the next synthesis request. Shutdown flushes these observations
   before saving the final integrity report. A deterministic test blocks an
   audit callback while all four ready chunks continue writing and drain.

The configured default remains **en-US-Chirp3-HD-Iapetus, rate 0.95**. No automatic
speed changes, skipped/overlapping sentences, summarization, translation-word
changes or preference migrations were introduced.

## Diagnostics and embedded silence

Monotonic metadata now follows translation-ready, synthesis queue entry, worker
start, client preparation, each provider attempt/response, encoded-audio readiness,
shared-output eligibility, playback queue entry, decoding/resampling, successful
first write, start/end estimates and observed completion. Failures retain attempt
timings too. Logs separate synthesis queue wait, total synthesis, measured provider
request time and shared-output wait. Raw audit events support calculating retries,
backoff and initialization separately without printing the translated words.

Per-output logs show current and rolling-average gaps, audio-not-yet-eligible
time and estimated avoidable transition time. PortAudio timing is explicitly
labeled as estimated; no hardware-loopback measurement is claimed. A prior
completion callback may still be pending when the next buffered write occurs.
Durable audit event envelopes use UTC write time; queued TTS events additionally
retain UTC observation time. The pipeline measurements inside their payloads use
process-local monotonic time.

Exact digital leading/trailing zero padding is measured without changing samples.
No representative retained Cloud output was available from the drained session,
so excessive Google padding was not confirmed and no trimming was added. Quiet
phonemes, breaths and natural punctuation/prosody are preserved.

## Verification

Evidence is under ignored `build/tts-continuity/`.

- Ready-chunk restart stress: baseline maximum gap **5.020 s**, fixed virtual
  DAC gap **0 s**, one start/drain instead of 40 starts/drains. Sample order and
  PCM are preserved. `final-boundaries.json` also measures real wall-clock
  scheduling with SQLite/decoding and a mocked blocking device: maximum observed
  boundary work **16 ms** across 60 English chunks, below the 200 ms target.
- Two-hour ready-playback simulation: **24,000** ordered chunks, zero simulated
  gaps, no lost/duplicated/reordered PCM, one stream start/final drain.
- Accelerated two-hour English/Yoruba service: **900** source rows, 300 translation
  batches, all rows delivered per language, nine injected malformed responses
  repaired, all translation/synthesis/ready queues drained. This exercises
  translation, history, summaries and both independent synthesis lanes.
- Targeted tests cover real wall-clock boundary scheduling, bounded pre-synthesis
  behind shared-output ordering, no duplicate synthesis, retained ready-audio
  recovery, output disconnects/FIFO acknowledgements, differing voice sample
  rates, unchanged padding, delayed/failed startup, and slow-provider timing.
  Existing suites cover both recognition architectures, pause/resume, End Session,
  original audio integrity and language failure/deferral.
- **393** complete unit/regression tests passed in **96.524 seconds**, with
  external sockets and Google token refresh blocked. Eleven focused continuity
  tests include a blocked audit callback that cannot stall subsequent writes.
- Final 180-second Dynamic file pipeline: 22 ordered translation/synthesis/output
  batches, exact original/encoded-sample checks and zero pending work. Final
  180-second Realtime pipeline: 28 ordered batches, forced disconnect/reconnect,
  exact original hash, no duplicate commits and zero pending/review work.
- Final source Qt/audio smoke passed, including the four tabs, fullscreen,
  display settings, native Realtime imports, counted microphone frames (not
  retained/uploaded) and digital-silence output.
- Two muted real-device runs used the production player: eight English-only
  chunks and eight shared English/Yoruba chunks with alternating 24/16 kHz
  inputs. Both drained in order with no reported underflows and a maximum
  estimated avoidable gap of **31 ms**. Reported device latency was 182 ms.
  These are PortAudio estimates using digital silence, not audible/loopback proof.

- The Windows package was rebuilt after the final application edit. Packaged
  smoke passed at **100%, 150% and 200%** scaling, including narrow navigation,
  fullscreen, all four tabs, settings roundtrips, native Realtime imports and
  digital-silence real-device output. Narrow 200% navigation and 150% fullscreen
  screenshots were visually checked. **All 33 bundled application modules match
  final source.** Test host: Windows build 26300; Windows 10 was not available.

The final public ZIP uses the existing explicit file allowlist and blank-key
environment template. Promotion to the normal paths is gated on a successful
`scripts/audit_windows_release.py` source-parity/privacy audit. The audit's final
result and archive SHA-256 are retained locally under
`build/tts-continuity/release-audit.json`. Final release paths are
`dist-app/ChurchTranslator/ChurchTranslator.exe` beside `_internal`, and
`dist-app/ChurchTranslator-Windows.zip`. The previous binaries/archive are kept
locally under `build/tts-continuity/rollback/`; installed settings and credentials
are preserved. Changes are committed on the existing `main` branch and pushed to
the existing remote after verification.

## Practical limits

On one shared physical output, Yoruba speech necessarily occupies airtime between
English chunks. Continuous English listening requires English-only output or
independent physical outputs for the two languages. The release preserves saved
routing and never removes another language's speech to improve English latency.
When generated speech exceeds available output time, backlog still grows and is
reported honestly.

Offline/provider simulations do not establish audible quality, natural church
acceptance, recognition/translation accuracy, exact Google/auth latency, physical
speaker gaps, Windows 10 behavior or a two-hour hardware endurance run. Windows 11
package verification cannot certify every Windows driver. No private recordings
were uploaded and no new paid API calls were made.
