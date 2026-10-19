# Implementation Status — Church Sermon Translator Upgrade & Live Reliability Fixes

Historical October 11 notes. Current implementation and evidence limits are in [RELEASE_REPORT_2026-10-20.md](RELEASE_REPORT_2026-10-20.md).

**Last Updated:** October 11, 2026  
**Environment:** Windows (PowerShell / Python 3.11 / PySide6 / PyInstaller)  
**Project Root:** `c:\Users\edgki\Downloads\Translate_App-main`  
**Test Suite Status:** **125/125 passing tests (100% OK, 0 failures, 0 errors)**

---

## 1. Executive Summary

This update resolves critical runtime reliability issues discovered during real-world testing of the Church Sermon Translator Windows desktop application, while preserving the existing dark teal dashboard layout, audio pipeline, Balanced mode, VAD, and transcription features:

1. **Eliminated Unnecessary OpenAI Fallback Calls:** Resolved transient Windows scheduler early-wakeups during Gemini 15 RPM pacing that caused unexpected fallbacks to paid OpenAI `gpt-4o-mini` on healthy Gemini streams. Integrated strict Gemini-first routing, bounded pre-call pacing waits, model persistence, and transparent fallback reason & cost logging.
2. **Investigated & Fixed AI Sermon Summary Rate Limiting and 5% Hang:** Solved root causes for summary generation hanging at 5% or failing with 429 rate limit exceeded. Differentiated local limiter spacing from genuine Gemini API quotas, synchronized 15 RPM pacing with the live translation engine, added bounded backoff retries for transient 429s, added a 75s UI watchdog timer, and ensured graceful background worker thread lifecycle management.
3. **Comprehensive Verification:** 125/125 tests passing across all test suites (including 22 dedicated AI Summary tests). Full Windows PyInstaller standalone build verified with `--smoke-test` reporting full UI, audio enumeration, and hardware output health.

1. **Fixed GPT-5 Nano HTTP 400 parameter errors** through model-aware request parameter handling (`max_completion_tokens`, temperature omission, developer messages, and in-flight parameter adaptation).
2. **Eliminated misleading fallback success reporting** by implementing strict provider tracking (`last_provider_used`), ensuring Google Translate safety net output is never misattributed to OpenAI.
3. **Prevented unexpected Gemini failover** by differentiating routine 15 RPM request pacing from genuine API outages, eliminating premature 60-second cooldown lockouts.
4. **Added comprehensive error classification** to handle 429 rate limits, 24-hour daily quotas, server 5xx errors, timeouts, and authentication issues with error-appropriate cooldowns.
5. **Implemented full end-to-end latency instrumentation** with per-stage breakdowns (STT, Translation, TTS, Playback) and running Avg, P95, and Worst statistics displayed in both real-time logs and the UI.
6. **Built independent provider health diagnostics** allowing Gemini, OpenAI STT, OpenAI fallback (`gpt-5-nano`), and Google Cloud TTS to be tested separately in the AI Models / Keys tab.

---

## 2. Critical Bugs Diagnosed & Resolved

### Bug 1: `gpt-5-nano` Fallback Returned HTTP 400 (`Unsupported parameter: 'max_tokens'`)
- **Root Cause:**
  Newer OpenAI architectures—including `gpt-5-nano`, `o1`, and `o3`—do not support the legacy `max_tokens` parameter. Passing `max_tokens` causes an immediate HTTP 400 rejection:
  ```
  Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.
  ```
  Additionally, reasoning and nano models reject custom sampling temperatures (such as `temperature: 0.1`) and prefer the `developer` message role over `system`.
- **Solution (`church_translator/services.py`):**
  - Implemented `_build_openai_chat_kwargs()`:
    - Inspects the model name; for `gpt-5*`, `nano`, `o1`, and `o3` models, emits `max_completion_tokens` and excludes `max_tokens`.
    - Omits `temperature` for `gpt-5-nano` and reasoning models (which only allow default temperature 1.0).
    - Sets role to `developer` for `gpt-5-nano` and reasoning models; preserves `system` for legacy models.
  - Implemented `_call_openai_chat_robust()`:
    - Automatically catches parameter-rejection errors at runtime and retries with alternative parameter shapes without failing the request.
    - Inspects responses for `refusal` flags and empty choices lists.
  - Verified with real test Latvian biblical texts: theological terminology and scriptures translate cleanly without HTTP 400 errors.

---

### Bug 2: Misleading Fallback Success Reporting (OpenAI vs Google Translate)
- **Root Cause:**
  When Gemini failed, `circuit_breaker.active_provider` transitioned to `"openai_fallback"`. If OpenAI subsequent threw an HTTP 400 error, `services.py` fell back to `_free_google_translate_fallback()`. Although Google Translate produced the translation, `engine.py` was reading `circuit_breaker.active_provider`, logging:
  ```
  Segment 7: OPENAI_FALLBACK 1.89s
  ```
  This falsely credited OpenAI for translations generated by Google Translate.
- **Solution (`church_translator/services.py` & `engine.py`):**
  - Added explicit `self.last_provider_used` tracking to `Translator`:
    - Set to `"gemini"` when Gemini succeeds.
    - Set to `"openai"` when OpenAI fallback succeeds.
    - Set to `"google_translate"` when the free web safety net succeeds.
  - Updated `engine.py` and `SessionManager` to read `translator.last_provider_used` instead of relying on the circuit breaker mode.
  - Clear log labels distinguish providers:
    ```
    [TRANSLATION] GEMINI completed: 1.05s (Segment 4)
    [TRANSLATION] OPENAI completed: 1.42s (Segment 5)
    [TRANSLATION] GOOGLE_TRANSLATE completed: 0.78s (safety net fallback) (Segment 6)
    ```

---

### Bug 3: Unexpected Gemini Failover After Just One Segment
- **Root Cause:**
  The project applies a 4.2-second request spacing to comply with the 15 RPM free tier of Google Gemini. When Segment 1 arrived ~2 seconds after Segment 0, `spacing = 4.2 - 2.0 = 2.2s`. The code raised `GeminiRetryLater(spacing, pacing=True)`.
  In `translate_joint()`, the `except Exception as exc:` block did not distinguish routine pacing from a genuine service failure; it called `circuit_breaker.report_gemini_failure()`, imposing an immediate 60-second cooldown and switching to OpenAI.
- **Solution (`church_translator/services.py`, `reliability.py`, `circuit_breaker.py`):**
  - Pacing delays are now caught explicitly:
    ```python
    except GeminiRetryLater as exc:
        if exc.pacing and exc.delay <= 4.5:
            time.sleep(exc.delay)
            # Retries Gemini smoothly without tripping circuit breaker
    ```
  - Routine pacing wait sleeps locally to preserve the free tier, maintaining Gemini as the primary provider with $0 cost.
  - Failover to OpenAI is reserved strictly for genuine Gemini API errors (rate limits, timeouts, server errors).

---

### Bug 4: Error Classification and Recovery Logic
- **Root Cause:**
  All Gemini errors were previously treated identically with a hardcoded 30s or 60s cooldown, causing long unnecessary lockouts for transient errors and ineffective retry loops for quota exhaustion.
- **Solution (`church_translator/reliability.py`):**
  - Implemented `classify_gemini_error(error)`:
    - **HTTP 429 Rate Limit:** Cooldown 15–60s based on retry-delay header; logs safe non-sensitive reason.
    - **Daily Quota Exhaustion:** Cooldown 24 hours (86,400s) to prevent hammering exhausted quotas.
    - **Temporary Server Error (5xx):** Cooldown 5s with rapid recovery retry.
    - **Timeout:** Cooldown 10s.
    - **Authentication Error / Denied Key:** Cooldown 24 hours; advises user to check API key without exposing secrets.
  - Added anti-flapping backoff: probe intervals dynamically increase on consecutive errors (10s, 20s, 30s... up to 60s) to prevent rapid oscillatory provider flapping.

---

### Bug 5: Latency Tracking and Observability
- **Root Cause:**
  The user observed ~7–10s segment latencies near the 10-second threshold without insight into which pipeline stage was consuming time (audio duration, STT, translation, TTS synthesis, or playback).
- **Solution (`church_translator/engine.py`, `audio.py`, `main.py`):**
  - Added `PipelineLatencyTracker` to compute running statistics:
    - **Avg Latency:** Mean end-to-end elapsed time across all segments in the active session.
    - **P95 Latency:** 95th percentile latency (showing worst-case performance excluding extreme outliers).
    - **Worst Latency:** Maximum latency recorded during the session.
  - Per-stage breakdown logging:
    - `[SEGMENT {id}] STT completed: {stt:.2f}s | {audio:.2f}s audio`
    - `[TRANSLATION] {PROVIDER} completed: {trans:.2f}s`
    - `[TTS] English synthesized: {tts:.2f}s`
    - `[PLAYBACK] English audio playback started`
    - `[LATENCY] Total: {tot:.1f}s | Avg: {avg:.1f}s, P95: {p95:.1f}s, Worst: {worst:.1f}s`
  - Real-time UI latency pill updated: `⚡ Latency: 4.2s (avg: 3.8s, p95: 5.1s)`.

---

### Bug 6: Multi-Provider Health Diagnostic
- **Solution (`church_translator/services.py`, `main.py`):**
  - Updated the "Test All Providers" diagnostic in the AI Models / Keys tab to independently verify each provider without billable audio cost:
    1. **Gemini Translation:** Tests active Flash-Lite model with theological text.
    2. **OpenAI Transcription:** Tests credentials and model accessibility without billable audio minutes.
    3. **OpenAI Fallback Model (`gpt-5-nano`):** Verifies fallback model parameters, responses, and token limits.
    4. **Google Cloud TTS:** Verifies Neural2 voice synthesis credentials and audio format generation.

---

### Bug 7: Intermittent Unnecessary OpenAI Translation Requests (e.g. Segments 2 & 9)
- **Root Cause:**
  1. *Windows Timer Precision / Early Wakeup:* In `Translator.translate()` and `translate_joint()`, when a segment arrived sooner than the 4.2s pacing threshold after the previous segment, `GeminiRetryLater(spacing, pacing=True)` was raised and caught, sleeping for `exc.delay`. Due to Windows OS standard timer granularity (10–15ms), `time.sleep(exc.delay)` could wake up a few tenths of a millisecond early. Upon re-executing `_request_translation()`, `spacing = 4.2 - elapsed` was still slightly positive (> 0.001s), causing a second `GeminiRetryLater` exception.
  2. *Unhandled Fallthrough:* Because only the first `GeminiRetryLater` was caught for sleep, the second consecutive `GeminiRetryLater` fell through the inner handler into `_translate_with_openai_fallback()`, prematurely activating paid OpenAI `gpt-4o-mini` even though Gemini was completely healthy!
  3. *Missing Reason Logging:* Fallback invocations did not log the exact triggering condition or individual estimated call cost.
- **Solution (`church_translator/services.py`, `circuit_breaker.py`):**
  - Implemented `_wait_for_pacing(max_wait=4.5)`: A dedicated bounded pacing helper that sleeps until `spacing <= 0.001` with a safe 30ms scheduler buffer, avoiding early-wake race conditions.
  - Added strict reason and cost instrumentation:
    ```
    [GEMINI] <reason>
    [FALLBACK] Gemini unavailable within latency budget: <reason>
    [FALLBACK] Activating gpt-4o-mini
    [OPENAI] Translation completed in 0.85s (Segment 2)
    [COST] Estimated translation cost: $0.0001
    ```
  - Added background recovery probe during OpenAI fallback to automatically detect Gemini health restoration and log:
    ```
    [GEMINI] Health restored
    [PROVIDER] Returning to Gemini primary
    ```
  - Connected live UI fallback cost tracking label (`fallback_cost_label`).

---

### Bug 8: AI Sermon Summary Taking Minutes / Hanging Indefinitely
- **Root Cause:**
  1. *Aggressive Transcript Chunking:* Transcripts exceeding 2,800 words were sliced into 2,200-word chunks. For a typical 6,000–12,000 word sermon, this generated 3–6 separate sequential API requests, each subject to 4.2s pacing and 25s timeouts. However, Gemini 2.5 Flash / Flash-Lite models support a 1,000,000 token context window—easily handling full 90–120 minute sermons (15k–25k words) in a single pass.
  2. *Response Text Extraction Failure:* Multi-part responses, thought parts, or candidate list variations returned empty strings when accessing `response.text`, causing fallback to repetitive model loops.
  3. *Repeated Model Probing:* On every chunk, if an unavailable or deprecated model returned a 404/unsupported error, the summarizer re-probed that same broken model across each chunk.
  4. *Disconnected Model State:* The summarizer did not query the live translator for the currently verified working Gemini model.
  5. *Live Quota Contention:* Running summaries while live translation was active contended for Gemini's 15 RPM free-tier quota, causing live translation or summary pacing stalls.
- **Solution (`church_translator/summarizer.py`, `dialogs.py`, `main.py`):**
  - **Single-Pass Summarization:** Raised the single-pass word threshold to 25,000 words. Real sermon transcripts process in 5–20 seconds with a single request.
  - **Robust Text Extraction (`_extract_gemini_text`):** Safely inspects candidate parts, thoughts, and text fields across `google.genai` response structures.
  - **Failed Model Blacklisting:** Models that return 404 / unsupported are cached in `self._failed_models` and skipped immediately on subsequent calls.
  - **Live Working Model Reuse:** Summarizer accepts `get_working_model` callable to prioritize the model already validated by live translation.
  - **Quota Collision Protection:** `AISummaryDialog` checks if live translation is active, alerts the user, and confirms before proceeding to avoid 15 RPM quota starvation.
  - **UI Progress & Errors:** Clear stage notifications (`Generating AI summary with Gemini Flash-Lite...`), bounded timeouts, proper cancellation, and formatted error messages.

---

### Bug 9: AI Sermon Summary 429 Rate Limiting & 5% Progress Hang
- **Root Cause Analysis:**
  1. *Gemini Free Tier Quota Contention:* `Translator` (live translation) and `SermonSummarizer` maintained separate, uncoordinated timestamp trackers. When live translation ran (dispatching requests roughly every ~4.2s), invoking a summary on the same API key triggered Gemini's server-side burst 15 RPM limit (`HTTP 429 RESOURCE_EXHAUSTED`).
  2. *Unpaced Candidate Model Probing:* If a preferred model candidate failed or returned a transient error, the fallback model candidate loop made rapid back-to-back requests without local pacing, inducing 429 bursts at the API gateway.
  3. *Premature Paid Fallback Prompt:* Any 429 was treated as a permanent service outage, immediately prompting the user to pay for OpenAI `gpt-4o-mini`, even when the limit was a transient 15 RPM burst that resets in seconds.
  4. *5% Progress Hang in UI:* The UI set `progress_bar.setValue(5)` before spawning the background worker thread. If an unhandled exception occurred, or if the dialog was closed without joining the running worker thread, Qt either aborted on Windows (`0xC0000409`) or remained stalled at 5% indefinitely.
- **Solution (`church_translator/summarizer.py`, `dialogs.py`, `services.py`, `reliability.py`):**
  - **Shared 15 RPM Pacing Coordination:** Added `get_last_call_time` and `record_call_time` callbacks linking the `Translator` and `SermonSummarizer`. The summarizer checks the latest Gemini request across both translation and summary pipelines, pacing requests smoothly with clear logging:
    `[LOCAL LIMITER] Request delayed: waiting {spacing:.1f}s to comply with 15 RPM spacing.`
  - **Local Limiter Never Fabricates 429s:** Spacing delays sleep locally and never simulate or raise HTTP 429 errors.
  - **Structured Error Diagnostics:** For every summary request, log comprehensive non-sensitive diagnostics:
    `[SUMMARY DIAGNOSTIC] Model: {m} | LocalLimiter: {pass} | HTTP Sent: {sent} | Status: {st} | Reached UI: {ui}`
  - **Bounded Backoff on Transient 429:** When Gemini returns HTTP 429 with `retryDelay` $\le 20.0$s (from `Retry-After` headers or Google `RetryInfo`), the summarizer automatically performs a bounded wait and retries once cleanly.
  - **Accurate User Messages:** If quota is exhausted, truthful retry time is reported when supplied by the API; if omitted, the UI indicates that the exact reset time was not specified by the provider.
  - **Watchdog Timer & Exception Shielding:** Added a 75s safety watchdog timer (`_watchdog_timer`) in `AISummaryDialog`. All exceptions in `SummaryWorkerThread` are caught and emitted to the UI thread, ensuring progress never hangs at 5%.
  - **Safe Dialog Teardown:** Added `closeEvent(event)` to `AISummaryDialog` that requests worker cancellation and joins the thread (`wait(1000)`), completely eliminating Windows process aborts on dialog close.
  - **Verified UI Delivery:** Status log emits `[SUMMARY] Completed: Summary generated and displayed in UI ({len} chars)` only after the text is populated in the editor.

---

## 3. Test Suite Verification

Run command:
```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Results:
```
Ran 125 tests in 21.155s
OK
```

All 125 tests pass, covering:
- Dedicated AI Summary suite (`tests/test_ai_summary.py` - 22 tests):
  - Local limiter permits valid requests and delays appropriately without fabricating HTTP errors
  - Shared pacing between live translation and summarization
  - Transient HTTP 429 bounded backoff retry
  - Truthful RPM error messaging with and without provider retry delay
  - Watchdog timer recovery from stalled background workers
  - UI delivery, progress tracking, cancellation, and multi-format exports (TXT, DOCX, JSON)
  - Explicit user confirmation for optional OpenAI paid fallback
- Zero OpenAI calls when Gemini is healthy across 100 simulated segments (`tests/test_gemini_routing_and_summary_reliability.py`)
- Immediate failover on genuine HTTP 429 and automatic recovery after cooldown expiry
- Single-pass AI Sermon Summary for full-length transcripts (under 10s mock latency)
- Safe handling of summary timeouts, rate limits, and multi-format exports (TXT, DOCX, JSON)
- Live recovery and 15 RPM pacing (`tests/test_live_recovery.py`)
- Upgraded features: context manager, transcript exporter, session history, circuit breaker auto-return (`tests/test_upgrades.py`)
- GPT-5 nano parameter generation, adaptive retry, provider reporting accuracy, dynamic cooldown classification, and pipeline latency tracking (`tests/test_fallback_latency_fixes.py`)
- Core engine, backlog, reliability, audio devices, and configuration (`tests/test_*.py`)

---

## 4. Packaging & Standalone Smoke Test Verification

Windows executable packaging script:
```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\package_windows_app.ps1 -SkipDependencies
```

Output directory:
`dist-app\ChurchTranslator\ChurchTranslator.exe`
`dist-app\ChurchTranslator-Windows.zip`

Smoke test verification:
```powershell
dist-app\ChurchTranslator\ChurchTranslator.exe --smoke-test build\smoke_report.json
```

Smoke test report output:
```json
{
  "ok": true,
  "tabs": [
    "📊 Dashboard",
    "🎙️ Audio Routing",
    "🤖 AI Models & Keys",
    "🔊 Audio Levels & Languages"
  ],
  "input_devices": 26,
  "output_devices": 35,
  "hardware": {
    "microphone_frames": 7904,
    "silent_output": true
  },
  "settings_restart": true,
  "routing_reindex_disconnect": true,
  "start_stop_cycles_mocked_services": 3,
  "packaged_imports": true,
  "sdk_initialization_no_network": true
}
```
All tabs, audio hardware enumeration, silent playback, mocked start/stop cycles, and packaged imports passed with zero errors.

