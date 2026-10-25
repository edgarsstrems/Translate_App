# Live sermon stability and Windows release

This focused pass preserves the four-tab UI, Latvian source language, saved voice
preferences, summaries, exports, session history, durable queues and paid-fallback
consent. The private sermon artifacts and 42-case evaluation stay local. No private
sermon text, prepared document or microphone audio was uploaded during this pass.

## Findings and changes

| Area | Finding and resulting behavior |
|---|---|
| Live panels | Existing updates were already incremental; there was no repeated full-document replacement. However, the 400-block cap removed old transcript content. Live panels now retain all text, disable unused undo history and append with a separate document cursor, preserving reader selection, focus and horizontal/manual vertical position. The activity log remains bounded at 600 blocks. |
| Layout flicker | A rendered stress test reproduced changing panel geometry as the wrapping status label changed size. Reserving a fixed two-line status area stopped the tested layout shifts. This was independently caught after the initial unit checks. |
| Dynamic capture | Six seconds was a forced ceiling, even during active words. It is now a soft target: normal pause holds remain 0.64–0.80s; after the soft target a 0.32s breath can release the phrase. Uninterrupted speech is still bounded at eight seconds. Both limits are configurable through DYNAMIC_SOFT_SECONDS/DYNAMIC_HARD_SECONDS. Capture remains an exact nonoverlapping sample partition, without guessing sentence completion. |
| Context lifecycle | The earlier local report identified context applied before Start being cleared; that staging fix already existed in this checkout. Apply-while-idle, one-session activation, Stop cleanup and restart behavior remain tested. No second lifecycle rewrite was needed. |
| Context retrieval | Exact words missed simple Latvian inflections. Conservative suffix matching now recognizes closely inflected terms while retaining two-word/35% current-source evidence. Only vocabulary present in current speech is supplied, with no prepared paragraphs. Diagnostics distinguish loaded/active/supplied, section index, lexical coverage, budget and omission reason even when recent-source context is present. Lexical coverage is not a calibrated semantic confidence. |
| Recognition | Supported existing OpenAI transcription models request token log probabilities. A conservative, explicitly uncalibrated low-confidence flag preserves recognized words and retains audio for review. Unsupported confidence metadata is omitted for other model IDs. Recognition language/model settings, normalization, digital-silence protection and original-word preservation remain intact. |
| Uncertainty | Recognition uncertainty now reaches translation requests, including coalesced batches. Prompts require preserving recoverable meaning and a brief unclear marker for unresolved wording, instead of reconstructing a sermon from a draft. This is a model instruction, not a guarantee of semantic correctness. |
| Translation validation | Explicit changed numeric chapter:verse references are rejected and enter the existing bounded same-provider repair. Spelled-out references remain allowed. Empty/malformed/copied/expanded-response checks and reference-free repair are retained. Arbitrary implausible meanings cannot be detected reliably by these deterministic checks. |
| Playback | Compatible output segments reuse the device stream. Every segment drains before acknowledgement; decode/write/drain failures retain pending work. Diagnostics expose queued audio seconds, backlog warnings, language order, per-language speech-ready timing and PortAudio output-start estimates. No sentences are expired, dropped or sped up automatically. |
| Providers and voice | Existing Gemini timeout, cooldown, background recovery and billing-consent behavior remains intact and regression-tested. No model or saved voice migration occurred. A diagnostic-only Iapetus/0.95 request was accepted by Cloud TTS. |

## Verification

The original clean baseline passed 239 tests. The new focused regressions cover
long documents, tail-only edits, selection at EOF, manual scroll/focus, stable
status geometry, soft/hard boundaries, inflected context and topic mismatch,
confidence metadata/uncertainty propagation, numeric references, stream reuse,
drain failure retention and backlog warning without dropping jobs. Final suite
and packaging results are recorded in `verification/sermon-reliability-2026-10-25.json`.
Failed attempts remain in ignored `build/sermon-reliability/`.

| Check | Observed result |
|---|---|
| Rendered GUI | 100 rapid updates per batch, 1,000 accumulated segments in each of three panels, queued worker-thread signals, input meter and activity updates, and window resizing. Zero document removals; selected manual reading viewport pixel-identical across the final 100 updates; follow-bottom resumes. One run took 0.688s wall/0.672s CPU and recorded 17/16/16 panel paint events. Paint counts depend on scheduling. Screenshots were inspected. The waveform bars are existing static decoration, not a live signal renderer. |
| Two-hour capture per mode | Balanced 1,458 segments, maximum 5s; Low Latency 2,431, maximum 3s; Dynamic 900, maximum 8s. Identical hashes for all nonzero samples in order, with no reported errors. Accelerated synthetic input, not microphone acceptance. |
| Represented 60-minute service | 450 source rows, 150 batches, 155 mocked provider calls, five invalid-response repairs, all 450 rows per language delivered and queued in order. Queues drained; summary/history reopened; history bounded at eight rows. 4.516s drain wall time and 1,478,238 peak traced Python bytes. Virtual pacing and mocked providers/audio; this does not measure process RSS or an actual wall-clock hour. |
| Public/private diagnostics | All 12 public synthetic cases and all 42 private diagnostic cases pass. Private results remain local. These are format/error checks, not ASR or semantic-accuracy scores. |
| Live synthetic capture | Known 6.354s Latvian fixture retains all five checked keywords in every mode. Dynamic uses one segment and matches the complete fixture in this run (STT 0.938s). Balanced/Low Latency retain minor wording errors; source audio is synthetic. |
| Gemini context canary | 0.781s; unrelated prepared material and prior generated English were not imported. Lithuanian output has awkward grammar, so this is scope-isolation evidence only. |
| Live serial chain | A separate 4.572s pre-generated Latvian fixture: STT 2.469s, Gemini translation 0.750s, English synthesis 0.984s. Speech-ready 4.203s after completed audio, or 8.775s including the simulated capture duration. One sample cannot establish a production percentile. |
| Real-device muted output | Two Iapetus/0.95 audio segments, each 5.873s, drained in order without reported playback errors. First output estimate 9.00s including simulated capture duration; second 15.10s because it deliberately queues behind the first. Queue waits 0.03s and 6.11s. Output was muted; physical audibility/quality was not evaluated. |
| Source smoke | Four tabs, isolated settings restart, routing, audio enumeration, actual microphone frames without upload, silent output and mocked Start/Stop pass. |
| Paid fallback | Stayed disabled in live checks. Existing consent, failure, cooldown, recovery, repair and spending-limit paths were verified with mocks; no paid OpenAI translation acceptance is claimed. |

## Before/after examples and boundaries

Synthetic context example: prepared vocabulary `Ķermenis un veselība` now matches
recognized `Ķermeni un veselību sargājam`; exact-word retrieval previously missed
both inflections. Unrelated topic text still produces no prepared hint. This
measures retrieval, not an improvement in a model's recognition or translation.

Synthetic quality example: `Jāņa 4:12` → `John 4:13` previously passed the broad
response gate; it now triggers `changed_numeric_reference`. Correct notation and
spelled-out numbers remain allowed. This does not repair unknown spoken numbers.

UI example: the old panels silently lost their earliest rows after 400 blocks;
all 1,000 source rows now remain visible/selectable. The rendered test first
failed on status-driven geometry changes, then passed after the fixed status area.

The recording referenced by the master prompt was not present in its attachment
folder or the inspected Downloads/Videos locations. Original source audio was not
available for validating the quoted recognition errors, so no invented corrections,
word-error-rate claims or natural-sermon before/after examples are offered.

Prepared-document recognition hints were not enabled: automatic approval review
rejected sending private document terminology to OpenAI STT without explicit
authorization for that disclosure. Existing translation-context behavior remains
available. Model context supplied is never reported as proven model influence.

Longer translated speech can still accumulate playback lag at a fixed selected
speed. Stream reuse removes repeated device construction, while queued-duration
logs expose the remaining rate mismatch. Changing voice speed, overlapping content
or dropping sentences automatically would violate the preserved behavior.

## Release and remaining acceptance

Build after the final application edit using `scripts/package_windows_app.ps1
-SkipDependencies`, then run `scripts/verify_windows_package.ps1` and
`scripts/audit_windows_release.py`. Final evidence records the actual scaling,
module parity and archive-privacy results. The standalone executable is
`dist-app/ChurchTranslator/ChurchTranslator.exe` with its `_internal` folder;
the public distribution is `dist-app/ChurchTranslator-Windows.zip`. Root
`ChurchTranslator.exe` remains the source launcher. Binaries remain Git-ignored.

Natural 45–60 minute ASR/translation quality, original-sermon audio comparison,
reverberant church sound, speaker changes, audible quality, physical dual-output
routing, Windows 10 rendering and a real-time hour of CPU/RSS remain unverified.
The changes do not make arbitrary corrupted recognition reliably recoverable.
Next priorities are original-audio evaluation and supervised service testing,
followed by an explicitly authorized experiment with bounded recognition hints.

The confidence integration follows the [official OpenAI transcription API](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create).

Final release verification: **253 tests pass in 31.231s**. The rebuilt executable
passes fresh smoke checks at **100%, 150% and 200%** scaling; dashboard, voice and
summary screenshots were inspected. All **22 packaged application modules** match
source. The public ZIP passes credential/private-path checks with blank template
keys. No natural-sermon or Windows 10 acceptance is inferred from these checks.
