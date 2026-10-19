import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from church_translator.config import load_config
from church_translator.session_manager import TranscriptSegment, SessionRecord
from church_translator.summarizer import (
    SermonSummarizer,
    GeminiSummaryError,
    estimate_openai_summary_cost,
    StructuredSummary,
    is_json_response,
    get_adaptive_guidance,
)


class AISummaryTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.segments = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text="Šodien mēs runājam par Dieva neizmērojamo žēlastību un mīlestību.",
                english_translation="Today we speak of God's boundless grace and love.",
            ),
            TranscriptSegment(
                segment_id=2,
                captured_at=time.time() + 5,
                timestamp_str="10:00:05",
                latvian_text="Romiešiem 8:28 apliecina, ka tiem, kas mīl Dievu, visas lietas nāk par labu.",
                english_translation="Romans 8:28 affirms that all things work together for good to those who love God.",
            ),
            TranscriptSegment(
                segment_id=3,
                captured_at=time.time() + 10,
                timestamp_str="10:00:10",
                latvian_text="Lūgsim un uzticēsimies Kungam katrā mūsu dzīves solī. Āmen.",
                english_translation="Let us pray and trust the Lord in every step of our life. Amen.",
            ),
        ]

    def test_cost_estimator(self):
        cost = estimate_openai_summary_cost(self.segments, "gpt-4o-mini")
        self.assertGreater(cost, 0.0)
        self.assertLess(cost, 0.05)

    def test_single_pass_summary_success(self):
        summarizer = SermonSummarizer(self.config)
        expected_summary = (
            "1. Sermon Title: Dieva Žēlastība\n"
            "2. Main Message: Sprediķis par Dieva mīlestību.\n"
            "3. Bible Passages: Romiešiem 8:28\n"
            "4. Key Points: Uzticēšanās Dievam\n"
            "5. Examples: Pāvila vēstule\n"
            "6. Practical Applications: Ikdienas lūgšana\n"
            "7. Conclusions: Dieva uzticība\n"
            "8. Short Recap: Dievs vada savus bērnus."
        )

        with patch.object(summarizer, "_call_gemini", return_value=expected_summary) as mock_call:
            progress_stages = []
            def on_progress(stage, step, total):
                progress_stages.append(stage)

            res = summarizer.generate_summary(
                self.segments,
                language="lv",
                sermon_title="Dieva Žēlastība",
                provider="gemini",
                progress_cb=on_progress,
            )
            self.assertEqual(res, expected_summary)
            self.assertTrue(mock_call.called)
            self.assertIn("Preparing transcript", progress_stages)
            self.assertIn("Generating summary", progress_stages)
            self.assertIn("Processing response", progress_stages)

    def test_cancellation_during_generation(self):
        summarizer = SermonSummarizer(self.config)
        cancelled = True
        with self.assertRaises(InterruptedError):
            summarizer.generate_summary(
                self.segments,
                language="en",
                cancel_check=lambda: cancelled,
            )

    def test_gemini_429_rate_limit_classification(self):
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=Exception("429 Resource Exhausted: Rate limit exceeded")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments, provider="gemini")
            self.assertTrue(ctx.exception.is_quota_or_rate_limit)
            self.assertIn("15 RPM", str(ctx.exception))

    def test_gemini_quota_exhausted_classification(self):
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=Exception("Daily quota exceeded for models/gemini-3.5-flash-lite")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments, provider="gemini")
            self.assertTrue(ctx.exception.is_quota_or_rate_limit)
            self.assertIn("quota exhausted", str(ctx.exception).lower())

    def test_gemini_timeout_classification(self):
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=Exception("Request timed out after 25s")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments, provider="gemini")
            self.assertIn("timed out", str(ctx.exception).lower())

    def test_openai_fallback_provider(self):
        summarizer = SermonSummarizer(self.config)
        expected_openai_summary = "1. Sermon Title: Faith in God\n2. Main Message: Trusting Christ..."
        with patch.object(summarizer, "_call_openai", return_value=expected_openai_summary) as mock_openai:
            res = summarizer.generate_summary(self.segments, language="en", provider="openai")
            self.assertEqual(res, expected_openai_summary)
            self.assertTrue(mock_openai.called)

    def test_full_sermon_single_pass_avoids_unnecessary_chunking(self):
        """Full 90-120 minute sermons (~10,000 words) must avoid unnecessary chunking in Gemini 1M context."""
        summarizer = SermonSummarizer(self.config)
        # Create a 6,000 word transcript (typical 45-60 min sermon)
        sermon_text = " ".join(["Mācītājs sludina par ticību, cerību un mīlestību."] * 1000)
        segments = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text=sermon_text,
                english_translation="The pastor preaches on faith, hope and love.",
            )
        ]
        with patch.object(summarizer, "_call_gemini", return_value="1. Sermon Title: Faith\n2. Main Message: Faith brings hope.\n8. Short Recap: Love.") as mock_call:
            res = summarizer.generate_summary(segments, language="en", provider="gemini")
            self.assertIn("Faith", res)
            # Verified: Exactly 1 single-pass API call, no expensive chunking roundtrips!
            self.assertEqual(mock_call.call_count, 1)

    def test_hierarchical_chunking_for_long_sermons(self):
        summarizer = SermonSummarizer(self.config)
        summarizer.max_single_pass_words = 2500
        # Create a large segment with 3500 words
        long_text = " ".join(["Jēzus Kristus un Dieva mīlestība."] * 700)
        long_segments = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text=long_text,
                english_translation="Jesus Christ and God's love.",
            )
        ]
        
        intermediate_calls = []
        def fake_call_ai(prompt, **kwargs):
            intermediate_calls.append(prompt)
            if kwargs.get("is_intermediate"):
                return "Section key points: God's grace."
            return "1. Sermon Title: Long Sermon Unified Summary\n2. Main Message: Trust God.\n8. Short Recap: Trust God."

        with patch.object(summarizer, "_call_ai", side_effect=fake_call_ai):
            progress_stages = []
            res = summarizer.generate_summary(
                long_segments,
                language="lv",
                sermon_title="Garais Sprediķis",
                provider="gemini",
                progress_cb=lambda stage, s, t: progress_stages.append(stage),
            )
            self.assertIn("Long Sermon Unified Summary", res)
            self.assertGreaterEqual(len(intermediate_calls), 2)
            self.assertTrue(any("Processing section" in p for p in progress_stages))
            self.assertIn("Combining sections", progress_stages)

    def test_local_limiter_permits_valid_request_without_delay(self):
        """Local limiter permits valid summary request immediately when pacing interval is satisfied."""
        logs = []
        summarizer = SermonSummarizer(self.config, status_cb=logs.append)
        summarizer._last_call_time = time.monotonic() - 10.0  # 10s ago, well above 4.2s

        with patch.object(summarizer, "_call_gemini_single_model", return_value="Main Message:\nSummary content"):
            res = summarizer.generate_summary(self.segments, provider="gemini")
            self.assertEqual(res, "Main Message:\nSummary content")
            self.assertFalse(any("[LOCAL LIMITER] Request delayed" in log for log in logs))
            self.assertTrue(any("[SUMMARY DIAGNOSTIC]" in log and "LocalLimiter: Passed" in log for log in logs))

    def test_local_limiter_correctly_delays_and_does_not_fabricate_http_errors(self):
        """Local limiter delays request when called within 4.2s; does not fabricate HTTP 429 errors."""
        logs = []
        summarizer = SermonSummarizer(self.config, status_cb=logs.append)
        summarizer._last_call_time = time.monotonic() - 1.0  # 1s ago -> ~3.2s pacing delay

        with patch("time.sleep", return_value=None):  # fast mock sleep
            with patch.object(summarizer, "_call_gemini_single_model", return_value="Main Message:\nSummary content"):
                res = summarizer.generate_summary(self.segments, provider="gemini")
                self.assertEqual(res, "Main Message:\nSummary content")
                self.assertTrue(any("[LOCAL LIMITER] Request delayed" in log for log in logs))
                # Must NOT fabricate HTTP 429 error
                self.assertFalse(any("HTTP 429" in log for log in logs))
                self.assertTrue(any("LocalLimiter: Delayed" in log for log in logs))

    def test_shared_rate_limiter_coordination_with_translation(self):
        """Summarizer coordinates with Translator's last call time to avoid 15 RPM collision."""
        translator_last_time = time.monotonic() - 1.5
        logs = []
        summarizer = SermonSummarizer(
            self.config,
            status_cb=logs.append,
            get_last_call_time=lambda: translator_last_time,
            record_call_time=MagicMock(),
        )
        summarizer._last_call_time = 0.0  # Summarizer hasn't run yet, but Translator has

        with patch("time.sleep", return_value=None):
            with patch.object(summarizer, "_call_gemini_single_model", return_value="Main Message:\nSummary content"):
                res = summarizer.generate_summary(self.segments, provider="gemini")
                self.assertEqual(res, "Main Message:\nSummary content")
                # Checked Translator's clock and delayed ~2.7s
                self.assertTrue(any("[LOCAL LIMITER] Request delayed: waiting" in log for log in logs))

    def test_gemini_bounded_retry_on_transient_429(self):
        """Transient HTTP 429 with short retryDelay triggers bounded retry and succeeds."""
        logs = []
        summarizer = SermonSummarizer(self.config, status_cb=logs.append)
        
        # 1st call fails with 429 and retryDelay of 3s; 2nd call succeeds
        mock_exc = Exception("429 Resource Exhausted: retry in 3s")
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=[mock_exc, "Main Message:\nRecovered summary text"]):
            with patch("time.sleep", return_value=None):
                res = summarizer.generate_summary(self.segments, provider="gemini")
                self.assertEqual(res, "Main Message:\nRecovered summary text")
                self.assertTrue(any("[GEMINI API] Bounded retry: waiting 3.0s" in log for log in logs))
                self.assertTrue(any("Bounded retry succeeded" in log for log in logs))

    def test_gemini_rpm_with_retry_after_displays_exact_seconds(self):
        """When Gemini returns HTTP 429 with Retry-After header, error message states exact seconds."""
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=Exception("429 RESOURCE_EXHAUSTED: retry in 35s")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments, provider="gemini")
            self.assertIn("35 seconds", str(ctx.exception))
            self.assertEqual(ctx.exception.retry_delay, 35.0)
            self.assertEqual(ctx.exception.quota_dimension, "RPM")

    def test_gemini_rpm_without_delay_indicates_unknown_delay(self):
        """When Gemini returns HTTP 429 without delay info, error states exact reset time was not specified."""
        summarizer = SermonSummarizer(self.config)
        with patch.object(summarizer, "_call_gemini_single_model", side_effect=Exception("429 Resource Exhausted")):
            with self.assertRaises(GeminiSummaryError) as ctx:
                summarizer.generate_summary(self.segments, provider="gemini")
            self.assertIn("Exact provider retry delay not specified", str(ctx.exception))
            self.assertIsNone(ctx.exception.retry_delay)


class AISummaryDialogUITests(unittest.TestCase):
    """Rigorous UI integration tests for AISummaryDialog."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        import sys
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.dialogs = []
        self.config = load_config()
        self.summarizer = SermonSummarizer(self.config)
        self.session = SessionRecord(
            session_id="test-session-ui",
            created_at="2026-10-11 10:00:00",
            title="Dieva Žēlastības Sprediķis",
            duration_seconds=1800.0,
            state="completed",
            secondary_language="none",
            sermon_context_title="",
        )
        self.segments = [
            TranscriptSegment(
                segment_id=1,
                captured_at=time.time(),
                timestamp_str="10:00:00",
                latvian_text="Šodien mēs runājam par Dieva neizmērojamo žēlastību un mīlestību.",
                english_translation="Today we speak of God's boundless grace and love.",
            ),
            TranscriptSegment(
                segment_id=2,
                captured_at=time.time() + 5,
                timestamp_str="10:00:05",
                latvian_text="Romiešiem 8:28 apliecina, ka tiem, kas mīl Dievu, visas lietas nāk par labu.",
                english_translation="Romans 8:28 affirms that all things work together for good.",
            ),
        ]

    def tearDown(self):
        for dlg in self.dialogs:
            try:
                if hasattr(dlg, "_worker_thread") and dlg._worker_thread:
                    dlg._worker_thread.cancel()
                    dlg._worker_thread.wait(500)
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self.dialogs.clear()
        self.app.processEvents()

    def test_ui_generate_summary_latvian_and_english_workflow(self):
        from church_translator.dialogs import AISummaryDialog

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()
        # 1. Verify default initial state
        self.assertFalse(dlg._is_generating)
        self.assertFalse(dlg.copy_btn.isEnabled())
        self.assertFalse(dlg.download_btn.isEnabled())
        self.assertTrue(dlg.generate_btn.isEnabled())
        self.assertFalse(dlg.cancel_btn.isVisible())

        # 2. Test Latvian Generation
        dlg.lang_combo.setCurrentIndex(1)  # lv
        self.assertEqual(dlg.lang_combo.currentData(), "lv")

        expected_lv_summary = (
            "1. Sermon Title: Dieva Žēlastība\n"
            "2. Main Message: Sprediķis par mīlestību.\n"
            "3. Bible Passages: Romiešiem 8:28\n"
            "4. Key Points: Uzticēšanās\n"
            "5. Examples: Pāvils\n"
            "6. Practical Applications: Lūgšana\n"
            "7. Conclusions: Cerība\n"
            "8. Short Recap: Dievs mīl mūs."
        )

        progress_events = []
        def fake_generate(segs, language="en", **kwargs):
            pcb = kwargs.get("progress_cb")
            if pcb:
                pcb("Preparing transcript", 1, 4)
                pcb("Connecting to Gemini", 2, 4)
                pcb("Generating summary", 3, 4)
                pcb("Processing response", 4, 4)
            return expected_lv_summary

        with patch.object(self.summarizer, "generate_summary", side_effect=fake_generate):
            dlg._generate_summary(provider="gemini")
            self.assertTrue(dlg._is_generating)
            self.assertFalse(dlg.generate_btn.isEnabled())
            self.assertTrue(dlg.cancel_btn.isVisible())

            # Spin event loop to allow QThread signals to deliver to UI thread
            for _ in range(50):
                self.app.processEvents()
                time.sleep(0.01)

            # Verify successful display
            self.assertFalse(dlg._is_generating)
            self.assertEqual(dlg.progress_bar.value(), 100)
            self.assertIn("Dieva Žēlastība", dlg.summary_text.toPlainText())
            self.assertTrue(dlg.copy_btn.isEnabled())
            self.assertTrue(dlg.download_btn.isEnabled())
            self.assertTrue(dlg.generate_btn.isEnabled())
            self.assertFalse(dlg.cancel_btn.isVisible())

        # 3. Test English Generation
        dlg.lang_combo.setCurrentIndex(0)  # en
        self.assertEqual(dlg.lang_combo.currentData(), "en")
        expected_en_summary = "1. Sermon Title: God's Grace\n2. Main Message: Faith and salvation."

        with patch.object(self.summarizer, "generate_summary", return_value=expected_en_summary):
            dlg._generate_summary(provider="gemini")
            for _ in range(50):
                self.app.processEvents()
                time.sleep(0.01)

            self.assertEqual(dlg.progress_bar.value(), 100)
            self.assertIn("God's Grace", dlg.summary_text.toPlainText())
            self.assertTrue(dlg.copy_btn.isEnabled())
            self.assertTrue(dlg.download_btn.isEnabled())

    def test_ui_cancellation(self):
        from church_translator.dialogs import AISummaryDialog

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        def slow_generate(segs, **kwargs):
            cancel_check = kwargs.get("cancel_check")
            for _ in range(100):
                if cancel_check and cancel_check():
                    raise InterruptedError("Cancelled")
                time.sleep(0.01)
            return "Should not finish"

        with patch.object(self.summarizer, "generate_summary", side_effect=slow_generate):
            dlg._generate_summary(provider="gemini")
            self.app.processEvents()
            # Click cancel
            dlg._cancel_generation()
            for _ in range(50):
                self.app.processEvents()
                time.sleep(0.01)

            self.assertFalse(dlg._is_generating)
            self.assertTrue(dlg.generate_btn.isEnabled())
            self.assertFalse(dlg.cancel_btn.isVisible())
            self.assertIn("cancelled", dlg.status_label.text().lower())

    def test_ui_gemini_error_and_retry(self):
        from church_translator.dialogs import AISummaryDialog
        from PySide6.QtWidgets import QMessageBox

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        with patch.object(self.summarizer, "generate_summary", side_effect=GeminiSummaryError("429 rate limit exceeded", is_quota_or_rate_limit=True)):
            with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok), \
                 patch.object(QMessageBox, "question", return_value=QMessageBox.No):
                dlg._generate_summary(provider="gemini")
                for _ in range(50):
                    self.app.processEvents()
                    time.sleep(0.01)

                self.assertFalse(dlg._is_generating)
                self.assertTrue(dlg.generate_btn.isEnabled())
                self.assertTrue(dlg.generate_btn.isEnabled())
                self.assertIn("Gemini Notice", dlg.status_label.text())

    def test_ui_openai_fallback_confirmation_yes_and_no(self):
        """OpenAI fallback is offered only with explicit confirmation; declining preserves transcript with zero cost."""
        from dataclasses import replace
        from church_translator.dialogs import AISummaryDialog
        from PySide6.QtWidgets import QMessageBox

        # Ensure OpenAI key is present on config via replace for frozen AppConfig
        cfg = replace(self.config, openai_api_key="test-sk-valid")
        summarizer = SermonSummarizer(cfg)
        dlg = AISummaryDialog(summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        # Case 1: User declines OpenAI fallback (No)
        openai_calls = []
        def fake_openai(segs, **kwargs):
            openai_calls.append(kwargs)
            return "Should not be called"

        with patch.object(summarizer, "generate_summary", side_effect=GeminiSummaryError("429 rate limit exceeded", is_quota_or_rate_limit=True)), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.No) as mock_q, \
             patch.object(summarizer, "_call_openai", side_effect=fake_openai):
            dlg._generate_summary(provider="gemini")
            for _ in range(50):
                self.app.processEvents()
                time.sleep(0.01)

            self.assertTrue(mock_q.called)
            # Question displayed estimated cost
            question_text = mock_q.call_args[0][2]
            self.assertIn("Estimated cost: ~$", question_text)
            self.assertIn("gpt-4o-mini", question_text)

            # OpenAI was NOT called
            self.assertEqual(len(openai_calls), 0)
            self.assertFalse(dlg._is_generating)
            self.assertTrue(dlg.generate_btn.isEnabled())
            self.assertTrue(dlg.generate_btn.isEnabled())

        # Case 2: User confirms OpenAI fallback (Yes)
        with patch.object(summarizer, "generate_summary") as mock_gen, \
             patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            def fake_gen_provider(segs, provider="gemini", **kwargs):
                if provider == "gemini":
                    raise GeminiSummaryError("429 rate limit exceeded", is_quota_or_rate_limit=True)
                return "1. Sermon Title: OpenAI Generated Summary\n8. Short Recap: God is good."

            mock_gen.side_effect = fake_gen_provider
            dlg._generate_summary(provider="gemini")
            for _ in range(50):
                self.app.processEvents()
                time.sleep(0.01)

            self.assertFalse(dlg._is_generating)
            self.assertEqual(dlg.progress_bar.value(), 100)
            self.assertIn("OpenAI Generated Summary", dlg.summary_text.toPlainText())


    def test_ui_empty_response_handling(self):
        from church_translator.dialogs import AISummaryDialog

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        with patch.object(self.summarizer, "generate_summary", return_value="   "):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_crit:
                dlg._generate_summary(provider="gemini")
                for _ in range(50):
                    self.app.processEvents()
                    time.sleep(0.01)

                self.assertFalse(dlg._is_generating)
                self.assertTrue(dlg.generate_btn.isEnabled())
                self.assertTrue(mock_crit.called)

    def test_ui_watchdog_prevents_indefinite_hang_at_five_percent(self):
        """Watchdog resets UI and prevents indefinite hang at 5% if worker thread stalls."""
        from church_translator.dialogs import AISummaryDialog
        from PySide6.QtWidgets import QMessageBox

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        def hanging_generate(segs, **kwargs):
            time.sleep(.15)  # Simulate bounded in-flight request
            return "Late"

        with patch.object(self.summarizer, "generate_summary", side_effect=hanging_generate):
            with patch.object(QMessageBox, "warning"):
                dlg._generate_summary(provider="gemini")
                self.assertTrue(dlg._is_generating)
                self.assertEqual(dlg.progress_bar.value(), 5)

                # Trigger watchdog timeout manually to avoid real 75s delay
                dlg._on_worker_timeout_watchdog()
                self.app.processEvents()

                # Keep duplicate requests disabled until the worker acknowledges cancellation.
                self.assertTrue(dlg._is_generating)
                self.assertFalse(dlg.generate_btn.isEnabled())
                self.assertIn("timed out", dlg.status_label.text())
                dlg._generate_summary()
                dlg._worker_thread.wait(1000)
                self.app.processEvents()
                self.assertFalse(dlg._is_generating)
                self.assertTrue(dlg.generate_btn.isEnabled())


    def test_summary_exports_all_formats(self):
        """Verify summary exports correctly to TXT, Markdown, Word DOCX, and PDF."""
        import tempfile
        from church_translator.summarizer import SermonSummarizer

        title = "Grace and Peace"
        text = "1. Sermon Title: Grace and Peace\n2. Main Message: God loves everyone."

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            txt_path = tmp / "summary.txt"
            md_path = tmp / "summary.md"
            docx_path = tmp / "summary.docx"
            pdf_path = tmp / "summary.pdf"

            SermonSummarizer.export_summary_to_txt(txt_path, title, text)
            self.assertTrue(txt_path.exists())
            self.assertIn("Grace and Peace", txt_path.read_text(encoding="utf-8"))

            SermonSummarizer.export_summary_to_markdown(md_path, title, text)
            self.assertTrue(md_path.exists())
            self.assertIn("# Sermon Summary", md_path.read_text(encoding="utf-8"))

            SermonSummarizer.export_summary_to_docx(docx_path, title, text)
            self.assertTrue(docx_path.exists())
            self.assertGreater(docx_path.stat().st_size, 1000)

            SermonSummarizer.export_summary_to_pdf(pdf_path, title, text)
            self.assertTrue(pdf_path.exists())
            self.assertGreater(pdf_path.stat().st_size, 1000)

    def test_adaptive_guidance_tiers(self):
        """Adaptive guidance produces correct target word counts based on transcript size."""
        g_tiny = get_adaptive_guidance(150)
        self.assertIn("30–60 words", g_tiny)

        g_short = get_adaptive_guidance(600)
        self.assertIn("60–120 words", g_short)

        g_medium = get_adaptive_guidance(2500)
        self.assertIn("120–200 words", g_medium)

        g_long = get_adaptive_guidance(6500)
        self.assertIn("200–350 words", g_long)

    def test_summary_always_substantially_shorter_than_transcript(self):
        """Concise structured summaries are significantly shorter than source transcripts."""
        summarizer = SermonSummarizer(self.config)
        # 150-word transcript
        transcript_150 = " ".join(["Šodien runājam par mīlestību un mieru kristīgajā dzīvē."] * 19)
        segs_150 = [TranscriptSegment(1, time.time(), "10:00:00", transcript_150, "English text")]

        json_summary_150 = '''{
            "title": "Mīlestība un Miers",
            "main_message": "Kristus aicina mūs mīlēt vienam otru un saglabāt mieru sirdī.",
            "key_teachings": [
                "Mīlestība ir kristīgās dzīves pamats.",
                "Dieva miers pārspēj visu saprašanu."
            ],
            "bible_references": [],
            "practical_takeaway": "Rīkojieties mīlestībā pret savu tuvāko katru dienu."
        }'''

        with patch.object(summarizer, "_call_gemini", return_value=json_summary_150):
            res = summarizer.generate_summary(segs_150, language="lv")
            words_in_summary = len(res.split())
            words_in_transcript = len(transcript_150.split())
            # Summary (around 30-50 words) must be substantially shorter than transcript (150 words)
            self.assertLess(words_in_summary, words_in_transcript * 0.5)
            self.assertGreater(words_in_summary, 20)
            self.assertIn("Mīlestība un Miers", res)
            self.assertNotIn("Bible References:", res)  # Omitted when empty

    def test_missing_bible_references_omitted_completely(self):
        """When no Bible references were mentioned, section is omitted without inventing citations."""
        struct = StructuredSummary(
            title="Faith and Daily Walk",
            main_message="We walk by faith and not by sight in all our endeavors.",
            key_teachings=["True faith perseveres.", "God sustains the faithful."],
            bible_references=[],  # Explicitly empty
            practical_takeaway="Trust God during everyday decisions.",
        )
        clean = struct.to_clean_text()
        self.assertNotIn("Bible References", clean)
        self.assertIn("Faith and Daily Walk", clean)
        self.assertIn("Main Message:", clean)
        self.assertIn("Key Teachings:", clean)
        self.assertIn("Practical Takeaway:", clean)

    def test_dashboard_in_place_editing_and_sync(self):
        """Editing cards in dashboard view immediately synchronizes to clean ordinary text."""
        from church_translator.dialogs import AISummaryDialog

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        # Set summary
        json_sum = '''{
            "title": "Initial Title",
            "main_message": "Original main message teaching.",
            "key_teachings": ["Point one.", "Point two."],
            "bible_references": ["John 3:16"],
            "practical_takeaway": "Original takeaway."
        }'''
        dlg._on_summary_success(json_sum, "en")

        # Edit title in card
        dlg.card_title_edit.setText("Updated Custom Sermon Title")
        # Edit main message in card
        dlg.card_main_msg.setPlainText("Edited message by user.")
        # Edit first teaching point
        dlg._teaching_edits[0].setText("Custom edited first point.")

        clean_text = dlg.get_current_summary_text()
        self.assertIn("Updated Custom Sermon Title", clean_text)
        self.assertIn("Edited message by user.", clean_text)
        self.assertIn("1. Custom edited first point.", clean_text)

    def test_raw_text_editing_updates_dashboard_cards(self):
        """Editing in raw text mode updates the dashboard cards seamlessly."""
        from church_translator.dialogs import AISummaryDialog

        dlg = AISummaryDialog(self.summarizer, self.session, self.segments)
        self.dialogs.append(dlg)
        dlg.show()

        raw_input = (
            "NEW TITLE FROM RAW MODE\n\n"
            "Main Message:\n"
            "Raw mode message teaching.\n\n"
            "Key Teachings:\n"
            "1. First point from raw.\n"
            "2. Second point from raw.\n\n"
            "Bible References:\n"
            "Romans 12:2\n\n"
            "Practical Takeaway:\n"
            "Be transformed by renewing your mind."
        )

        dlg._switch_to_raw_view()
        dlg.summary_text.setPlainText(raw_input)
        dlg._switch_to_dashboard_view()

        self.assertEqual(dlg.card_title_edit.text(), "NEW TITLE FROM RAW MODE")
        self.assertEqual(dlg.card_main_msg.toPlainText(), "Raw mode message teaching.")
        self.assertNotIn('Bible References:', dlg.get_current_summary_text())
        self.assertFalse(hasattr(dlg, 'add_bible_btn'))
        self.assertEqual(set(dlg.section_cards), {'main_message', 'key_teachings', 'highlights'})

    def test_downloaded_txt_summary_clean_ordinary_format(self):
        """Downloaded TXT summary uses clean ordinary text matching Section 6 without UI cards or borders."""
        import tempfile
        from church_translator.summarizer import SermonSummarizer

        title = "Living a Life That Bears Fruit"
        struct = StructuredSummary(
            title=title,
            main_message="Our faith should be visible through the way we live.",
            key_teachings=[
                "True faith is reflected in our actions.",
                "Spiritual growth should produce good fruit.",
                "Our lives should bring glory to God.",
            ],
            bible_references=["John 15:1-8"],
            practical_takeaway="Examine whether your everyday actions reflect the faith you profess.",
        )
        clean_text = struct.to_clean_text()

        with tempfile.TemporaryDirectory() as tmpdir:
            out_txt = Path(tmpdir) / "summary.txt"
            SermonSummarizer.export_summary_to_txt(out_txt, title, clean_text)

            content = out_txt.read_text(encoding="utf-8")
            # Verify clean layout matching Section 6
            self.assertIn("Living a Life That Bears Fruit", content)
            self.assertIn("Main Message:\nOur faith should be visible", content)
            self.assertIn("Key Teachings:\n1. True faith is reflected", content)
            self.assertIn("Bible References:\nJohn 15:1-8", content)
            self.assertIn("Practical Takeaway:\nExamine whether your", content)
            # Must NOT contain UI borders or HTML tags or cards
            self.assertNotIn("<div>", content)
            self.assertNotIn("border:", content)
            self.assertNotIn("====", content)


if __name__ == "__main__":
    unittest.main()


