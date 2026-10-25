# Per-sermon context, completed summaries and equal panels

Implemented and tested the context/summary/layout changes. **Natural sermon ASR
accuracy remains an open acceptance item.** Real recording comparisons reproduced
missing clauses at segment boundaries and an unresolved corrupted word; prepared
vocabulary alone did not reliably repair them. No perfect-recognition claim is made.

The supplied sermon is an evaluation example. It is not installed as default
context. Every applied document replaces the index, belongs to its session, and is
cleared from live processing at End Session. Recordings, extracted documents,
transcripts and detailed provider results remain in ignored local folders.

| Requested area | Result and evidence boundary |
|---|---|
| Why context was absent from STT | The engine explicitly passed `previous_context=None`, and the OpenAI adapter never included `prompt`. This was a previous disclosure restriction, subsequently superseded by the owner's explicit authorization. |
| Supported recognition context | Configured `gpt-4o-transcribe` accepts the actual `audio.transcriptions.prompt` request. It now receives bounded prepared vocabulary and the immediately preceding recognized Latvian. Supported models are explicitly enumerated; unsupported models omit hints and report why. No transcription-model migration occurred. Whisper receives a conservative 224-byte budget; local Whisper uses `initial_prompt`. |
| Complete-document preparation | DOCX body paragraphs and tables are read in order. The complete text is indexed locally before live use; title, section headings, references, proper-name candidates and topic vocabulary are extracted. Retrieval combines Latvian inflections, document term weights, a small lexical concept map and recent-source evidence. This is lightweight heuristic retrieval, not unrestricted semantic understanding. |
| Real use versus proven benefit | Accepted provider requests contained prepared hints. Logs distinguish parsed/active/supplied, mechanism, section, omission, processing time and unknown influence. Loaded context is never reported as proof of recognition improvement. |
| Translation | Gemini receives current recognized Latvian, matching prepared topic/vocabulary, bounded recent Latvian and uncertainty. Current source remains the only spoken authority; reference-free quality repair and paid fallback consent remain intact. No prepared paragraph is inserted as missing speech. |
| Capture, VAD and gain | All three synthetic two-hour capture profiles preserve nonzero samples in order. A real local defect was found: below-threshold non-continuation audio could be acknowledged without a review copy. Nonzero rejected audio now remains in the durable review queue. Quiet amplification remains linear with peak headroom; waveform correlation exceeds 0.99999 in regression tests. Recognition performance under natural quiet/church audio is not thereby proven. |
| Missing clauses and eight seconds | Same-audio tests found substantially more complete recognition from an uncut 18-second diagnostic upload than segmented requests. This implicates acoustic context/segmentation in that passage, without proving all historical omissions have this cause. Capture stays at eight seconds. A bounded audio-lookback trial failed its real comparison by returning no new text, so it was removed. Eight seconds is not certified optimal or omission-free. |
| Context hallucinations | A music excerpt initially produced sermon-like words with context, while the no-context arm produced music markers. Sparse prompted recognition now gets one reference-free check. The final tested music excerpt returns no spoken text in both arms. Disagreement is uncertain and retains audio. This limited guard does not detect every possible hallucination. |
| Confidence | Token log probabilities are explicitly uncalibrated. Diagnostics state metadata availability and `omission_detection=false`; a low flag of false is not proof of completeness. |
| Post-session summary | Stop drains or retains pending work, then saves a completed snapshot with segment, uncertainty and queue/review counts. AI Summary selects the active or most recently completed session, including after restart. Late queued final text can still reach the completed session's visible panels. Live context clearing preserves saved speech. |
| Regeneration and History | Real Qt dialogs/workers and configured Gemini generated three Latvian summaries: after End Session, regeneration, and History. All saved to the same session. Capture drain was simulated with a fixed transcript; this is provider/GUI evidence, not a natural full-sermon run. Existing editing, copy/save/export and billing consent remain. |
| Equal panels | Equal grid stretch, shrinkable contents, common header/footer reservations and consistent scrollbars equalize outer cards, text areas and viewports to within one Qt pixel. The language dropdown remains searchable; its font/content height and header alignment are tested. |
| Smooth rendering | A rendered 1,000-segment-per-panel stress retained every row, preserved selection/focus and manual reading pixels, resumed follow-bottom, and resized during queued worker signals. No document clearing/removals. |
| Voice and backlog | Iapetus/0.95 remains selected in diagnostic requests; saved settings are not changed. Two muted output segments drained in order. Queue duration remains visible, and no sentences are dropped or automatically sped up to hide backlog. |

## Real context-off/on observations

The owner's linked public recording was retrieved locally. Initial comparisons
used three 12-second excerpts (two spoken, one music), plus an unrelated synthetic
announcement. Further bounded probes covered another spoken section and the
PDF-aligned store/ulcer examples. The historical bilingual PDF is diagnostic
evidence, not an authoritative transcript or English target.

| Check | Observation |
|---|---|
| Unrelated synthetic announcement | Both arms match its 13-word intended source: WER 0, substitutions 0, deletions 0, insertions 0. Context did not import sermon content in this sample. |
| Natural spoken passages | Hypotheses changed and some clauses appeared with context, but errors remained. There is no established overall improvement in Latvian recognition or natural English translation. |
| Store example | The corrupted word persists in segmented runs; Gemini can still interpret damaged wording incorrectly. Uncut recognition was more complete and spelled a different, plausible word. No document-based correction was installed. |
| Ulcer example | Both arms recognized a first-person speaker account in the tested excerpt. The draft was not used to replace the speaker's statement. No manual audio reference was available to certify every word. |
| Music negative case | First context runs failed; the first narrow guard also failed. The widened sparse-output check passed the final same-audio rerun. Failed attempts are preserved locally. |
| Natural WER and sentence coverage | Not scored: no independently checked verbatim Latvian reference. Neither the prepared DOCX, historical ASR nor a longer model response is accepted as spoken ground truth. |
| Measured STT usage | A/B and diagnostic logs record 68 STT requests including rejected experiments: 11,413 input tokens, including 4,625 audio-input tokens, and 1,515 output tokens. The separate fixed latency probe adds one STT request. The provider invoice was not available; exact billed cost is unverified. Paid OpenAI translation stayed disabled. |

## Verification and release

- Final regression suite: **268 tests pass**. New tests cover parsing/table order,
  replacement/context lifecycle, inflections/topics, actual prompt delivery,
  unsupported models, prompt budgets, sparse hallucinations, gain/review retention,
  completed snapshots/restart, real Qt regeneration/History, and equal geometry.
- Two-hour synthetic capture: Balanced 1,458 segments, maximum 5s; Low Latency
  2,431, maximum 3s; Dynamic 900, maximum 8s. Identical nonzero-sample hashes.
- Accelerated represented hour: 450 source rows, 150 batches, 155 mocked provider
  calls, five injected invalid-response repairs, both language queues drained.
- Public/private deterministic response-regression suites pass. These are not
  ASR or semantic-translation accuracy scores.
- Live Gemini summaries: after-end/regeneration/History produced 63/58/67-word
  Latvian summaries using the configured Flash-Lite model; session identity passed.
- Fixed synthetic live chain: 4.572s source audio; STT 2.890s, Gemini 0.656s,
  English TTS 1.172s. Speech-ready 9.290s including simulated capture duration.
  Estimated output starts were 9.55s and 17.57s; the second waited 8.05s in queue.
  Muted real-device output does not establish physical audible quality.
- Source/packaged smoke, 100/150/200% scaling, final module parity and public ZIP
  privacy results are recorded in `verification/context-integration-2026-10-25.json`.

Package: `dist-app/ChurchTranslator/ChurchTranslator.exe` with its `_internal`
folder. Public distribution: `dist-app/ChurchTranslator-Windows.zip`. The root
EXE is the source launcher. Generated binaries remain Git-ignored; source,
regressions and this privacy-safe report are committed and pushed.

Remaining acceptance: independently checked natural audio reference, reliable
boundary recovery, a complete natural 45–60 minute service, church acoustics,
audible routing/voice quality and Windows 10. The context system is functional;
the requested guarantee of complete accurate recognition has not been achieved.

The supported prompt and confidence parameters were checked against the
[official OpenAI transcription documentation](https://developers.openai.com/api/docs/guides/speech-to-text).
