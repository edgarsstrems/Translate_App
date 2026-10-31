# Church Translator reliability candidate — 31 October 2026

This candidate fixes a reproduced silence failure and passes local automated
checks. Natural Latvian recognition/translation accuracy has not been measured
against a checked transcript. The user confirmed the existing executable works
well apart from accuracy. Keep that build as the church-service recommendation
until a listening rehearsal establishes that the candidate is better on the
actual mixer/input. A 10–12 second listener delay is a goal, not a guarantee.

**Credential-source distinction:** the first approved replay used the old
development `.env` key and received HTTP 429 `credit_balance_exhausted`. No
automatic retry was sent. The installed EXE already has a different, newer key,
which is preserved. The bounded comparison completed successfully using the installed key: 17
requests, including one recovery check, with no saved key changes. The old-key
error does not establish that the installed application lacks credits.

## Diagnoses and evidence

1. **Nonzero digital noise was treated as speech.** Two closed retained sessions
   contain a virtual-device floor of about -96.7 dBFS RMS / -90.3 dBFS peak.
   These are near one PCM16 quantization step, with stationary broadband energy
   and low speech probabilities, rather than captured speech. The old engine
   submitted every nonzero segment and applied gain up to 4x. One session's
   genuine spoken `Nē!` was followed by eight noise-only segments returning that
   same word. A second session returned previous-sentence candidates during
   the same noise floor. Original PCM, checks and selected/raw hypotheses were
   inspected locally; no private audio was uploaded.
2. **The repeat guard only annotated output.** It retained repeated recognition
   and allowed translation/TTS because wording alone cannot distinguish genuine
   emphasis. Audio evidence is now checked before recognition. Identical spoken
   words remain valid; there is no new transcript deduplication.
3. **Prompt contamination is supported by retained comparisons.** Earlier
   reference-free checks on noise returned punctuation/unrelated language instead
   of the previous Latvian clause. This supports an echo mechanism; it cannot
   prove the cause of every ASR mistake. No new paid A/B recognition was run.
4. **Gemini was prematurely bounded, not currently inaccessible.** The default
   client transport deadline was four seconds. Discovery and inference use the
   same configured key and SDK API route. Current real requests succeeded with
   HTTP 200; regular Flash needed 10.84 seconds for inference. Model discovery
   alone is not an inference or quota test. No authentication, regional, quota or
   payload error occurred in these checks. The first sandbox-only DNS failure
   disappeared with permitted network access and is not a provider failure.
5. **Audit initialization could race.** An unchanged baseline full-suite run
   failed with `database is locked` before recognition. Repeated WAL/schema setup
   on concurrent connections was replaced by initialization before sharing the
   audit. Concurrent writer coverage and the full suite pass afterward.

## Changes and preservation

- Added conservative stationary-noise analysis with the already bundled CPU
  Silero ONNX detector. Quietness alone cannot veto input. Original and
  level-normalized speech evidence must both be weak before stationary quiet
  noise is gated. Digital silence is gated directly. Nonzero gated audio stays
  in local review storage. Ambiguous audio still reaches STT without gain or
  previous/prepared-text prompting. VAD failure preserves ambiguous audio.
- Gemini defaults to a 12-second transport deadline, with a valid server deadline
  and no hidden SDK retries. A genuinely failed request can use an alternative
  only if that model has generated successfully under this key in the same
  translator instance. Recovery uses pending source on the ordered worker;
  it no longer buys a synthetic background generation alongside paid fallback.
  Authentication/daily exhaustion retain long cooldowns. Retry-After response
  headers, including dates and delays beyond 60 seconds, are respected.
- Translation instructions explicitly preserve complete spoken details,
  questions, emphasis, intentional repetition and supported biblical metaphors.
  Current source remains the only output authority; references never supply
  missing sermon sentences or verses. Semantic improvement remains unproven.
- Integrity reports record actual provider per completed batch and separate
  Gemini shares of completed and attempted distinct batches, including failed
  work in the latter denominator. Retries do not inflate distinct batch counts.
- Added clear input-health wording and dBFS diagnostics in the existing meter's
  tooltip and local audit. The percentage remains a display scale, not Windows
  volume. Retained whole-session RMS was -32.7/-33.5 dBFS, with -14.0/-12.2 dBFS
  peaks, leaving headroom. No blanket gain or Windows/Elgato volume change.
- Preserved the four tabs, dashboard/panels, QR, context isolation, saved keys,
  history, exports, summary language behavior, pause/resume, end-session drain,
  transcript retention and ordered durable playback. English remains
  `en-US-Chirp3-HD-Iapetus`, male, speed 0.95. Paid fallback consent is preserved.

## Capture comparison

| Mode | New normal capture range | Two-hour synthetic segments | Largest segment |
| --- | --- | ---: | ---: |
| Balanced — initial recommendation | 4–6 s, settled pauses | 1,257 | 6 s |
| Dynamic | 2.5–10 s, natural pauses; soft target 6 s | 810 | 10 s |
| Low Latency | 2.5–4 s, pause-sensitive | 1,828 | 4 s |

Stop/Pause can emit shorter tails. Dynamic can use a recent eligible breath when
the hard limit is reached; the remaining PCM is carried into the next segment.
All modes preserve exact nonoverlapping source ranges. No overlap was introduced:
there is no checked transcript demonstrating a safe text-reconstruction policy,
and automatic deduplication could delete genuine repetition. Longer boundaries
and retained samples cannot guarantee that ASR recognizes every word.

Two retained recordings (67.4 and 96.1 seconds) were partitioned in every mode,
both at original level and at 1% level. Sample reconstruction matches exactly.
All 35 original speech candidates remain eligible after the 100x reduction.
Fourteen original stationary-noise segments are gated, including nine that
previously produced selected text. This verifies the observed silence mechanism,
not universal whisper detection or a natural word-error improvement. Checked
reference WER, omissions, translation fidelity and natural sentence accuracy
remain unmeasured; the prepared sermon is not speech ground truth.

## Gemini checks, rates and cost

| Model | HTTP | Inference time | Result |
| --- | ---: | ---: | --- |
| Gemini 3.5 Flash-Lite | 200 | 3.27 s | Faithful-looking fixed English fixture |
| Gemini 3.1 Flash-Lite | 200 | 0.73 s | Same fixture; same English wording |
| Gemini 3.6 Flash | 200 | 10.84 s | Same fixture; no demonstrated quality advantage |
| Gemini 3.5 Flash-Lite | 200 | 1.16 s | Fixed source translated jointly to English/Yoruba |
| Gemini 3.5 Flash-Lite | 200 | 1.16 s | Yoruba numbers written as words; output synthesized locally |

Request spacing is reported separately from inference. Discovery took about
0.4 seconds. All five small generation checks succeeded; OpenAI was disabled in
the checker. This is **not** a representative Gemini success/fallback rate and
does **not** establish the 99% objective. Production rate counters are now saved
for subsequent real services. The two inspected pre-update sessions had **43/43
completed translation groups on OpenAI and 0/43 on Gemini**. Their complete
attempt denominator was not recorded, so the attempt rate cannot be recovered. No regular-Flash default change is justified by
this evidence. Quotas are project-specific; local spacing is not a quota promise.
The user's free-tier confirmation was used; the API key cannot independently
establish billing status or reveal all account/project restrictions.

Additional OpenAI testing: **17 successful transcription requests** on the
installed key, including one bounded recovery check; one earlier request on the
old development key was quota-rejected and was not retried. Server usage totals
were 2,224 input tokens and 316 output tokens. A conservative token-based cost
estimate is **$0.016504** (using $6/M for all input and $10/M output), below the
user's **$0.20 maximum** and the replay's $0.05 ceiling. This is not an invoice;
the input estimate deliberately overprices any text tokens. The failed request's
$0.03 reservation was an upper-bound reservation, not measured spending.
Gemini: five successful fixed-fixture generations on the user-confirmed free-tier
project, estimated incremental charge $0.00. No billing tier or saved fallback
authorization was changed. No paid OpenAI translation or Google Cloud TTS test
was made.

## Optional local Yoruba

Added Yoruba to the existing language selector. Settings → Voice Settings /
Yoruba Setup → Yoruba offers an explicit one-time Install action, progress,
status and voice preview. Runtime dependencies ship with the EXE; no system Python
or pip is required. The approximately 139 MiB model downloads only after consent,
persists under the application's model directory and works offline afterward.
Interrupted downloads retain completed verified files; incomplete files cannot
be loaded. Every model/config file has a pinned size and SHA-256 digest.

Model: `facebook/mms-tts-yor`, revision
`d359e06b481cb5402e2e4215475b90c07a3f12e2`. Safetensors is used, never pickle or
remote model code. Dependencies are pinned in `requirements-local-yoruba.txt`.
MMS model files are not bundled in the public ZIP; attribution is installed beside
the downloaded model. Its CC BY-NC 4.0 license permits qualifying noncommercial
use with attribution. A church's actual use must meet those terms; nonprofit
status is not by itself a license determination. Commercial use requires other
permission. Installation presents that restriction explicitly.

Three representative Yoruba sentences synthesized with networking blocked.
First CPU load was about 9.45 seconds; synthesis took 0.95–1.39 seconds for
2.83–4.53 seconds of audio in that run. Unicode NFC preserves tone marks, and
the actual tokenizer distinguishes accented and unaccented wording. This does
not verify pronunciation or correct tones. No native-speaker evaluation was
available. English text is not used as the Yoruba translation intermediary.
The tokenizer otherwise drops unknown characters, including digits. Requests for
Yoruba now ask for numbers as spoken Yoruba words; synthesis explicitly retains
unsupported numeric/name output for review instead of silently deleting it.
The final real Gemini output contained no digits and produced 11.06 seconds of
local audio with networking blocked. That source process used about 573 MiB peak
working set; its load plus synthesis took 11.20 wall seconds / 19.47 CPU seconds.
The packaged EXE independently synthesized all three fixed Yoruba sentences
offline. Its cold load was 29.16 seconds; subsequent synthesis took 1.03–1.55
seconds. Warm the optional voice with Preview before using it during a service.
Optional Yoruba failures retain durable jobs in `deferred-speech-yo.sqlite`
while subsequent English output continues. No automatic replay is performed.

Keep Yoruba off for the service until its pronunciation, output route and
sustained CPU/RAM behavior have been reviewed on the church computer.

## Verification and remaining acceptance

- Full suite: **318 tests pass**, 47.4 seconds; includes concurrent audit writes,
  noise/repetition isolation, quiet/short-tail preservation, context, consent,
  installer corruption/interruption, output order and Yoruba failure isolation.
- Every two-hour synthetic profile accounts for **115,200,000 samples**, with
  equal hashes and no introduced gaps/overlap. These are sample, not word checks.
- Thirty-minute callback/journal/queue/encoded-STT pipelines in all modes account
  for **28,800,000 samples** each. Dynamic/Balanced/Low Latency used 203/314/347
  mocked STT requests; all workers drain with no errors, gaps or pending jobs.
  Production batching yields 202/294/319 translation/synthesis/playback jobs.
  Accelerated maximum backlogs (68/145/141) are not real-time latency results.
- One simulated hour: 450 source rows, 150 translation batches, both output
  languages complete, five injected invalid responses repaired; summary/history
  reopen, exports and both queues drain. Providers and timing are simulated.
- Source Qt smoke and rendered 1,000-segment/panel stress pass. All text, manual
  reading pixels, selection, resizing and follow-bottom are preserved.
- Packaged EXE smoke passes at **100%, 150% and 200%** Qt scaling, including
  640–1280 logical-pixel navigation checks, all four tabs, isolated settings
  restart, routing reindex/disconnect, exports/history/summary UI and the actual
  bundled silence gate. Selected screenshots, including Yoruba setup, inspected.
- All **25 compiled application modules match source**. The staging ZIP audit
  finds blank template keys, no credential matches and no forbidden local data.
  The package includes the CPU Yoruba runtime, not downloaded user models.
- Packaged Yoruba synthesis passes offline for all three fixed sentences; no
  separate system interpreter is used. Public ZIP is approximately **833 MiB**,
  substantially larger because it bundles CPU PyTorch. No Windows 10 machine
  was available; this host reports Windows build 26300.

### Limited real-recording recognition comparison

The same approved **24-second** retained excerpt was recognized with the installed
OpenAI key in all three candidate profiles:

| Mode | Segments | Selected words | Total STT processing | Individual STT range |
| --- | ---: | ---: | ---: | ---: |
| Balanced | 5 | 37 | 6.02 s | 0.75–2.61 s |
| Dynamic | 3 | 41 | 3.50 s | 0.80–1.69 s |
| Low Latency | 8 | 42 | 5.55 s | 0.59–0.91 s |

One reference-free recovery check is included in the 17 network requests. The
three profiles differ in wording and details. More recognized words are not
proof of greater accuracy; no checked reference exists and this excerpt begins
partway through a sermon. WER, missing/correct words and a natural accuracy
ranking remain unknown. The excerpt and a private listening comparison are
under ignored `build/reliability-2026-10-31/RECOGNITION_COMPARISON.md`.

A cached local large-v3-turbo model also recognized the excerpt offline in all
three modes. CPU processing took 53.86/31.03/73.94 seconds for 24 seconds of input
(Balanced/Dynamic/Low Latency), under concurrent release-work load: it did not
keep pace in this test. Cached local Small processed Balanced in 15.67 seconds,
which is promising throughput for an emergency fallback but establishes neither
Latvian accuracy nor sustained provider/playback performance. No saved backend
was changed. The installed OpenAI key works, so local fallback is optional.

Natural accuracy, audible English quality, long real-provider backlog behavior,
speaker diarization, physical loopback and Windows 10 acceptance remain open.
Saved Elgato input captures All sound while English uses System output; verify
that the mixer's input bus excludes translated playback. No mixer routing was
changed without evidence of its current internal bus configuration. There was
no new live-service recording with a checked transcript. No claim of universal
silence rejection, substantial WER improvement or 10–12-second sustained delay.

## Service settings and rollback

For November 1: keep the current working build unless the candidate passes a
short rehearsal containing speech, a long silent pause and a genuinely repeated
phrase. Start with Balanced, OpenAI `gpt-4o-transcribe`, Gemini Automatic
Flash-Lite, and saved Iapetus 0.95. Keep prepared context explicitly opt-in and
Yoruba disabled until reviewed. Saved mode choices were not overwritten.

Candidate final layout:
`dist-app/ChurchTranslator/ChurchTranslator.exe` and
`dist-app/ChurchTranslator-Windows.zip`.

An immediately runnable copy of the previous executable is also prepared at
`build/reliability-2026-10-31/rollback-working/ChurchTranslator/ChurchTranslator.exe`,
with the existing private configuration copied locally. Its EXE hash matched the
pre-update installed EXE. Keep that folder private.

The previous public build is preserved at
`build/reliability-2026-10-31/rollback-ChurchTranslator-Windows.zip`.
Recoverable source checkpoint: `8e48d74` (includes earlier uncommitted fixes).

Exact rollback procedure, after ending the session and closing the candidate:

1. Extract the rollback ZIP into a separate folder, for example
   `build/reliability-2026-10-31/rollback-running/`.
2. Copy the current `dist-app/ChurchTranslator/.env`, `credentials` folder and
   `glossary.json` into the extracted `ChurchTranslator` folder. Keep these private;
   the rollback/public ZIP intentionally contains blank keys.
3. Launch that extracted `ChurchTranslator.exe` beside its `_internal` folder.
   The existing LocalAppData settings/history/models remain in place. Do not
   delete them or uninstall the application. Recheck input/output selections.

## Official references

- [Gemini generateContent](https://ai.google.dev/api/generate-content),
  [pricing](https://ai.google.dev/gemini-api/docs/pricing),
  [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits),
  [troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting).
- [OpenAI speech-to-text prompting](https://developers.openai.com/api/docs/guides/speech-to-text),
  [transcription pricing](https://developers.openai.com/api/docs/pricing).
- [Silero VAD](https://github.com/snakers4/silero-vad),
  [Meta MMS Yoruba model card](https://huggingface.co/facebook/mms-tts-yor),
  [CC BY-NC 4.0 terms](https://creativecommons.org/licenses/by-nc/4.0/).
