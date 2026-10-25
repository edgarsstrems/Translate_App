# Speech capture release — 25 October 2026

The stable eight-second Dynamic ceiling, four-tab UI, provider consent,
translation routing, ordered playback, voices, summaries and saved history are
preserved. No default model, overlap or longer-segment experiment was adopted.
This release improves capture accountability and failure handling; it does not
establish improved Latvian recognition accuracy.

## Completed

- Callback audio enters a bounded producer/consumer queue without API, disk,
  segmentation or UI handlers on the hardware callback. Overflow aborts input
  explicitly instead of overwriting older buffers; hardware/ADC warnings are
  reported by the consumer. Hardware-internal loss cannot be reconstructed.
- The worker journals original float32 callback audio locally, before
  segmentation, with capture IDs, half-open sample ranges and hashes. Digital
  silence and sub-threshold onsets remain accounted for. Capture IDs separate
  pause/resume streams. These archives intentionally persist after completion.
- Every nonzero segment reaches recognition even below the VAD threshold.
  Exact-zero segments are recorded and skipped explicitly. The local Whisper
  secondary VAD is disabled so it cannot remove preserved quiet speech.
- Encoded STT requests record actual duration, sample rate, padding, PCM16
  quantization error, hashes and attempt status. Raw/selected recognition,
  context delivery metadata, gain, confidence, recovery, usage and queue delay
  stay in the private local audit. Public logs do not print sermon text.
- Sparse text over energy-active audio can trigger one reference-free check,
  sharing the existing context-check budget. Disagreement/failure retains both
  hypotheses and audio for review. Energy and model agreement are not proof of
  completeness. Text-pattern filters no longer delete recognized clauses.
- Billing exhaustion is non-retryable. Failed requests leave their original
  audio unacknowledged in the durable queue. End Session flushes its final
  partial segment and drains or preserves pending work.
- `scripts/export_speech_diagnostic.py` exports one exact original audio range
  plus metadata as a local private ZIP; `--tail capture_id` exports journaled
  but unsegmented crash audio. Nothing uploads automatically.
- Existing Set OpenAI API Key saves a replacement key for subsequent sessions;
  no rebuild is required. Stop the active session before changing keys.

## Verification

- Baseline: 274 tests passed. Final: **287 tests passed in 48.201 seconds**.
- Coverage includes exact sample partition, onset/tail preservation, ADC gaps,
  buffer overflow, storage failure, corrupt/gapped journals, PCM16 upload
  accounting, bounded recovery, genuine repetition, exhausted-key retention,
  durable reopen, ordering, End Session, translation/playback and Qt stability.
- A 60-second accelerated callback/journal/durable-queue/actual-encoded-request
  check preserved **960,000 samples**, with nine synthetic STT requests and
  exact expected PCM16 request audio. No external provider was used.
- The extended two-hour submission soak was interrupted for the user's wrap-up
  deadline. Its partial journal is retained privately; it is not a passing test.
- Rebuilt EXE: launch/smoke passed at **100%, 150%, 200%** scaling. Smoke also
  exercised brief actual microphone capture and muted output, without providers.
  This does not establish church acoustics, audible quality or natural sermons.
- Release audit passed: source/package parity, no forbidden private paths or
  credential matches, blank keys in the public ZIP. Existing source settings,
  credentials and session data were preserved. The new local app folder was
  initialized from the existing .env after the clean public ZIP was produced.

## Provisional Latvian comparisons and limits

Before the user's no-more-paid-requests instruction, 40 live requests were
attempted across three short excerpts; 29 returned and 11 failed, principally
with exhausted credits. Attempted input duration was 483.5 seconds including
failed attempts, not a billable-usage or cost claim. A prior sandbox attempt had
connection failures and is separately retained. No calls were made after the
user prohibited further paid requests.

Eight-second windows, pause-aware experimental windows, 24-second windows,
gain, previous recognized text, gpt-4o-transcribe, mini-transcribe and whisper-1
were compared provisionally. Experimental timestamp ownership was used only in
an offline overlap arm; production uses no overlap or fuzzy deduplication.
No consistent winner was established. More returned words are not proof of
more correct words. Some omissions may occur within recognition itself, while
the old VAD gate and text filters were independently identified loss risks.
Their contribution to particular historical missing words is unverified.

No human-checked reference exists: **baseline/updated WER, verified deletions,
clause omissions, boundary errors and hallucination rates are unavailable**.
Recognition improvement and live latency improvement are not claimed. Gain
was not shown consistently better or worse, so its existing behavior remains.
The pastor's DOCX remains reference context, never spoken ground truth.
Separate prepared-context A/B could not be completed after credits exhausted.
No billed-cost estimate is asserted without reliable provider accounting.

Private draft hypotheses, timestamps and small original WAV excerpts for human
review are under `build/speech-completeness/audio-only/`, including
`HUMAN_REVIEW_DRAFT.md` and `benchmark.json`. They are excluded from the ZIP.

Remaining limits: fluent clause omissions can escape the sparsity heuristic;
model disagreement is unresolved without listening; the latest natural service,
complete two-hour submission soak, extended physical-device run and Windows 10
remain unverified. Crash durability begins when the consumer commits each raw
callback; hardware buffers and queued/uncommitted tail samples can be lost on
power failure. The original float journal uses approximately 230 MB/hour plus
database overhead; disk/storage failures remain unavoidable and explicit.

OpenAI request support was checked against the installed SDK (3.8.0) and the
[official speech-to-text documentation](https://developers.openai.com/api/docs/guides/speech-to-text).
Log probabilities are not acoustic word alignment or calibrated omission proof.

## Final artifacts and church steps

- EXE: `dist-app/speech-completeness/ChurchTranslator/ChurchTranslator.exe`
- ZIP: `dist-app/speech-completeness/ChurchTranslator-Windows.zip`
- Run the EXE with its entire folder, or extract the complete ZIP to a writable
  directory. In AI Models & Keys, choose Set OpenAI API Key, save the funded
  replacement, then start a new session. The extracted public ZIP needs its own
  Gemini/Google credentials if they are not otherwise configured.
- Confirm the microphone and both outputs, apply the pastor's DOCX as context,
  retain Dynamic mode, and make a short speech test including End Session before
  the service. Check backlog/error messages and local disk space.
- If recognition previously failed with exhausted credits, its recordings are
  preserved for local recovery/review; starting a new session does not silently
  retranscribe old failed sessions or duplicate already spoken translations.

Detailed local evidence is under `build/speech-completeness/`. Changes were
saved without committing or discarding the pre-existing working-tree changes.
