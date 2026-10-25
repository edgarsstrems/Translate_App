# Summary language and recognition review verification

This release fixes session-specific summary language and preserves recognition
review evidence. It does **not** establish improved natural-sermon recognition or
repair the four damaged historical phrases. Experimental changes without a
demonstrated benefit were removed. No transcript was replaced with prepared text.

| Requested verification | Result |
|---|---|
| Latvian default | New sessions select Latvian even when old global settings say English. The existing English live panel and selectable output are unaffected. |
| Language persistence | Each session stores its own selection in SQLite. Selection survives restart even before generation. Saving/generating remembers the report language; legacy summaries use recorded metadata or the sole available language. English regeneration and Latvian regeneration are tested. Existing copy, edit, save and export paths retain the selected report language. |
| Completed session summaries | Real Qt/Gemini generation after End Session, regeneration and History passed twice. The settled-render run saved 53, 68 and 54 words, respectively, in Latvian. Capture drain was simulated; no active microphone was needed. |
| Context matching | A separate global-vocabulary fallback was evaluated and removed. It did not activate in the tested passages or selected historical model results, so there was no measured recognition benefit. Section evidence gates remain. |
| Overall context during transitions | Existing bounded document vocabulary still bootstraps recognition. Recent recognized Latvian narrows later requests. Unrelated recent speech may correctly omit prepared vocabulary; this release does not claim continuous semantic understanding of the whole sermon. |
| OpenAI delivery | Accepted real `gpt-4o-transcribe` requests contained prepared vocabulary and previous source where applicable. Supported-model, actual request, model-specific budget, DOCX paragraph/table parsing and session cleanup regressions pass. Real A/B used the previously extracted document text. Delivery is not proof of improved recognition. |
| Gemini delivery | Real baseline/candidate translation requests received source-specific prepared context when the existing matcher selected it. Current source remains separate from references; generated translation is not fed back. Paid OpenAI translation fallback stayed disabled. |
| Recognition improvements | None demonstrated on natural audio in this pass. The store-word corruption reproduced in both arms. The ulcer passage yielded a first-person account in both arms; this supports investigating the historical ASR but does not certify the words. |
| Remaining recognition errors | Store wording and boundary omissions remain. Scientific and colloquial excerpts were also probed; those model hypotheses and the earlier bilingual transcript are not independent verbatim references. No natural WER or certain correction is reported. |
| Uncertainty | Raw OpenAI text, selected text, review reason and reference-free reconciliation now remain alongside audio in the durable review queue, including when the selected result is empty. A failed optional check retains that evidence without speaking an unverified sparse result. Restart persistence is tested. Token scores remain uncalibrated; incoherent but confident ASR can still evade this gate. |
| Eight-second capture | Preserved. A fresh two-hour synthetic Dynamic run produced 900 ordered segments, maximum 8 seconds, with identical hashes for all nonzero samples. Real A/B used the actual nonoverlapping Dynamic partition. There is no evidence here to certify eight seconds as optimal or to justify increasing it. No overlap was added. |
| English faithfulness | Two revised prompt variants were tested against identical damaged Latvian, plus an unrelated/reference control. Neither reliably resolved the errors; the stronger variant still rendered the ulcer phrase nonsensically. Both variants were removed. The baseline translation behavior remains, with this limitation explicitly open. |
| Latency and voice | One fixed synthetic chain: capture 4.572s, STT 3.157s, Gemini 0.906s, English TTS 1.219s; speech-ready 9.854s from simulated capture start. Muted real-device output estimated starts at 10.10s and 18.11s, queue waits 0.03s and 8.03s. Iapetus/0.95 was used diagnostically; saved preferences were unchanged. These are not natural-service averages or physically measured audible starts. |
| Regression and GUI | **274 tests pass** in 52.359s. Source smoke and rendered stress pass. The 1,000-segment-per-panel stress preserved selection, focus, follow-bottom, all text and pixel-identical manual reading. Equal panel geometry passed. A simulated hour delivered 450 rows per language in order and drained its queues. The summary verification harness now waits for queued layout events before taking screenshots; settled images were inspected. |
| Executable and release | The rebuilt EXE passes 100/150/200% scaling and the archive passes module parity, private-path, credential and blank-key checks. The original package directory denied replacement of a native dependency, so a fresh build was made under `dist-app/final-accuracy/`. The default `dist-app/ChurchTranslator/` was restored from it: 78 generated files copied, 1,931 identical files preserved. No local settings or credentials were overwritten. Packaging tools now accept a configurable output directory. Detailed evidence is in `verification/final-accuracy-2026-10-25.json`. |

The first global-vocabulary and translation experiments failed their usefulness
checks. Their reports, subsequent checks and original audio remain under ignored
`build/final-accuracy/`; they are not in the public distribution. Controlled API
tests were authorized by section 4 of the supplied request. The initial approval
review rejection was resolved by presenting that explicit authorization.

The comparison made 20 STT requests, two additional 18-second audio diagnostics,
and 15 Gemini translation chains. Separate fixed fixtures used one STT chain,
six Gemini summaries and one Gemini translation/TTS latency chain. No exact
billed-cost claim is made without a provider invoice. No private report, audio,
document, session database or credential is intended for Git or the public ZIP.

Remaining acceptance requires independently checked verbatim Latvian, reliable
boundary and corrupted-word recovery, a complete natural service, church
acoustics, physical audible routing/voice quality and Windows 10. In particular,
the quoted science and colloquial examples must not be confidently rewritten
from a prepared document or a second model's guess.
