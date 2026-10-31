# Speech completeness candidate — 31 October 2026

Final package verification completed 1 November 2026 (Europe/Riga).

This release improves the chance that captured speech reaches the source transcript.
It does **not** establish complete or accurate natural Latvian recognition. No
human-checked transcript was available. A real church audio test remains necessary.
The selected transcription model and provider consent settings are preserved.

## Findings and evidence

- The latest closed Dynamic recording contained 49.8 seconds; Balanced contained
  103.8 seconds. Their original callback journals and logical segment ranges had
  no uncovered samples, pending pipeline work, or recorded hardware warnings.
  This establishes application sample continuity, not upstream device completeness.
- Recognition was a demonstrated bottleneck: Dynamic's first ten seconds returned
  six Latvian words despite approximately 8.72 seconds of probable speech under
  the new, uncalibrated Silero analysis. Another ten-second segment returned seven
  words. Old energy-only sparse-output thresholds failed to warn about both.
- Existing evidence traced these short provider responses through unchanged source
  contribution. No later assembly/UI deletion explained those particular examples.
- An approved controlled WAV request to the currently selected gpt-4o-transcribe
  still returned six words. Changing the container alone did not fix this excerpt.
  A second approved request using server automatic chunking returned 17 words,
  preserving all six original words as its prefix. The gpt-transcribe candidate
  returned 26 words. None is scored as correct without an independent reference.
- Failed reference-free verification previously made the selected source empty.
  The raw hypothesis was retained privately but absent from the visible source.
  This release displays that hypothesis as uncertain and holds its translation/TTS.
- Previous stationary-noise gating was broader than warranted for quiet speech.
  The actual gated recorded floor was approximately -96.7 dBFS, one PCM16 unit.
  Broader room noise and ambiguous quiet speech now remain eligible for STT.

## Changes

`audio.py` retains independent callback, disk journal, segmentation and provider
workers. Each callback records capture identity, unique block ID, original sample
range, monotonic/wall timestamps, rate, channels, duration and device status.
An unexpected actual input rate is rejected. Queue overflow aborts future input
with a reserved packet rather than overwriting older audio. Journal failures keep
the uncommitted batch and queued tail in bounded memory, report the failure and
abort input. Those volatile packets cannot survive a process/power failure.

Storage monitoring runs off the audio callback. Below 2 GiB free it warns; below
256 MiB it stops future input while draining received packets. This reserve is a
best effort, not protection against another process filling the disk suddenly.
Pending and unresolved speech is never automatically deleted.

`speech_gate.py` computes speech probability/occupancy for ordinary as well as
quiet audio. VAD chooses boundaries and warns; it does not veto ambiguous speech.
Only digital zero and stationary, exact one-unit PCM16 floor dither without
speech evidence can skip recognition. Original PCM remains available in the journal.

`services.py` uses documented mono 16 kHz PCM16 WAV. Original float32 capture is
untouched; diagnostics include encoded/decoded hashes, quantization error,
padding and exact submitted bytes. Upload-only gain remains bounded at 4x with
a 0.92 peak target; there is no new denoiser or resampler. Retained-recording replay
found zero processed clipped samples. This does not prove gain improves recognition.

Probable speech with sparse/empty recognition now creates a coverage warning.
`STT_MAX_RECOVERY_REQUESTS=1` permits one evidence-triggered recovery; zero disables
it. Context checking and completeness recovery share that budget. The selected
gpt-4o-transcribe recovery removes references and uses server automatic chunking.
A recovery can select an exact normalized prefix extension with plausible speech
density; a conflicting response cannot replace existing wording/negation. Both
candidates and inputs stay available for review. This selection is a heuristic,
not proof that added words were spoken. Transient transport retries remain
separately bounded by the existing retry policy; exhausted credit is not retried.

`gpt-transcribe` is selectable as a candidate, using `languages=['lv']`, sanitized
keywords and contextual prompts without legacy temperature/logprob parameters.
Existing saved choices remain unchanged. Latvian requests succeeded in the approved
comparison; representative accuracy, service latency and failure recovery remain
unverified. See the official [speech guide](https://developers.openai.com/api/docs/guides/speech-to-text)
and [model documentation](https://developers.openai.com/api/docs/models/gpt-transcribe).

Token confidence and possible missing speech are reported separately. Missing
token scores now read `low=unknown`, with calibration and completeness unverified.
An entire plausible clause is not marked word-uncertain solely for sparse coverage.
Failed verification/language checks keep original text visible as uncertain while
blocking its translation and speech; ordinary uncertain wording keeps the existing
translation behavior. Original responses are retained before cleanup/selection.

## Boundaries, modes and assembly

`STT_OVERLAP_SECONDS=0.5` supports bounded 0–700 ms pre/post context for local Whisper
and whisper-1, whose word timestamps provide audio ownership. Logical cores remain
an exact partition. Words are assigned by audio-relative end time; genuine repeated
words remain. Missing, invalid or partial timestamps retain the full hypothesis
for review and hold translation rather than silently deleting untimed words.

The selected gpt-4o-transcribe and candidate gpt-transcribe return text without
word timestamps in this integration. Their effective overlap remains zero.
Guessing word locations or deduplicating similar text could delete real speech;
no overlap value is claimed to improve their natural word accuracy. Their recovery
instead uses the verified server resegmentation option where supported.

Dynamic retains natural pauses, a six-second soft target and ten-second maximum
normal request. Balanced retains its four-to-six-second behavior; Low Latency
remains available. Timestamped requests include context within their physical
duration ceilings; their logical cores are correspondingly shorter. Pause/Stop
flush every remaining sample. These profiles trade bounded extra boundary delay
for context; no absolute 10–12 second end-to-end guarantee is made under backlog.

Ordered durable queues still serialize source commits and translation groups.
Source persistence/UI updates happen before downstream failures. Held source is
visible but cannot enter translation/playback. Nonoverlapping recognition is never
text-deduplicated. Existing session context clearing and displayed text retention
at Stop remain in place.

## Coverage, translation and private diagnostics

`speech-audit.sqlite` now stores exact submitted WAVs, options, raw provider
responses/errors and lifecycle events: CAPTURED, SEGMENTED, QUEUED, SUBMITTED,
RECOGNIZED, COMMITTED, CONFIRMED_SILENCE, RETRY_PENDING, REVIEW_REQUIRED and
FAILED_RETAINED. Integrity reports distinguish submitted, returned, committed and
review counts, with possible unresolved speech ranges. These are heuristics;
`speech_completeness_verified` stays false and missed-word counts remain unknown.

Gemini remains primary. No translation/TTS paid calls were added to testing.
Existing same-provider bounded quality repair, grouped source IDs, durable pending
translation and consented fallback remain. Severe compression is now rejected for
one bounded repair. Possible English negation changes, sentence compression and
unexpected duplicate sentences generate local review warnings without rewriting
source or inventing meaning. Such checks cannot establish semantic equivalence;
legitimate grammatical changes can warn, and meaning reversals can go undetected.

Settings → **Private audio diagnostics / retention…** exports a selected segment
as a private ZIP with `original.wav`, exact `submitted-*.wav`, raw responses and
source contribution. Listen to both to distinguish upstream routing loss from
recognizer omissions. Existing audits can export originals; exact submissions are
available only for newly recorded sessions. Export never uploads audio. Explicit
deletion is restricted to an inactive, drained recording with no pending/review
queues. Transcripts/history, credentials and installed voice models are separate.

The selected Elgato virtual input is requested as 16 kHz mono. Logs cannot establish
whether its upstream mix includes all pastor channels, mute changes, interruptions
or translated playback feedback. Use the diagnostic WAVs and mixer to verify the
intended source. No routing/device choice was silently changed.

## Verification

Evidence is under ignored `build/completeness-2026-10-31/`; private speech/results
are excluded from Git and the public ZIP.

- Full automated suite: 334 tests passed; all 17 focused tests passed after the
  final timestamp-coverage guard. Cached real-response pipeline replay committed
  the 17-word recovery, kept both raw responses, and retained review audio (zero
  new API calls). Package checks are recorded alongside the build evidence.
- Focused cases cover 300/500/700 ms shared audio across all three modes, exact
  sample partition, crossing words, genuine repetitions, conflicting negation,
  missing timestamps, bounded recovery, held-source visibility, exact diagnostic
  bytes, low-storage drain, failed journal retention and guarded deletion.
- Replay of both original recordings and 1%-volume versions across all three
  profiles preserved every sample. All 22 previously recognized speech candidates
  stayed eligible at 1% level. New coverage checks warned on two Dynamic and one
  Balanced segment. This is neither a calibrated missed-speech duration nor WER.
- Two-hour synthetic segmentation per profile preserved all 115,200,000 samples
  in order. Delayed-provider 30-minute pipeline checks separately exercise actual
  journal/queues, encoding, source commits, grouped translation, muted playback
  and End Session drain. Dynamic committed 203 source segments; Balanced 314;
  Low Latency 344. Each accounted for all 28,800,000 captured samples with zero
  pending capture/translation/synthesis/playback work. Providers are mocked;
  see their JSON results. Backlog peaks were 81/145/165 segments respectively;
  these accelerated overload figures are not real-time latency measurements.
- Source smoke checks navigation, settings/history/context, audio enumeration,
  output opening, new diagnostic dialog, candidate selection and Yoruba install UI.
  UI stress retained 1,000 synthetic rows per panel with responsive queued updates.
- Packaged EXE smoke passed at 100%, 150% and 200% scaling. Offline Yoruba
  synthesized all three fixtures with zero provider requests and preserved tone
  marks at the tokenizer; native pronunciation remains unverified. All 27 compiled
  application modules matched final source. The exact-audio export module also
  matched source. The public archive audit found blank API keys, no credential
  matches (including the distinct installed key), and no forbidden local data.
  Results are kept with the final release evidence.

Natural WER, deletion/insertion/substitution rates, truly missed speech duration,
semantic translation accuracy, audible quality, upstream hardware completeness,
Windows 10, natural two-hour operation and representative provider latency remain
unverified. Synthetic, mocked and cached results do not establish these claims.

## Paid usage and release

Three individually approved ten-second requests this completeness task:
gpt-transcribe ~$0.000750 (2.05 s); selected-model WAV ~$0.000760 (2.27 s);
selected-model automatic chunking ~$0.001008 (2.75 s). Task estimate ~$0.002518;
conservative cumulative session estimate **$0.019022**, below the user's **$0.20**
maximum. No further paid tests are authorized by this report. These are usage
estimates, not an account billing statement. Free-tier Gemini status is preserved.

Final layout: `dist-app/ChurchTranslator/ChurchTranslator.exe` beside `_internal`,
and `dist-app/ChurchTranslator-Windows.zip` beside that folder. Public keys are
blank; installed keys, settings, credentials and glossary must remain preserved.

Checkpoint branch `codex/speech-completeness-baseline-20261031` points to the
previous release commit `53a3f25`. Its public rollback ZIP is retained privately
at `build/completeness-2026-10-31/rollback-ChurchTranslator-Windows.zip`.
