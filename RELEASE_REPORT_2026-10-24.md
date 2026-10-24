# Church Translator audit and Windows release

The audit preserves the existing four-tab PySide6 design, Latvian source audio,
saved settings, provider preferences, durable queues and summary history. It found
focused response-validation, translation-alignment, context-parsing and dual-voice
failure gaps. These were fixed and verified without redesigning working systems.

The supplied sermon documents were reviewed in full. Detailed timestamped analysis
and historical regression excerpts remain **local**, under ignored `private-audit/`,
at the user's request. They are excluded from Git and the public Windows ZIP.
Only synthetic regression fixtures and aggregate verification evidence are shipped.
The prepared sermon is not treated as verbatim spoken ground truth, and no ASR
accuracy percentage is claimed without original audio.

## Fixes and implementation

| Files | Problem and resulting behavior |
|---|---|
| `translation_quality.py`, `services.py` | Empty output, incomplete JSON and clear multiword Latvian source copies are rejected before delivery/history/TTS. One same-provider repair attempt respects Gemini pacing and existing paid-fallback consent. Two failed responses do not multiply into repairs across the model catalog. Authorized OpenAI repairs recheck the spend limit and account for both responses. Short shared words, names, bracketed speech and Latvian targets remain allowed. |
| `services.py` | Truncated JSON is no longer rescued with regex and passed off as a complete sentence. Correct single-language JSON and plain-text responses remain supported. Quality logs report provider/language/reason without logging transcript content. |
| `session_manager.py`, `engine.py`, `transcript_exporter.py` | Coalesced source batches now retain explicit translation ownership. Readable TXT/Markdown/DOCX/PDF exports align the combined source with its translation once, with a timestamp range. JSON preserves every source row and group ID. The SQLite migration is additive and idempotent. Old blanks are labeled as unsaved, never filled by guessing. |
| `engine.py` | A synthesis error for one language no longer prevents attempting the other. Completed output is recorded in the durable job; failed language text remains recoverable. Reopen/recovery tests confirm the successful language is not queued twice. The stage still pauses after a partially failed job; recovery remains the established explicit backlog workflow. |
| `sermon_context.py` | Reference parsing supports numbered abbreviations, singular Psalm labels, Mt and en-dash ranges. Equivalent Unicode diacritics match consistently. A regression caught a pattern absorbing a verse number from the preceding line; that was fixed. No prepared number replaces an explicitly recognized chapter/verse. |
| New verification scripts/tests | Offline document parsing, fixture replay and an accelerated simulated service provide reusable checks. No desktop runtime dependency or extra routine model call was added. README and AGENTS locate the workflows. |
| `verify_windows_package.ps1` | The first packaged diagnostic timed out in this checkout's path containing spaces. Explicit argument quoting and report-freshness checks fix the verifier and prevent stale reports from counting as successful checks. Timed-out diagnostic processes are terminated. |

## Audit of the previous 17 requirements

| # | Requirement | Current verification |
|---|---|---|
| 1 | Resizable window | Existing scrollable layout/small-window Qt tests pass; four-tab design preserved. |
| 2 | Live auto-scroll | Real-widget tests verify independent follow-bottom, manual-scroll hold and resume. |
| 3 | Sermon context | Conservative segment relevance and bounded history retained; new notation/Unicode tests pass. No measured audio-level benefit claimed. |
| 4 | Dynamic transcription | Sample-preserving segmentation and continuation tests pass. The eight-second forced boundary remains. |
| 5 | Windows 10 dark theme | Fusion palette and current-host control screenshots checked. Actual Windows 10 rendering remains unverified. |
| 6 | Summary close/reopen | Existing close/cancel/late-result and history regressions pass. |
| 7 | Multilingual TTS | Live fixed English, Latvian, Lithuanian and Russian synthesis passes. Universal language availability is not claimed. |
| 8 | Voice selection UI | Locale compatibility, catalog controls and Qt tests pass; screenshot inspected. |
| 9 | Summary UI | Resize, edit, button fit and wrapping pass; source and packaged screenshots inspected. |
| 10 | Multiple voices | Live account catalogs return multiple choices. Natural voice quality was not listening-tested. |
| 11 | Context logging | Logs distinguish availability, relevance, supplied context and provider support; model influence remains unknown. |
| 12 | Context reset | New-session/restart and explicit template-load regressions pass. |
| 13 | Hallucinations/ordering | Strict prompts and ordered pipelines retained; new output checks and 450-row simulation pass. Arbitrary meaning errors remain possible. |
| 14 | Summary history | Automatic save/reload and cancellation tests pass; Latvian remains default. |
| 15 | Session cleanup | Stop/new-session/old-signal tests pass. Stop preserves visible text and history, clears active context/summary. |
| 16 | Voice settings | Language-specific voice/rate/gain and capability-gated pitch/preview tests pass. Saved preferences override fresh defaults. |
| 17 | Dynamic pause threshold | Adaptive 0.64–0.80s remains in place. Tests preserve 440/600ms internal pauses. Balanced/Low Latency settings retained. |

## What context actually does

The user explicitly applies context after starting a session. The context manager
parses sections, vocabulary and references; saved context does not automatically
activate on restart. Translation requires fresh current-segment evidence: at least
two shared content words and 35% overlap. It supplies matching vocabulary rather
than a whole draft, plus explicitly present references and bounded relevant
source/translation history. Exact-word matching can miss inflections or badly
damaged recognition. It is not speaker identification.

OpenAI STT requests Latvian without a text prompt. Local Whisper uses neutral
Latvian instructions and explicitly configured glossary hotwords. Prepared document
body text is not sent to recognition. The summary UI uses actual source segments
and no prepared body text; free web translation cannot accept context. Logs prove
these actions, never that a model relied on a hint. Historical context use cannot
be established without that session's logs. English resembling a familiar
quotation does not prove context was supplied.

No speculative correction model or document-derived STT prompt was enabled: its
benefit cannot be measured without original audio, and unrelated speech must not
be forced into a prepared script.

## Tests and measurements

The clean baseline passed 210 tests in 60.085s. The expanded suite passes **224
tests** in 48.814s, including 14 new focused audit tests. Intermediate failures caught a
single-language JSON compatibility case and changed retry-count expectations;
they were resolved before the passing run. Local failed and successful logs are
retained under ignored `build/audit-2026-10-24/`.

| Check | Result |
|---|---|
| Private local evaluation | All 42 diagnostic cases pass, including 30 historical and 12 synthetic cases. Six manually justified references translate exported text, not verified original speech. Full private results remain local. |
| Public fixtures | All 12 synthetic diagnostic cases pass; empty/malformed/copied output is detected while shared names, unfinished clauses and intentional repetitions remain allowed. |
| Deterministic comparison | Across the local 42-case set, the old nonempty gate flags 2 candidates and new checks flag 6. This measures response failure detection, not model/translation/ASR accuracy. |
| Validator overhead | Median 0.0174ms, maximum 0.1316ms for the final fixed local run; too small a sample for a production guarantee. |
| Two-hour Dynamic capture | 7,200 simulated seconds, 898 segments, maximum 8.0s; all nonzero samples preserved in order with matching hashes and no reported errors. |
| Simulated service | 450 source rows / 150 batches; both languages receive all 450 rows in order; 5 injected empty responses repaired; work queues drain, history stays at 8 rows, workers finish. |
| Simulation resources | 4.312s drain wall time, 1,502,663 peak traced Python bytes. Virtual pacing and mocked providers/audio; not process RSS or a wall-clock hour test. |
| Summary source/history simulation | All 450 source rows reach the real summary prompt; a controlled Latvian result saves and reopens. Full-sermon model faithfulness remains unverified. |
| Source smoke | Four tabs, settings restart, routing, 26 input / 35 output devices, 3 mocked start/stop cycles, summary and voice controls pass. Actual microphone frames and silent output checked; no microphone upload. |
| Exports | Real SQLite/TXT/Markdown/JSON grouping regressions pass; DOCX paragraph structure checked. Generated PDF text/alignment and rendered page checked. Native Word rendering was unavailable. |
| Live fixed services | Gemini translation and Latvian summary, OpenAI Latvian transcription, Cloud catalogs and en/lv/lt/ru synthesis pass. |
| Live synthetic serial chain | STT 4.88s, translation 0.67s, synthesis 0.51s; 6.06s after capture to audio ready. One fixture, no physical playback. |
| Three live dual-language fixtures | All Gemini: 3.094s, 4.500s, 4.875s; median 4.500s, slowest 4.875s. Three samples do not establish a production P95. |

Initial live checks failed on sandbox DNS. A bounded network-enabled rerun passed
using only fixed synthetic phrases. Paid translation fallback stayed **off** for
all live checks; its repair/consent/cost behavior was tested with mocks. No private
sermon, microphone capture or session database was uploaded.

Fresh English Cloud defaults remain **en-US-Chirp3-HD-Iapetus**, male, rate 0.95.
The live check retained the existing saved Standard-J English preference. Both
fresh-default and saved-preference regressions pass. Normal successful requests
make no extra model calls. A rejected Gemini result can add one paced request;
an authorized OpenAI repair can add one billed request within the existing cap.
There is no claimed before/after improvement in natural ASR or model latency.

Official documentation checked: [OpenAI transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
supports prompting for the configured transcription model; this app retains its
unprompted Latvian path pending audio evidence. [Google rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
vary by project/model/tier. [Cloud Chirp 3](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd)
lists Iapetus as male and supports pace control. No provider/model preference was
migrated during this audit.

## Windows build and distribution

Reproduce after the final application edit:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/package_windows_app.ps1 -SkipDependencies
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify_windows_package.ps1
.\.venv\Scripts\python.exe scripts/audit_windows_release.py
```

Final verification and hashes are recorded in `verification/audit-2026-10-24.json`.
The rebuilt EXE passed fresh 100%, 150% and 200% scaling checks. All 22 packaged
application modules match the final source; the ZIP audit found no credentials,
session data or private sermon artifacts, and public template keys are blank.
Dashboard, summary and voice screenshots were inspected. The package filter also
passed a dummy-private-file exclusion check.

The standalone EXE is `dist-app/ChurchTranslator/ChurchTranslator.exe`; keep its
`_internal` folder beside it. The public distribution is
`dist-app/ChurchTranslator-Windows.zip`. The root `ChurchTranslator.exe` is the
existing source launcher, not the standalone distribution. Built binaries remain
ignored by Git; source, tests and nonprivate evidence are committed and pushed.

## Readiness and remaining limitations

This build is suitable for a **supervised** service with the source transcript
visible, checked routing, account quota headroom and the existing backlog recovery
procedure available. It is not certified for unattended, error-free 60-minute
translation. Deterministic validation cannot reliably repair damaged source speech
or detect every invented meaning.

Original sermon audio, natural long-service ASR, physical speaker changes,
diarization, audible voice quality, physical dual-device playback, real-time
hour-long memory growth, Windows 10 rendering and native Word layout remain
unverified. Energy segmentation is not linguistic sentence recognition. Outages
and exhausted quotas can pause processing; crash boundaries can leave a partially
audible recoverable job. Short ambiguous copied fragments and arbitrary semantic
errors still need review. Controlled/synthetic results are not natural-service
acceptance.
