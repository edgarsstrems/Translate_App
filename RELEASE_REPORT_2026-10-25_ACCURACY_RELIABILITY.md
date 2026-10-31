# Accuracy, context and reliability review — 25 October 2026

## Findings and changes

The starting revision was `bb6c0b8`, with a clean working tree. Recent history
(`ab65d2c`, `cf7fd3c`, `923db4c`, `dc92146`, `1c3eb71`) was inspected before
editing. The current compact dashboard is preserved. Existing private analysis,
cached same-audio baseline/candidate results and retained audio were inspected.
The new request supplies log/transcript excerpts, not a complete identifiable
latest-service recording and log bundle. Older recordings cannot verify the
exact words in these new examples.

* The reference-free check previously treated any 1–12-word result at no more
  than 1.5 words/second as a possible prompt echo. It did not require reference
  overlap. It now requires at least two matching word stems comprising at least
  half the result. Normal short clauses no longer automatically buy a second
  request merely because previous-source/prepared hints were supplied.
* A second energy check classified seven words over eight active seconds as
  suspicious. Energy includes music/noise and is not measured speech duration.
  Its recovery threshold now requires at most 0.5 words per active second,
  retaining the existing occupancy/duration guards. This is a conservative
  heuristic adjustment, not a calibrated omission detector. It trades sensitivity
  to modest omissions for fewer unsupported recovery requests; fluent omissions
  can still escape detection.
* Reference-free disagreement previously selected the second transcript without
  evidence of greater accuracy. Disagreeing candidates now remain in the durable
  review evidence with original audio; neither candidate is spoken automatically.
  This can leave an unresolved segment awaiting review. Agreement remains
  unverified, and failed checks retain the original candidate for review.
* Uncertainty still includes script/language/prompt-pattern concerns, severely
  sparse output, failed checks and low uncalibrated token scores. Ordinary
  eight-second boundaries and incomplete grammar do not themselves trigger it.
  Low scores are still heuristic evidence, never error probabilities. No grammar
  checker or finite Latvian dictionary is used to certify audio accuracy.
* Prepared hints were gated solely by a section match after bootstrap. Added a
  bounded general-topic fallback when current recognized source supplies two
  matching words, a recognized concept and sufficient document coverage. It
  supplies only relevant document vocabulary. Added transparent compassion,
  forgiveness, humility/patience and marriage concept groups. Unrelated source
  cannot inherit a sticky prior section; no prepared sentences are reconstructed.
  `influence=unknown/unproven` remains unchanged.
* Translation already isolates current source from references, preserves numbers,
  repetitions and incomplete thoughts, and permits one bounded quality repair.
  The existing instruction to mark unintelligible portions now also applies when
  the source uncertainty flag is false. No automatic biblical quotation or
  correction of the user's examples was added. Mocked delivery cannot establish
  that a real model will interpret the damaged phrases correctly.
* Backlog reporting previously counted the complete current clip until it ended
  and asserted a delivery-cadence cause. It now subtracts estimated wall-clock
  progress and explicitly leaves the cause unverified. This is an estimate,
  not a physical acoustic measurement. No content is dropped or compressed;
  synthesis and playback remain ordered. Output device drain behavior is unchanged.

## Audio, completeness and performance boundary

Offline measurements used three eight-second excerpts from the retained earlier
recording (store, ulcer, music). Preprocessing added no clipped samples in these
excerpts. Speech gains were 1.0; music gain was 3.779 with output peak 0.437.
This does not prove intelligibility improvement from gain, or establish that
gain caused the new service's errors. Originals remain unchanged.

Exact nonoverlapping capture, the eight-second Dynamic ceiling, final Stop tail,
raw callback journal, ordered durable queues and recovery are preserved. Previous
same-audio experiments had not demonstrated useful overlap recovery, so none is
introduced. No missing word is invented or removed by text deduplication.

Recognition, translation, speech-ready and playback-start timings remain separate
in existing logs. No new live latency measurement was made: real-provider latency,
listener-facing delay and natural-service backlog improvement are unverified.
The new remaining-duration test distinguishes 12 seconds of generated audio from
8 seconds estimated remaining after four seconds of the current clip. The
existing 18-second backlog test preserves all three six-second jobs.

English default remains `en-US-Chirp3-HD-Iapetus`, rate 0.95, with saved choices
preserved. No automatic acceleration or voice substitution is enabled. Sustained
translated speech longer than source cadence can still accumulate delay.

## Cost and limitations

Paid requests in this task: **0**, incremental API cost **0**. No microphone capture
or private upload was performed. Recovery branches share at most one additional
recognition request per segment; credit-exhaustion retry protection is preserved.
The three supplied understandable clauses are tested with synthetic eight-second
audio and mocked recognition: one request, unchanged text, no uncertainty flag
without other evidence. This verifies control flow, not the audio/text match.

No WER improvement, natural sermon acceptance, accurate correction of the quoted
phrases, calibrated confidence, physical audible quality or Windows 10 acceptance
is claimed. Broad-topic matching is still a local lexical heuristic. Confident
corrupted Latvian can evade review, and the translation prompt cannot guarantee
faithfulness. Prepared text and second-model guesses are never ground truth.

## Verification and artifacts

* Full suite: **296 tests pass**, 73.154 seconds. Source startup smoke passed.
* Rendered Qt stress: 1,000 segments per panel; selection, all text, manual
  viewport pixels, follow-bottom and resizing passed. Source image inspected.
* Two-hour synthetic segmentation per profile: Balanced 1,462 segments/max 5s,
  Low Latency 2,460/max 3s, Dynamic 936/max 8s. All nonzero sample hashes match
  in order, including the final Stop tail. These do not test word recognition.
* Callback/journal/durable queue/actual encoded request test: 120 simulated
  seconds, 16 mocked requests, all 1,920,000 float32 samples and encoded PCM16
  samples accounted for exactly, including zeros; no errors. The initially
  launched 7,200-second persistence-heavy version was intentionally interrupted
  to keep this check bounded. It has no completed acceptance result. The three
  separate two-hour segmentation checks above completed.
* Simulated one-hour service: 450 source rows, 150 batches, 450 delivered and
  queued speech rows per target language; five injected invalid responses
  repaired; both queues drained. Summary/history reopened. Provider calls in
  this simulation were mocks. Simulated median batch processing was 0.02s;
  this is not a real-provider latency measurement.
* Offline curated translation diagnostics pass. These evaluate response guards,
  not the semantic fidelity of generated translations.
* Final Windows build: source-parity audit passes for all 23 modules, blank
  template keys, no detected credentials/private paths in public ZIP. Packaged
  smoke passes at 100%, 125%, 150% and 200% Qt scaling. Installed generated files
  match staging (2 copied, 2,007 identical files preserved); standard-layout ZIP
  audit passes. No user process was terminated or file permissions changed.

Final artifacts:

* `dist-app/ChurchTranslator/ChurchTranslator.exe`
* `dist-app/ChurchTranslator-Windows.zip`
* `RELEASE_REPORT_2026-10-25_ACCURACY_RELIABILITY.md`
* ZIP SHA-256: `f21b95c1ebc21a6f03a1e266d071866047e9bcabb1479bfff8d24b7c60882915`

The previous public release is preserved at
`build/accuracy-reliability/previous-working-release.zip`.
Raw diagnostics and intermediate attempts stay under ignored
`build/accuracy-reliability/`. Initial regressions exposed an overbroad energy
trigger and a supposed prompt-echo fixture that did not actually echo its prompt;
the trigger was narrowed and that fixture now contains actual vocabulary overlap.
The initial build/source audit also caught application edits made during the
build; that intermediate package was not installed as the final release.
