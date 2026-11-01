# Silence and delivery reliability - 1 November 2026

## Evidence and root cause

The supplied seven diagnostic exports cover segments 185-191, totaling 16.10
seconds at 16 kHz. All seven original float32 hashes and all eight submitted WAV
encoded/decoded hashes match. The sample ranges are contiguous within this
window, with no removed context, padding or submitted sample-count discrepancy.
This establishes integrity of these retained files, not whole-service hardware
capture completeness or word accuracy. A separate 6.06-second export was also
used for local segmentation comparison.

| Segment | Seconds | RMS dBFS | VAD maximum | Probable speech seconds | Previous context | Old gain | Existing first response |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| 185 | 2.50 | -87.9 | .2449 | 0 | yes | 4x | previous prayer |
| 186 | 2.50 | -70.3 | .1204 | 0 | no | 1x | empty |
| 187 | 1.82 | -78.0 | .1549 | 0 | no | 1x | empty |
| 188 | 2.50 | -87.3 | .1632 | 0 | no | 1x | empty |
| 189 | 2.50 | -90.1 | .2953 | 0 | yes | 4x | previous prayer |
| 190 | 2.50 | -89.2 | .2750 | 0 | yes | 4x | previous prayer |
| 191 | 1.78 | -78.5 | .2776 | 0 | yes | 4x | previous prayer |

The four nonempty responses exactly reproduce their previous-speech prompt.
Their audit trails show translation, synthesis and estimated playback start.
Segment 186's second reference-free response was also empty. The bilingual PDF
shows the same repeated output; it is generated ASR, not a checked reference.

The owning source reproduces the failure deterministically: a VAD maximum of
0.20 re-enabled context and upload gain, while probable speech frames required
0.35. Thus normalized near-floor noise could become a speech candidate with no
probable speech ranges. The sparse-echo check excluded audio below three seconds
and text denser than 1.5 words/second. All four problematic windows escaped it.
High provider token scores did not establish acoustic support. Their
`speech_candidate` state also bypassed the engine's ambiguous-repetition warning.

Prompt contamination is strongly supported as a contributing mechanism. It is
not established as the sole cause: gain and acoustic classification also differ
between these examples, and no same-audio live A/B or checked reference exists.
Empty unprompted replies do not prove silence or explain every missing word.
Prepared context was inactive in these requests; the files do not establish why
the user left it inactive. It has not been enabled or disabled globally.

## Conservative recognition changes

References and upload gain now require the same probable speech frames. Scores
between .20 and .35 keep the original audio eligible for unprompted, unamplified
STT. Missing VAD also cannot authorize hints/gain. Digital zero and the existing
strict PCM16 stationary-floor rule remain the only silence gates. Quietness
alone never discards a segment. Supported quiet speech retains upload gain up to
4x, and genuine repetition is not deduplicated.

A second admission check combines an exact multiword previous-phrase echo with
available VAD, zero probable speech duration/ranges, RMS below -60 dBFS and peak
below -40 dBFS. Such a hypothesis stays out of live source, translation, summaries
and TTS, with original audio/raw text in durable review. This is an uncalibrated
review hold, not proof that nothing was spoken. It does not reject text equality
over probable speech. No duplicate paid decode is bought for this unsupported
echo or an already-unprompted empty result with no probable speech. Existing
bounded recovery remains for other eligible cases.

The saved STT model, voices, speed, consent settings, prepared-context support,
timestamped boundaries and source sample partitioning are unchanged. The supplied
requests use gpt-4o-mini-transcribe. No migration was applied to user settings.

`scripts/verify_silence_exports.py` replays original audio and existing raw
responses with a mocked endpoint. All four echoes are held; all seven inputs
remain available for recognition/review without references or gain. This proves
admission behavior, not how a fresh provider request would transcribe the audio.
Natural quiet speech, whispering and intentional repeated prayers still need
representative checked listening/recognition acceptance; synthetic probable-speech
cases verify that those paths remain eligible.

## Complete delivery, independent synthesis and recovery

The previous synthesis loop called English and the optional voice sequentially
on one worker. A 45-second optional initialization could occupy that worker and
delay subsequent English even on separate outputs. Enabled languages now use
independent durable `synthesis-<language>.sqlite` queues, voice instances and
workers. Optional models remain lazy; disabled Yoruba is not initialized.
English can synthesize the next sentence while an optional voice initializes.

Independent devices admit audio independently. A shared physical device admits
ready audio in original batch/language order and uses one existing playback
worker. Waiting for the preceding language on that device is reported separately
from audio readiness. No sentence is interrupted, overlapped, accelerated,
summarized or dropped to catch up. Yoruba synthesis failures remain in its
deferred queue; an English synthesis failure retains its own pending queue while
the other worker can continue. Such pending work is never reported as heard.

Shared-device order can still delay English playback behind optional speech or
initialization, although it no longer occupies English's synthesis worker.
There is no way to guarantee both chronological shared-device delivery and an
independent 12-second playback deadline under that condition.

Translation remains an ordered, bounded joint request for the enabled languages,
with its existing queue coalescing, 12-second Gemini transport deadline,
cooldown/recovery, verified alternatives and explicitly authorized fallback.
Healthy retained Gemini responses in this diagnostic set were not evidence for
an additional speculative same-provider request pool. No new parallel network
requests or paid fallback authorization were introduced. Consequently translation
timeouts and joint quality repair can still delay both languages; synthesis is
independent, while translation is not fully isolated by language. This is a
remaining limitation, not a claim that all head-of-line blocking is eliminated.

End Session still stops capture, flushes, joins recognition and translation,
drains all synthesis workers and then drains output. Completion totals now include
every language synthesis queue. Review and deferred work are reported separately.
Original capture, provider input/response, hypotheses, source and translated work
remain durable across failures. Playback now records PLAYING and
PLAYBACK_COMPLETED after software output drain. This is not physical listening
proof. A crash can leave the current item partially heard; recovery export warns
about that uncertainty and never automatically replays it. Audio still in the
hardware/callback tail at power loss cannot be promised durable.

`scripts/export_backlog.py` includes all work databases, including the new language
queues. Original diagnostic export and review remain available locally. Review
holds may defer genuine speech for manual work; they do not silently mark it
delivered. Whole-sermon completeness has not been proven.

## Latency and capture profiles

New per-stage/per-language logs distinguish transcript, translated text,
audio readiness and output-start estimate. They report current, rolling average,
rolling P95 (up to 2,048 observations), session average/worst/count and retain
existing queue/request timing, output duration/load and backlog logs. Timings use
monotonic elapsed clocks. Audio readiness is recorded before shared-device order
waiting. Compact latency remains the primary language's first successful output
write plus reported PortAudio latency, explicitly an estimate.

The additional supplied segment records STT processing 1.359 s, translation
0.843 s, synthesis 1.141 s and estimated English output start 9.860 s from capture.
That is one historical observation, not a new-release mean or P95. The four
unsupported prayer outputs started in 5.400-8.853 s; those numbers cannot establish
valid-speech latency. No new real-provider latency measurement was made.

The brief's slower segments 181/193-195 were not included in the supplied seven
exports, so their detailed queue/provider causes cannot be reconstructed. A
forced 10-second capture necessarily consumes 10 seconds before recognition,
translation, synthesis and output wait. Healthy Gemini must not be blamed for
that capture time. Dynamic retains its six-second soft/ten-second hard targets;
Balanced remains 4-6 seconds and Low Latency 2.5-4 seconds.

The same additional 6.06-second recording partitions as follows, preserving every
original sample and zero overlap: Dynamic one 6.06-second tail, Balanced two
segments with a 5.76-second maximum, Low Latency three with a 3.17-second maximum.
The 16.10-second near-floor window and 1%-level variants were also replayed across
all profiles. The additional recording and its 1%-level version retain eligible
4x upload gain; the near-floor window receives 1x across every profile. This
compares capture/normalization, not WER. There is insufficient
checked long-speech boundary evidence to replace Dynamic with forced 4-6-second
chunks. The configurable shorter profiles remain available. No 10-12-second
service-wide guarantee is made.

## Diagnostics and retention

The log follows new entries, including hidden/reopened operation. Scrolling up
pauses following; returning to the bottom resumes it. The existing rolling
600-entry activity limit, transcript retention and four-tab layout remain.

Private audio export accepts `185`, `185, 189, 190`, `185-191` and
`181, 185-191, 193`, with whitespace and automatic deduplication. Selected session,
recording date in UTC, count and available IDs/ranges appear in the dialog.
Export runs off the GUI thread, streams one segment at a time into chronological
`segment-<ID>/` folders and includes original/submitted WAVs, metadata, raw
responses and a manifest of requested/exported/missing/failed IDs. It never
substitutes another session. The parser permits up to 100,000 selected IDs to
bound hostile range expansion. Missing selections and failures are explicit.

Background startup cleanup uses 336 hours from the original UTC capture timestamp,
not folder modification time. It requires completion metadata, empty durable
queues, no reviews/deferred work and complete capture evidence. Missing/invalid
timestamps and incomplete records are protected. Capture and exports hold shared
SQLite-based cross-process leases; cleanup/manual deletion require an exclusive
lease. OS locks release after a crash. Linked/reparse targets are rejected.

Automatic expiry clears only original/submitted audio blobs in one transaction,
then lets SQLite checkpoint/vacuum reclaim space. It preserves databases, ranges,
hashes, raw text/metadata, transcript/history, summaries and user exports. No WAL
or shm file is manually deleted. Interrupted precommit cleanup rolls back all
blob changes. A failed postcommit compaction may reclaim less filesystem space;
reported reclaimed bytes are an estimate of removed audio payload. Startup logs
include scanned/eligible/protected/deleted counts, estimated bytes and errors.
Explicit guarded manual deletion remains available.

## Verification and release

Verification evidence is local under ignored `build/nov1-audio-investigation/`.
No private audio, response text, transcripts, databases, keys or installed models
are included in Git or the public package.

- Focused tests cover acoustic thresholds, supported quiet repetition, unsupported
  echoes, bounded retry use, parser/errors, manifest/session mapping, exact
  retention boundary, review/pending/invalid-time protection, cross-process
  export leases, rollback, log following and independent voice failures/order.
- Two-hour synthetic capture per profile preserves all 115,200,000 samples:
  Dynamic 810 segments/max 10 s, Balanced 1,257/max 6 s, Low Latency 1,828/max 4 s.
- Two-hour simulated service preserves 900 source rows per language in 300 joint
  batches, repairs nine injected invalid replies, drains independent synthesis
  workers and reopens summary/history. Peak traced Python allocation is about
  3.40 MB; this is not total RSS or two wall-clock hours of real hardware.
- UI stress retains 1,000 synthetic rows per panel, selection, pixel-stable
  manual reading, follow-bottom and resizing. Source smoke passes.
- Final native full suite: **365 tests passed** (91.720 seconds with concurrent
  build activity). No tests were removed or weakened.
- Thirty-minute delayed Dynamic journal/queue/encoding/output simulation preserves
  all 28,800,000 samples, completes 203 requests/batches/output jobs, reports zero
  uncovered ranges or pending work and reaches a maximum capture backlog of 72.
- Equivalent Balanced and Low Latency checks also preserve all 28,800,000 samples
  and drain: Balanced 314 requests/314 output batches, Low Latency 457 requests/454
  output batches (coalescing preserves every source row). Maximum artificial
  capture backlogs were 145 and 206; these accelerated floods are not real-time
  latency measurements.
- Packaged EXE smoke passes at 100%, 150% and 200%; all 31 compiled application
  modules match final source. One first 150% run failed the fullscreen-state
  assertion after one Qt event pass. Four subsequent same-binary 150% runs from
  the app directory and the source 150% run pass. Its cause is unproven; the
  original failure is retained rather than reported as uniformly passing.
- The real Qt diagnostics chooser/background export test keeps 44 GUI heartbeat
  ticks running while producing a CRC-valid archive, exports the three available
  fixture segments and accurately reports the requested missing ID. The input
  help appears beneath the field. No provider is used.
- Final ZIP privacy/parity and promotion results are recorded in the accompanying
  local release-result JSON after audit.

The initial full suite under the offscreen Qt plugin exposed three geometry
failures; those pass on the native Windows Qt platform at 100% scaling. Two test
fixtures also needed explicit setup for the new per-language workers. The final
native suite is the acceptance run; no failing test was removed or weakened.

Release layout: `dist-app/ChurchTranslator/ChurchTranslator.exe` beside
`_internal`, and `dist-app/ChurchTranslator-Windows.zip`. The build is verified in
the isolated `build/nov1-reliability-release/` staging folder. Final ZIP creation,
privacy audit and promotion follow the EXE/scaling checks. The prior public ZIP is preserved locally as
`build/nov1-audio-investigation/rollback-ChurchTranslator-Windows.zip`.

Git branch: `main`; starting revision: `2d748ef`. The release commit and verified
push result are reported with delivery and in the local release-result JSON.
Rollback source to the starting revision using a separate checkout/revert; do not
reset existing work. To roll back the executable, close it normally and restore
the saved public ZIP's runtime/resources while keeping installed `.env`,
credentials, `.appdata` and glossary. Never restore with a command that deletes
user settings or pending work.

OpenAI/Gemini inference requests in this work: **zero**. No private recordings or
transcript text were uploaded. One initial integration test inadvertently reached
an unmocked optional Cloud TTS path with fixed synthetic fixture text. Synthesis
failed and produced no successful audio; its transport/billing outcome was not
captured, so zero paid TTS usage/cost cannot be certified. The test now explicitly
supplies a mocked voice for each worker; the final suite also disables installed
Google credentials. Subsequent checks use mocks or local processing only.

Remaining unverified: new real-provider outputs/latency, natural quiet/whispered
speech and whole-sermon WER, listening quality, whole-service physical capture,
long natural backlog/CPU contention and Windows 10 hardware. The local source
contains fullscreen projection but no identifiable QR-code implementation or QR
dependency; QR projection acceptance cannot be asserted. No unrelated QR feature
was invented. Native Windows-host/scaling checks do not establish Windows 10 or
all-driver compatibility. A real service acceptance run remains necessary.
