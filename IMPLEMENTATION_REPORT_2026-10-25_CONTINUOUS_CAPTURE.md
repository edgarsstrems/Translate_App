# Continuous capture implementation — 25 October 2026

The local capture and queue changes pass automated verification. Natural sermon
recognition accuracy remains an open acceptance gate. No paid API requests were
made, no private audio was uploaded, and nothing was published.

## Findings

* The existing PortAudio callback was independent of API requests, but its
  bounded queue fed a worker responsible for both original-audio persistence
  and segmentation. Segment delivery, storage and status callbacks could delay
  that worker. Queue overflow aborted input and discarded the overflowing
  callback. A regression test now verifies retention of that packet.
* Model initialization occurred before opening input. Capture now starts before
  model readiness, so initialization latency becomes a durable processing backlog.
* Earlier history (`923db4c` and earlier) gated recognition on VAD energy. Quiet
  audio could be retained only for review rather than submitted. The starting
  checkout already submitted all nonzero segments; that behavior is preserved.
* Recognition checks can explain a missing displayed sentence independently of
  capture loss: failed reference-free checks and suspected prompt echoes hold
  candidates for review. Successful acoustic disagreement now keeps the original
  candidate, with uncertainty and both candidates retained, rather than suppressing
  it or substituting a longer guess. Failed checks remain held, as AGENTS.md requires.
* The repeat guard compared word membership without word order and could mark
  distinct clauses using the same vocabulary uncertain. It now compares complete
  ordered wording. Exact repetitions still receive review metadata and are kept.
* Shutdown counted review copies as unfinished pipeline work. Drained sessions
  now distinguish review items from pending recognition/translation/speech jobs.

The supplied screenshot shows a reference-free recognition check, low input level
and approximately ten seconds of displayed latency. It does not establish which
words were captured or omitted. No matching new recording or human-checked source
transcript was supplied, so these findings do not prove the cause of every reported
missing sentence or repair the quoted Latvian phrases.

## Architecture and behavior

`device → callback memory queue → dedicated original-PCM journal → segmenter →
durable STT queue → ordered translation queue → synthesis queue → ordered playback`

The callback copies samples and assigns sample positions. It performs no network,
disk, segmentation or UI work. A dedicated journal worker commits original float32
PCM and timing details to SQLite with FULL synchronization. It commits already
waiting packets together without a batching timer. The segmenter reads only
committed packets, verifies hashes and consumes exact nonoverlapping prefixes.
Provider requests and UI status updates cannot block the journal worker.

Callback memory is bounded to 600 normal 100 ms packets (about 3.84 MB PCM), plus
one overflow reserve and bounded worker batches. Segmentation and provider backlog
grow on disk. If storage itself cannot keep up, the reserved overflowing packet
is retained, input is explicitly stopped and the user receives an error. This
protects already-delivered samples; it cannot record future speech after an input
stop. Device overruns and ADC gaps remain warnings of unreconstructable possible
hardware loss. Zero/unavailable ADC timestamps remain unknown timing.

Dynamic retains adaptive pause detection, a six-second soft target and an
eight-second hard ceiling. A normal phrase pause is approximately 640–800 ms,
with a 320 ms breath eligible after the soft target. Continuous speech spans
consecutive ranges; 20 seconds without pauses produces 8 + 8 + 4 seconds, with
any final partial samples preserved. Balanced remains approximately 4–5 seconds;
Low Latency remains 2–3 seconds. Live parameter changes preserve buffered samples.

There is no overlap and no transcript boundary deduplication. Original PCM is
independent of upload preprocessing. Upload gain is capped at 4x with peak
headroom, replacing the 12x ceiling; this is a conservative noise-amplification
limit, not demonstrated recognition improvement. No words are synthesized to
fill missing content. Existing source-only context, Gemini-first translation,
fallback consent, saved settings and Iapetus 0.95 defaults are preserved.

Durable queues now retain stable enqueue receipts across reopen. Repeating an
acknowledged segment key cannot requeue translation or playback. These receipts
protect downstream delivery; they cannot make an external API request or physical
playback atomic with a local database transaction. Recovery remains explicit,
with pending queues, original PCM and local export tooling retained. There is no
automatic paid replay or guarantee against replay of a partly played crash item.

End Session still finalizes off the GUI thread, stops callbacks first, drains the
journal/segmenter, flushes the partial tail and drains downstream workers in order.
Capture finalization failure is reported without skipping downstream cleanup.
Transcript windows remain intact. Local `backlog/<session>/integrity.json` reports
captured/journaled/segmented samples, uncovered ranges, overlap, warning/overrun
counts, failed/recovered counts and pending/review jobs. Recognition, translation,
synthesis and playback-start timing remain distinct in internal diagnostics.

## Verification

* Full suite: **303 tests pass**, 56.271 seconds on this Windows host.
* The new worker test blocks segment delivery while all 20 seconds plus 73 samples
  reach the original journal. Release/drain reconstructs the exact original.
* Overflow, unsegmented crash tail, hardware warning, queue receipt reopen,
  quiet/zero/uneven frames, mode changes, model-readiness ordering and shutdown
  failure have deterministic regressions. Existing Pause/Resume, translation
  ordering and provider-failure tests remain passing.
* Identical accelerated 30-minute inputs use actual callback, journal, queue,
  encoded STT request and output-worker paths with mocked delayed providers:

| Check | Dynamic | Balanced |
| --- | ---: | ---: |
| Captured, journaled and logical samples | 28,800,000 | 28,800,000 |
| Recognition / translation / synthesis / output jobs | 235 each | 366 each |
| Maximum observed recognition backlog | 25 | 76 |
| Gaps / overlap / pending jobs / review items | 0 | 0 |
| Additional real API requests | 0 | 0 |

  The first recognition request waits until at least 60 simulated seconds have
  been delivered. Both modes reconstruct the same all-sample SHA-256. The test
  paces its accelerated producer against journal ingress capacity, not APIs;
  it is not a real-time hardware or recognition-accuracy test. Encoded PCM16 is
  compared to the exact expected quantized upload, while original float32 samples
  remain preserved. This distinction matters for extremely quiet input.
* Two-hour segmentation checks: **115,200,000 samples including zeros per mode**,
  identical hashes. Balanced 1,462 segments, maximum 5 s; Dynamic 936, maximum
  8 s; Low Latency 2,460, maximum 3 s. These are synthetic sample checks.
* A retained local 48-second sermon excerpt, a 0.01-volume copy and digital
  silence were compared offline across all modes. All 768,000 samples per arm
  match exactly, with no overlap or introduced clipping. Original excerpt:
  Balanced 11 segments, Dynamic 7, Low Latency 17. No speech-recognition calls,
  word-error measurement or intelligibility improvement is claimed.
* Simulated one-hour service: 450 source rows and 450 translated/queued speech
  rows per language, 150 batches, five injected invalid responses repaired,
  ordered drain and summary/history reopening pass. Peak traced Python allocation
  during this text-pipeline drain was about 1.50 MB, not total process RSS.
* Source startup and rendered 1,000-segment-per-panel Qt stress pass. Selection,
  all text, manual reading pixels, resizing and follow-bottom are preserved;
  the resulting dashboard image was inspected. Translation guard fixtures pass.

Intermediate evidence is retained under ignored `build/continuous-capture/`.
The first slow persistence run was interrupted to batch already-waiting journal
commits; it is not an acceptance result. One full-suite attempt under concurrent
disk soaks hit an existing two-second test gate and leaked its fixture queues.
The fixture now has bounded scheduling tolerance and guaranteed cleanup; the
focused check and final full suite pass. Earlier gain assertions were updated
to verify the new 4x policy and unchanged waveform rather than the old ceiling.

## Files

Application changes: `church_translator/audio.py`, `backlog.py`, `engine.py`,
`speech_audit.py`, and the acoustic-disagreement/confidence-note branch in
`services.py`. Existing uncommitted changes in `sermon_context.py`, `services.py`,
`audio.py` and their tests were preserved and included in validation.

Verification: `scripts/verify_capture_submission.py`, `verify_long_capture.py`,
new `verify_offline_modes.py`, new `tests/test_continuous_journal.py`, plus focused
updates to continuity, STT, speech-completeness, audit and shutdown fixtures.
`AGENTS.md` documents the journal architecture and reusable checks.

## Windows build and acceptance boundary

Windows packaging and the final-layout audit pass: **23 source-matching modules**,
blank public keys, no detected credentials or private paths in the ZIP, and
packaged startup/geometry checks at **100/125/150/200%** scaling. Six changed files
were copied from the verified staging build; 2,068 identical files were retained.
Local settings were preserved, with no process termination or permission changes.
The previous ZIP is backed up under `build/continuous-capture/`.

Archive SHA-256: `2317709d9a745bc3242777a84092159b868d25971ef471a36111833ba4a0b13e`.
Machine-readable results: `verification/continuous-capture-2026-10-25.json`.
The final local layout is
`dist-app/ChurchTranslator/ChurchTranslator.exe` beside `_internal`, with
`dist-app/ChurchTranslator-Windows.zip` beside the folder.

No natural WER improvement, missing-word rate, 8–12 second real-provider latency,
audible English quality, Windows 10 compatibility or full church-service
acceptance is claimed. A process/power failure can lose callback packets not yet
committed; an actual disk failure cannot provide durable storage. Finite memory
and unavailable storage require an explicit input stop, never a silent discard.
Uncalibrated ASR scores and acoustic sparsity remain review heuristics; confident
fluent omissions can evade them. The end-to-end mock test proves ordered delivery
of supplied model responses, not their correspondence to spoken Latvian.

Natural recognition acceptance remains incomplete. The locally verified build
is a review candidate, not a claim that every acceptance criterion is closed.
