# Church Translator recovery release — 25 October 2026

This release repairs navigation/status layout, incorrect ADC loss diagnostics,
and a stale-message state bug. It preserves the existing speech-completeness,
context, summary and durable-queue improvements. Natural-sermon recognition
accuracy has not been established; another controlled church test is required.

## Git investigation and recovery point

The starting HEAD was `923db4c`, with 26 modified/untracked project files.
Before editing, their contents, a binary Git diff and the starting revision were
saved under ignored `build/responsive-recovery/before/`. Existing edits were
preserved. The root `SKILL.md` and project instructions were followed.

Recent history reviewed:

| Commit | Relevant behavior and decision |
|---|---|
| `165ef22` | Earlier short-phrase/capture work; historical code included energy-triggered buffer resets. Restoring the whole recorder could restore loss risks. |
| `3fa3618` | Latvian-only recognition and selectable-language work. Retain Latvian source restrictions. |
| `003d0cf`, `0241ef2`, `aa0fe38` | Context, voice defaults, summary/Windows styling and bounded provider failover. Retain existing behavior and paid-fallback consent. |
| `7d27884` | Translation-quality checks and explicit source/translation batch alignment. Retain them. |
| `1c3eb71` | Source-only translation context and capture profile tuning. Its shorter Dynamic ceiling was subsequently corrected. |
| `dc92146` | Eight-second Dynamic ceiling with six-second soft target; incremental text insertion, manual-reading stability and playback diagnostics. Retain these tested improvements. |
| `923db4c` | Bounded STT document vocabulary/previous-source prompts and completed-session summaries. Retain reference safeguards and lifecycle behavior. |

The existing release reports provide controlled test evidence for these versions,
but do not identify a human-certified best home-tested recognition version.
No natural accuracy winner can be selected from commit dates or test counts.
No whole-project rollback or speculative recognition/prompt rewrite was made.

## Regressions repaired and improvements retained

- Status occupied `QTabWidget`'s corner widget, taking space from navigation.
  Status now has a separate row. Each tab page scrolls independently; navigation
  and status remain visible. Native tab overflow buttons retain full tab labels
  when selected at narrow widths. Small pages retain scrolling to full-size
  controls rather than shrinking them until unreadable.
- Modes display D/B/L/C with full names in tooltips. State displays Live/Pause/
  Idle, with compact Checking/Waiting/Blocked states. Microphone levels use
  `Mic: N%`. Latency uses measured latest/average values; p95 remains in a tooltip.
  The latency region reserves two lines, so changing numbers cannot move panels.
- Queued worker status messages could mark a paused or stopped session Live.
  Such messages now respect the actual engine, pause and stopping state.
- The new ADC diagnostic treated zero timestamps as measured negative gaps.
  A real local input stream returned zero on all 11 observed callbacks, reproducing
  the screenshot's -0.100000-second messages. Zero, absent and nonfinite clocks
  no longer imply loss. Forward timing gaps still warn. Backwards/repeated clocks
  report unreliable timing once per stream, without claiming proven missing audio.
  Input-overflow warnings and sample-accounting checks remain active.
- The Audio Routing note now correctly describes Dynamic's eight-second ceiling.
- Removed three hardcoded button width minima that could override Qt's styled
  text size and trim labels on narrow pages. An explicit geometry regression
  first caught this failure; it now passes using Qt's natural button size hints.
- Existing Fusion dark palette, dropdown/slider styling and four-tab design remain.
  No OS-specific rendering fix beyond the existing deterministic style was needed.

The incoming working tree already removed additional energy/text loss gates,
disabled local Whisper's secondary VAD, journaled raw callback samples, retained
uncertain recognition with audio, classified exhausted billing as non-retryable,
and made summary language session-specific. These changes are included and covered
by the current suite; they were not newly invented in this layout repair.
Source-only context, bounded reference-free recognition reconciliation, failed
request retention, ordered translation/playback, and provider consent remain.
No overlap, fuzzy deduplication, longer Dynamic segments or default model change
was introduced. Context delivery tests do not establish speaker identification or
improved natural recognition.

## Screenshot review

1. Reduced desktop window: addressed header competition and made page scrolling
   independent of navigation. At very small widths page controls still use scrollbars.
2. Cropped dashboard: useful clipping evidence, but the crop alone cannot establish
   which offscreen controls were inaccessible. Fresh width/geometry tests cover that.
3. ADC errors: reproduced the unavailable-clock diagnostic defect and repaired it.
   The displayed transcript does not establish the actual spoken words.
4. Long paused/latency/microphone labels: replaced with compact displays and retained
   detailed latency statistics in the tooltip.
5. Navigation overflow beside live status: separated status from navigation and
   verified each tab's full selected rectangle stays outside the overflow buttons.

## Verification

- Baseline: 287 tests passed in 45.772 seconds.
- Updated suite: 291 tests passed in 44.538 seconds; none failed or skipped.
  Four new behavioral regressions cover ADC
  clocks, exact callback sample preservation, tab access at 640/720/960/1280 logical
  pixels, changing measurements, compact labels and queued paused-state updates.
  Existing tests cover quiet onsets, tails, request failure/reopen, ordering,
  repeated speech, context loading/delivery/unrelated speech, Gemini failure,
  paid consent, summaries, restart, End Session and close/drain behavior.
- Source smoke passed: all four tabs, header widths, equal transcript geometry,
  settings/restart, mocked start/stop, summary editing/language and SDK imports.
  It also exercised brief microphone capture and silent output without providers.
- Real production-recorder check: 19,200 callback samples received, consumed and
  emitted; no warnings. Audio was held briefly in memory, not uploaded.
- Two-hour accelerated Dynamic segmentation: 936 ordered segments, eight-second
  maximum, matching hashes for all nonzero samples, no errors.
- 120-second synthetic callback/journal/durable-queue/encoded-request check:
  1,920,000 samples preserved, 16 requests to a simulated endpoint; exact float32
  partition including zeros and expected PCM16 request audio, no errors.
- Rendered Qt stress: 1,000 segments per panel, no document removals, selection
  preserved, pixel-identical manual-reading viewport and follow-bottom recovery.
- Simulated one-hour service: 450 source rows and 450 translations/speech rows per
  language, injected malformed responses repaired, queues drained, history reopened.

Source click/overflow checks passed at 100/125/150/200% Qt scaling. The first
packaged build passed all four scales and the archive audit; visual review then
prompted the button-width repair above, followed by a final rebuild. Final build
also passed **100/125/150/200%**, each at 640/720/960/1280 logical widths, including
the button-label geometry assertions. All **23 packaged modules match source**;
the archive audit found no forbidden paths or credential matches and confirmed
blank public keys. Scaling, archive audit and executable hash are recorded in
`verification/responsive-recovery-2026-10-25.json`. Supporting screenshots,
the intermediate failed button assertion, and detailed logs stay under ignored
`build/responsive-recovery/`.

## Controlled live API check and cost boundary

The user explicitly authorized a small test with the updated key in the latest
packaged application. The diagnostic read that key without printing or saving it
to reports. Paid OpenAI translation fallback was disabled for the diagnostic.
No private sermon, prepared document or microphone audio was uploaded.

Initial setup lacked isolated Google credentials; a subsequent restricted-network
attempt failed DNS before STT. The successful network-enabled run used the existing
source Google credential file only within its diagnostic process. Saved provider,
voice and consent settings were not modified.

The successful run made **one OpenAI transcription request for 4.57225 seconds**,
one Gemini translation chain and two short Google TTS syntheses (Latvian fixture
and English output). The same English bytes were played twice with volume zero;
there were no additional TTS calls for replay. Recognition was not flagged uncertain.
Measured STT/translation/English-TTS times were 1.610/0.859/1.031 seconds.
Speech-ready time was 3.500 seconds after capture, or 8.072 seconds from simulated
capture start. English used `en-US-Chirp3-HD-Iapetus` at 0.95. This is one synthetic
chain, not a natural latency average, calibrated ASR score or audible voice review.
No billed dollar amount is asserted without reliable provider billing data.

## Artifacts, cleanup and remaining acceptance

- EXE: `dist-app/responsive-recovery/ChurchTranslator/ChurchTranslator.exe`
- ZIP: `dist-app/responsive-recovery/ChurchTranslator-Windows.zip`
- Report: `RELEASE_REPORT_2026-10-25_RESPONSIVE_RECOVERY.md`

Run the EXE with its entire folder. The public ZIP contains blank keys and excludes
local settings, credentials, sessions, recordings and private diagnostic reports.
The local installed folder retains the latest packaged application's configuration.
That configuration referenced a missing relative Google credential file in the
previous packaged folder. The existing source credential was copied to the new
local folder at that same relative path, preserving the `.env` bytes exactly and
restoring local Cloud TTS configuration. It remains excluded from the public ZIP.
Obsolete packaged ChurchTranslator executables are removed only after replacement
verification, as requested: **three obsolete EXEs removed** from `dist-app/ChurchTranslator/`,
`dist-app/final-accuracy/ChurchTranslator/` and `dist-app/speech-completeness/ChurchTranslator/`.
Each exact old EXE was first verified recoverable from its existing ZIP. Their
settings/credentials and previous ZIP archives remain. The root source launcher
and third-party helper binaries are retained.

Remaining acceptance: representative human-checked Latvian reference audio,
natural omissions/boundary/hallucination measurements, church acoustics and routing,
physical audible quality, extended real-device operation, and Windows 10 rendering.
Qt scale-factor tests on the current Windows host do not replace testing actual
Windows display scaling, Windows 10, mixed-DPI monitors or church hardware.
Fluent recognition omissions can still escape sparse-output checks. Hardware loss
and uncommitted callbacks cannot be reconstructed after device/power failure.
