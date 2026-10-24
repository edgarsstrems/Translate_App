# Context isolation and capture mode fix

This follow-up makes recognized current speech the sole content translated. It
preserves the existing four-tab UI, user preferences, history, queues and provider
consent. Private screenshots, activity logs, sermon text and historical fixtures
remain local and are excluded from Git and the public ZIP.

## Findings

The supplied activity log says `prepared_context=false` for STT. It also records
context applied before Start being cleared when the new session starts, followed
by `recent_translations` entering later translation prompts. The screenshot's
English largely follows its displayed Latvian. Without the source audio, this
does not establish where an allegedly early statement first entered the pipeline.
It does establish two preventable risks: generated English being reused as model
context, and context/logging behavior that obscured whether the prepared document
was active. Both are addressed.

The log's startup tuning line described legacy Balanced values while actual
capture used Dynamic's eight-second ceiling. Those forced chunks alone account
for much of the observed delay; the initial Gemini timeout adds another four
seconds before the authorized fallback. Provider settings were not changed.

## Implemented behavior

- Every Gemini single/multiple-language and OpenAI fallback request uses one
  structured `INPUT_DATA_JSON` contract. `current_source` is the only text to
  translate. `reference_only`, matched glossary hints and boundary flags are
  distinct data fields. Input text is never treated as model instructions.
- Prepared context supplies only vocabulary evidenced by current speech, never
  prepared paragraphs or future sentences. At most one preceding recognized
  source segment, bounded to 400 characters, can help with an ambiguous word or
  pronoun. Generated target-language output is never fed back to a model.
- System instructions forbid translating/continuing reference material, importing
  facts from earlier speech, completing a familiar quotation, adding a clause or
  actor, and filling an unfinished ending. Close word-level interpretation may
  use context; wholesale reconstruction of missing speech is forbidden. Explicit
  numbers, negation and uncertainty remain authoritative.
- Glossary entries are supplied only when their source term occurs in the current
  segment. Unspoken names are no longer routinely placed in prompts.
- Empty/malformed/copied responses and gross unsupported expansion are rejected.
  A quality repair strips all reference/glossary data while preserving exactly the
  same current source. It remains one same-provider attempt, under existing pacing,
  billing consent and spending limits. The expansion check is deliberately broad;
  it cannot identify every small semantic addition.
- OpenAI recognition receives neither prepared context nor a previous-text prompt.
  Local Whisper remains independent of the prepared document. Nonoverlapping
  recognition no longer deletes genuine repeated speech through previous-text
  filtering or collapses repeated tails. Local recognition is not silently rewritten
  from English into Latvian. Existing explicit local hotwords remain a separate
  user configuration; they are not populated from the pastor document.
- A forced audio cut alone no longer sends an entirely silent continuation to STT.
  Continuation needs some measured speech evidence. Existing quiet-word/sample
  retention and unrecognized-audio recovery remain in place. Dynamic recognition
  now uses the capture policy's quiet-speaker scale rather than a mismatched fixed
  threshold. Ambiguous nonzero low-energy continuation audio is retained for review.
- Context explicitly applied while idle is staged for one new session. Unstaged
  old context is cleared, Stop clears active context, and restart does not restore
  it automatically. Logs distinguish prepared context from recent source history,
  and state `previous_translations=false` and `output_scope=current_source_only`.
- Startup diagnostics now report the actual selected capture profile. P95 uses
  the same nearest-rank calculation in the pipeline and dashboard, avoiding
  understated percentiles for small sample counts.

## Capture modes

| Mode | Normal capture window | Boundary behavior |
|---|---|---|
| Balanced | 4–5 seconds | Prefer a pause held for 0.60s after four seconds; otherwise force at five. A retained recent pause cannot shorten the chunk below four seconds. |
| Low Latency | 2–3 seconds | Prefer a 0.45s settled pause after two seconds; otherwise force at three. Provider pacing can still create a queue. |
| Dynamic | Natural phrases, normal six-second ceiling | Adaptive 0.64–0.80s holds preserve short breaths and quiet final words. Longer ongoing speech gets an explicit forced-continuation boundary. |

All modes preserve exact, nonoverlapping sample order. Pause/Stop flush the final
tail even below a normal minimum. Entire digital silence is not uploaded as
speech. Short complete phrases can still be released by Dynamic after a longer
pause. Translation may coalesce already-recognized queued segments; it never
waits for or generates words from a future sermon section. The six-second Dynamic
ceiling reduces forced capture delay from the previous eight seconds; it is not
a promise of six-second audible output.

## Verification

- **239 automated tests pass** (47.235s), including 15 new context/capture tests.
  They inspect actual provider request payloads, prohibit prior English and
  unspoken script/glossary text, test reference-free repair, preserve repeated
  recognition, reject silent forced tails, exercise real Qt context staging, and
  verify real sample partitions and selected-profile logging. Older tests were
  updated to assert the new requested capture windows and source-only contract.
- **Two-hour synthetic capture checks pass in all three modes** with identical
  hashes for every nonzero sample in order: Balanced 1,458 segments / 5.0s maximum;
  Low Latency 2,431 / 3.0s; Dynamic 1,257 / 6.0s. No segmentation errors reported.
- The accelerated **450-row/60-minute simulated service** passes: 150 translated
  batches, both languages delivered/queued in source order, five injected empty
  results repaired, queues drained, workers finished, history bounded to eight
  rows, summary prompt coverage and history reopen confirmed. Providers and
  playback are mocked; this is not natural-service acceptance.
- A **real fixed synthetic Cloud TTS → OpenAI STT** check runs the actual three
  capture profiles over 6.354 seconds of known Latvian audio. All five selected
  fixture keywords are retained in every mode. Balanced produces 5.0s and 1.354s
  chunks; Low Latency 2.1s, 3.0s and 1.254s; Dynamic 6.0s and 0.354s. Short final
  tails are expected. The fixed outputs still include minor recognition errors
  in Balanced/Low Latency; keyword retention is not perfect-transcription proof.
- A **real Gemini English/Lithuanian context canary** does not import the unrelated
  reference claim or previous generated-English marker. The final-prompt request
  takes 1.094s. This checks scope leakage, not complete translation quality:
  the Lithuanian fixture has an awkward case form, so no claim of flawless grammar
  is made. OpenAI translation scope/repair is verified with mocks; direct paid
  translation was skipped because the configuration used for diagnostics had
  paid fallback disabled. No consent setting was changed.
- Source smoke passes the UI, settings, audio enumeration, silent output,
  summary editing and mocked start/stop checks. The final rebuilt EXE is checked
  separately at 100%, 150% and 200% scaling, followed by module/privacy audit.
  Compact final evidence is in `verification/context-capture-2026-10-24.json`.

The final rebuilt EXE passed fresh checks at all three scaling levels, with
dashboard, voice and summary screenshots inspected. All 22 packaged application
modules match source. The public ZIP audit found no credential matches, session
data or private sermon artifacts; template keys are blank.

Useful checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe scripts/verify_long_capture.py build/balanced.json --mode balanced
.\.venv\Scripts\python.exe scripts/verify_long_capture.py build/low-latency.json --mode low_latency
.\.venv\Scripts\python.exe scripts/verify_long_capture.py build/dynamic.json --mode dynamic
.\.venv\Scripts\python.exe scripts/verify_service_simulation.py build/service.json
# Opt-in configured provider usage, fixed synthetic input only:
.\.venv\Scripts\python.exe scripts/verify_context_capture.py build/live-context.json --live
```

The [official OpenAI transcription guide](https://developers.openai.com/api/docs/guides/speech-to-text)
documents optional prompting for the existing transcription integration. This
application deliberately does not supply the prepared sermon or recent speech
as that prompt. Existing model selections are preserved.

## Limits and delivery

Models can still mishear audio or add a small unsupported meaning despite strict
scope instructions. No deterministic cross-language check can prove every clause
is faithful. Original-audio natural sermon accuracy, physical mixer/loopback
isolation, audible voice quality, dual-device playback and Windows 10 acceptance
remain unverified. The supplied input name describes an aggregate audio source;
the log alone cannot establish whether translated playback also enters that mix.

The final standalone application is `dist-app/ChurchTranslator/ChurchTranslator.exe`
with its accompanying `_internal` folder. The distributable is
`dist-app/ChurchTranslator-Windows.zip`. Built binaries stay local/ignored;
code, synthetic checks and nonprivate evidence are committed and pushed.
