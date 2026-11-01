# Production reliability and checked-sample accuracy — 1 November 2026

This update preserves the selected gpt-4o-transcribe model, Gemini translation,
Google English TTS and the working local Yoruba implementation. It improves
recovery selection and prevents several misleading outputs. It does not establish
complete natural-sermon recognition or production acceptance on every church PC.

## Confirmed findings

- The latest retained Dynamic session contained 160.1 seconds, zero uncovered
  sample ranges and no pending capture/translation/synthesis/playback work after
  Stop. Nine audio review items remained. Capture continuity and word completeness
  are separate: a user-checked portion of one segment confirmed substantial
  recognition omissions despite successful requests.
- All 25 retained submitted WAVs matched their stored encoded/decoded hashes.
  Sample counts, 16 kHz rate, padding and upload-only preprocessing were reproduced
  exactly, with zero clipped samples. No trimming/format corruption explained these
  examples. Whether gain improves or damages perceived speech clarity remains
  unverified; original capture remains untouched.
- The final 100 ms was genuinely 1,600 original samples. Its upload contained
  400 ms of additional zero padding. The previous-text prompt contained the word
  returned by that tiny request. Carryover/hallucination is plausible; neither
  duration nor an independent recognizer proves what was actually spoken there.
- The previous recovery rule accepted only exact prefix extensions. It retained
  a five-word first pass even though an existing response to the identical audio
  preserved those words in order and contained additional recoverable clauses.
- Prompt-echo checks counted common function words as evidence of echo. That
  could hold ordinary speech after a disagreeing check. A Latvian sentence-final
  question tag, “ne?”, also produced a false English negation warning when
  translated appropriately as “right?”.
- The screenshot's adjacent English subject pronouns were delivered without a
  grammar guard. The Latvian itself remains ambiguous without checked audio;
  correcting its subject automatically would be unsafe.
- A rejected translation could exhaust its one repair, be classified as an outage,
  and enter automatic recovery again. The effective quality retry budget therefore
  was not bounded over the life of that source item.
- The compact latency callback ran when synthesis finished and audio was queued.
  That measured readiness, not output start or what listeners had heard.
- The recovery exporter attempted to read the audit database as a work queue and
  treated already encoded playback bytes as PCM arrays. Both paths could prevent
  a useful export of retained work.

## Transcription and boundaries

`services.py` now accepts a bounded recovery extension when the original words
remain a complete ordered anchor chain, with probable-speech support and plausible
word density. Interior gaps are tightly bounded; replacing/reordering original
words or inserting a negation between anchors prevents automatic selection.
Prefix extensions remain supported. Both responses, audio and selection evidence
remain in private review. This is an uncalibrated heuristic, not an acoustic oracle.

`recognition_quality.py` uses original duration rather than padded duration to
identify implausibly dense text over very short audio. Such a hypothesis stays in
review with its audio/raw response and is held from the main source, translation
and TTS. Genuine short words remain eligible. Under 650 ms, prior/prepared text
is omitted and a duplicate recovery is not bought on the same inadequate window.

Prompt-echo comparison now uses content-bearing words. A short probable spoken
phrase surrounded by pauses is assessed using probable speech duration. Loud but
VAD-ambiguous audio still reaches STT, with hints/gain disabled. Quietness alone
never discards audio. Original PCM, bounded callback buffering, independent
journaling, disk reserve checks, ordered queues, Pause and Stop drain remain intact.

Dynamic's normal maximum remains 10 seconds; Balanced and Low Latency retain their
profiles. Existing 300/500/700 ms timestamped-overlap tests preserve all samples
and genuine repetitions. Text-only models retain nonoverlapping normal cores;
there is insufficient checked boundary evidence to enable text-only deduplication
safely. The review pack includes adjacent original audio for investigating tiny
tails. It does not automatically assign a contextual model guess to a 100 ms span.

## Checked accuracy result and scope

The user checked the clear part of one ten-second original recording. Their
uncertain ending is excluded. The 23-word reference preserves their wording;
case/punctuation are ignored, while diacritics remain significant. The comparison
replays selection of **existing real responses to the identical audio**, with no
new recognition request or model migration.

| Checked portion only | Previously selected first pass | New recovery selection |
|---|---:|---:|
| Reference words | 23 | 23 |
| Word error rate | 78.3% | 26.1% |
| Deletions | 18 | 2 |
| Deletion rate | 78.3% | 8.7% |
| Substitutions | 0 | 4 |
| Insertions | 0 | 0 |

This demonstrates better completeness on this one checked passage, with remaining
word errors. It is not a sermon-wide percentage, a new-model benchmark, or evidence
that every additional word is correct. The unchecked candidate ending is neither
credited nor penalized. Other recordings and boundary-word/hallucination rates
lack sufficient checked references. No measured missed-speech-duration claim is made.

`scripts/verify_production_recording.py` verifies original/submitted audio locally,
creates private contextual clips/reference templates and accepts explicitly checked
references for scoring. An already installed Whisper snapshot was used offline
on 4.1 seconds around the tail; its differing hypothesis is diagnostic evidence,
not ground truth. No downloads or external audio uploads occurred.

## Translation and uncertainty

The Gemini instructions require complete, natural spoken translation while
preserving actors, negation, tense, questions, conditions, terminology, repetitions
and unfinished meaning. Rhetorical questions and metaphors should sound natural;
unresolvable subject fragments require only a local unclear span. These are general
instructions, not replacements for the user's examples or repairs to Latvian source.
The final instruction is exactly the selected live-tested 1,781-character version
(the preceding version was 2,057 characters). It asks for idiomatic syntax and
context-appropriate metaphor senses while preserving meaning and genuine repetition.

An English adjacent conflicting-subject check triggers one same-provider repair.
Question tags no longer create the observed false negation warning. More common
negative verb forms are checked, while warnings remain unproven diagnostics:
legitimate changes in grammatical form can warn, and reversals can go undetected.

Exhausted quality repair now differs from service/quota failure. With paid fallback
off, the source item and raw replies are retained in `translation-review.sqlite`,
later segments continue, and unvalidated translation is not spoken. Existing
explicitly authorized fallback rules remain intact; nothing enables paid fallback.
New translation attempts retain raw replies, exact prompt and rejection reason
before parsing. End Session reports translation review separately from drained work.

Blanket “[uncertain]” prefixes are removed from live source text. Low token scores,
possible omissions, language/echo issues, very-short-audio hypotheses and translation
warnings remain separate review metadata. Token-level spans are recorded only when
token text exactly aligns with the selected text. No calibrated correctness
probability is claimed, and low-confidence wording is not silently rewritten.

An initial approved Gemini fixture check on the three user-provided examples made two
requests (initial plus permitted repair) but failed validation. The probe retained
failure metadata rather than raw replies, so the precise rejection reason cannot
be established retrospectively. The new production audit closes that evidence gap.
A separately approved single check of clear examples B/C through the normal
application path passed in 0.828 s but remained literal. The user then approved
at most two instruction experiments on those same text examples. Both returned
valid translations; the first used a more appropriate refining metaphor and a
more natural forgiveness question (0.812 s), while the second gave no further
benefit (0.766 s). The first was adopted exactly; the second was not selected.
All raw replies from these later checks were retained. Some phrasing remained
literal. This is limited qualitative evidence, not a general English fluency
score or whole-sermon semantic validation. No example-specific substitutions
were added. Offline tests verify rejection/repair/retention behavior separately.

## Yoruba, English speech, playback and interface

`yoruba_tts.py`, its dependencies, installed paths, installation/loading behavior
and model are unchanged. The user reports working Yoruba voice output. Shared
playback changes preserve chronological language order and independent volumes.
English Google TTS and saved voice/speed settings, including Iapetus at 0.95 when
configured, are preserved. No paid TTS tests or voice-setting changes were made.

The compact latency now updates from the primary language's first successful
output write plus PortAudio's reported device latency. Failed writes cannot report
successful output start. This remains a software estimate, not physical listening
proof. Recognition, translation, TTS readiness and queue waiting stay separate.
Playback-start events are retained locally. Generated/source-duration ratios and
remaining audio seconds expose output load; shared physical devices serialize
languages, while independent outputs remain simultaneous. No catch-up skipping,
voice acceleration or removal of pending audio was added. Long pauses and routing
affect whether a duration ratio actually produces growing delay.

Transcript text sizes are Small/Medium/Large/Extra Large (13/15/19/24 logical pixels).
Medium is the dashboard default; fullscreen has a separate saved choice, initially
Extra Large. F11/Escape toggles the existing three-column dashboard without changing
source text. Four tabs, selectors, waveform indicators, controls, dark styling,
copy/edit/export/history/summary/context, activity toggle, scrolling and the
existing QR functionality remain. Stop retains displayed text.

## Performance and verification

The retained real session's median stage times were STT 1.922 s, translation
0.664 s and English synthesis 0.711 s (maximums 2.422/4.000/1.500 s). They must not
be added as an end-to-end measurement; ASR includes recovery where used. All 16 completed translation batches used
Gemini; no OpenAI translation was recorded. The screenshot's former 13.1 s average
was a readiness measurement. Natural post-update output-start latency and long
church playback delay remain unverified.

- Full suite: 351 tests passed; focused production tests cover short words/tails,
  source anchors and negation edits, question tags, uncertainty spans, one repair,
  durable held translation, later delivery, failed/successful playback writes,
  readiness versus output timing, recovery export, fonts/fullscreen and restart.
- Retained Dynamic/Balanced recordings and 1%-volume variants replayed across all
  profiles without lost samples. Original/submitted input hashes and preprocessing
  were checked separately on all 25 retained submissions.
- Two-hour synthetic segmentation per mode preserved every sample. Thirty-minute
  delayed-provider pipeline checks exercised journal/queue/encoding/ordered source,
  translation, muted output and shutdown. Fixed-profile checks disable automatic
  profile return in the verifier, never in user settings.
- A simulated two-hour text service preserved 900 source rows per language in 300
  translation batches, repaired nine injected invalid replies, drained its queues,
  and preserved summary/history. Peak traced Python allocation during drain was
  2,868,963 bytes. This excludes total native RSS and is not a two-hour wall-clock
  hardware or real-provider soak.
- UI stress retained 1,000 rows per panel, selection, follow-bottom and pixel-stable
  manual reading while resizing. Source smoke passed, including fullscreen/fonts.
- The packaged EXE passed at 100%, 150% and 200% scaling, including fullscreen
  fonts, navigation and diagnostics. All 28 compiled application modules matched
  final source. Packaged offline Yoruba synthesized three fixtures with zero
  provider requests and preserved tone marks at the tokenizer. Native pronunciation
  was not independently graded. ZIP privacy and installed-file parity are checked
  before promotion; final machine-readable results accompany the release evidence.

Evidence and checked speech/reference text remain under ignored
`build/production-2026-11-01/`. Public reports contain numerical results, not private
recordings or the checked transcript.

## Costs, release and remaining limits

This update made **zero paid OpenAI requests**. The five explicitly approved Gemini
text-fixture requests used the user's free-tier-only project; expected paid cost
is zero, not an independently verified billing statement. No private audio was
uploaded. Conservative cumulative earlier OpenAI testing remains approximately
**$0.019022 of the $0.20 maximum**. No further live tests are authorized by this report.

Final paths: `dist-app/ChurchTranslator/ChurchTranslator.exe` beside `_internal`,
and `dist-app/ChurchTranslator-Windows.zip`. Keys remain editable without rebuilding.
Installed credentials/settings/glossary are protected; public keys must be blank.
The previous working release is checkpointed at `14ce92a` on
`codex/production-baseline-20261101`, with its ZIP preserved under
`build/production-2026-11-01/rollback-ChurchTranslator-Windows.zip`.
The intermediate verified production build (`1fb959e`) is also preserved under
`build/production-2026-11-01/release-staging/`; the final instruction rebuild is
staged separately, so neither working predecessor is deleted.

Still unverified: representative whole-sermon WER/deletions, accurate final-boundary
words, calibrated hallucination/uncertainty rates, semantic English/Yoruba accuracy,
physical audio quality/latency, upstream virtual-audio completeness, long natural
service backlog/memory, and Windows 10 on actual hardware. Current-host Windows
checks do not establish all-driver or Windows 10 acceptance. A real church test
remains necessary. Review holds can leave a source passage awaiting manual work;
that is reported and retained, never silently treated as delivered speech.
