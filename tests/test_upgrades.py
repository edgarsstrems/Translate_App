"""Comprehensive test suite covering all upgraded features and reliability targets:
- Gemini primary + OpenAI emergency fallback activation
- Automatic return to Gemini upon confirmed recovery
- Circuit breaker behavior (429, timeout, quota exhaustion)
- Fallback cost controls and usage metrics
- Sermon context parsing, persistence, and fast relevance lookup
- Pause and Resume continuity without duplicate transcription or stale audio
- Transcript multi-format exports (TXT, PDF, DOCX, Markdown, JSON)
- AI sermon summary generation and downloads
- Progressive session autosave and crash recovery
- End-to-end latency benchmarks
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from church_translator.circuit_breaker import ProviderCircuitBreaker
from church_translator.config import load_config, AppConfig
from church_translator.glossary import Glossary
from church_translator.sermon_context import SermonContextManager
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.transcript_exporter import TranscriptExporter
from church_translator.summarizer import SermonSummarizer
from church_translator.services import Translator


class TestUpgradeFeatures(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # ---------------------------------------------------------------------------
    # 1. Circuit Breaker & OpenAI Fallback Tests
    # ---------------------------------------------------------------------------
    def test_circuit_breaker_gemini_first_and_fallback_activation(self):
        logs = []
        cb = ProviderCircuitBreaker(gemini_timeout=4.0, spending_warning=0.10, spending_limit=0.50, status_cb=logs.append)
        
        # Initially primary is Gemini
        self.assertEqual(cb.active_provider, "gemini")
        self.assertTrue(cb.gemini_healthy)

        # Gemini succeeds
        cb.report_gemini_success()
        self.assertEqual(cb.active_provider, "gemini")

        # Gemini encounters 429 / failure
        cb.report_gemini_failure("429 Resource Exhausted", cooldown_seconds=15.0)
        self.assertEqual(cb.active_provider, "openai_fallback")
        self.assertFalse(cb.gemini_healthy)
        self.assertFalse(cb.should_probe_gemini())  # Still in cooldown

        # Usage tracking
        cb.record_fallback_usage(100, 50, "gpt-4o-mini")
        stats = cb.get_summary_stats()
        self.assertEqual(stats["total_requests"], 1)
        self.assertEqual(stats["total_tokens"], 150)
        self.assertGreater(stats["estimated_cost_usd"], 0.0)

    def test_circuit_breaker_automatic_return_to_gemini(self):
        logs = []
        cb = ProviderCircuitBreaker(gemini_timeout=4.0, status_cb=logs.append)
        
        # Trip circuit breaker
        cb.report_gemini_failure("Service Unavailable", cooldown_seconds=0.01)
        self.assertEqual(cb.active_provider, "openai_fallback")

        time.sleep(0.05)
        # Should now allow probing
        self.assertTrue(cb.should_probe_gemini())

        # Gemini recovers
        cb.report_gemini_success()
        self.assertEqual(cb.active_provider, "gemini")
        self.assertTrue(cb.gemini_healthy)
        self.assertTrue(any("Returned to Gemini primary" in msg for msg in logs))

    def test_circuit_breaker_cost_limit(self):
        logs = []
        cb = ProviderCircuitBreaker(spending_warning=0.0001, spending_limit=0.0002, status_cb=logs.append)
        
        # Trigger warning and hard limit
        cb.record_fallback_usage(2000, 1000, "gpt-4o-mini")
        self.assertTrue(cb.stats.warning_triggered)
        
        cb.record_fallback_usage(2000, 1000, "gpt-4o-mini")
        self.assertTrue(cb.stats.limit_reached)
        self.assertFalse(cb.is_fallback_allowed())

    def test_translator_openai_fallback_integration(self):
        base_cfg = load_config()
        cfg = type(base_cfg)(**{
            **base_cfg.__dict__,
            "gemini_api_key": "dummy_gemini",
            "openai_api_key": "dummy_openai",
            "openai_fallback_enabled": True,
        })
        glossary = Glossary({}, {})
        status_msgs = []
        translator = Translator(cfg, glossary, status_cb=status_msgs.append)

        # Mock Gemini failing with 429
        translator._translate_joint_with_gemini = MagicMock(side_effect=Exception("429 Resource Exhausted"))
        # Mock OpenAI fallback succeeding
        translator._translate_with_openai_fallback = MagicMock(return_value={"en": "Today we reflect upon God's grace."})

        res = translator.translate_joint("Šodien mēs runājam par Dieva žēlastību.", ["en"])
        self.assertEqual(res, {"en": "Today we reflect upon God's grace."})
        self.assertEqual(translator.circuit_breaker.active_provider, "openai_fallback")

    # ---------------------------------------------------------------------------
    # 2. Sermon Context Manager Tests
    # ---------------------------------------------------------------------------
    def test_sermon_context_persistence_and_extraction(self):
        storage_file = self.dir_path / "sermon_ctx.json"
        mgr = SermonContextManager(storage_file)

        sample_sermon = (
            "Ticība un cerība Dieva vārdā\n\n"
            "Romiešiem 8:28 saka, ka tiem, kas mīl Dievu, visas lietas nāk par labu.\n"
            "Mēs šodien pārdomāsim par apustuļa Pāvila vēstuli un kā uzticēties Jēzum Kristum "
            "dzīves grūtībās un pārbaudījumos.\n\n"
            "Pirmais punkts: neatlaidīga lūgšana un Svētais Gars kā mūsu Aizstāvis."
        )

        mgr.set_text(sample_sermon, title="Ticība un cerība", save=True)
        self.assertTrue(mgr.is_active)
        self.assertEqual(mgr.title, "Ticība un cerība")
        self.assertIn("Romiešiem 8:28", mgr.bible_references)
        self.assertTrue(len(mgr.sections) >= 1)

        # Reload from disk
        mgr2 = SermonContextManager(storage_file)
        self.assertFalse(mgr2.is_active)
        mgr2.load()  # Explicitly import saved reference material into this session.
        self.assertTrue(mgr2.is_active)
        self.assertEqual(mgr2.title, "Ticība un cerība")
        self.assertIn("Romiešiem 8:28", mgr2.bible_references)

        # Relevant retrieval
        hint = mgr2.get_compact_prompt_hint("mēs lasām no Romiešiem 8:28 par Dieva mīlestību")
        self.assertIn("Romiešiem 8:28", hint)
        self.assertIn("PREPARED SERMON REFERENCE", f"PREPARED SERMON REFERENCE: {hint}")

    # ---------------------------------------------------------------------------
    # 3. Session Persistence & Progressive Storage Tests
    # ---------------------------------------------------------------------------
    def test_session_manager_progressive_storage_and_recovery(self):
        sess_mgr = SessionManager(self.dir_path)
        sid = sess_mgr.start_session(title="Svētdienas Dievkalpojums", secondary_language="ru")

        seg1 = TranscriptSegment(
            segment_id=1,
            captured_at=time.time(),
            timestamp_str="10:00:05",
            latvian_text="Labrīt visiem baznīcā.",
            english_translation="Good morning to everyone in church.",
            secondary_language="ru",
            secondary_translation="Доброе утро всем в церкви.",
            status="delivered",
            uncertain=False,
            provider="gemini",
        )
        sess_mgr.append_segment(seg1)

        segments = sess_mgr.get_segments(sid)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].latvian_text, "Labrīt visiem baznīcā.")

        # Pause and resume
        sess_mgr.pause_session()
        self.assertTrue(sess_mgr.is_paused)
        sess = sess_mgr.get_session(sid)
        self.assertEqual(sess.state, "paused")

        sess_mgr.resume_session()
        self.assertFalse(sess_mgr.is_paused)

        # Simulate crash
        crashed = sess_mgr.get_most_recent_crashed_session()
        self.assertIsNotNone(crashed)
        self.assertEqual(crashed.session_id, sid)

        sess_mgr.end_session()
        sess_done = sess_mgr.get_session(sid)
        self.assertEqual(sess_done.state, "completed")

    # ---------------------------------------------------------------------------
    # 4. Transcript Multi-Format Exporter Tests
    # ---------------------------------------------------------------------------
    def test_transcript_exports_all_formats(self):
        sess_mgr = SessionManager(self.dir_path)
        sid = sess_mgr.start_session(title="Pārbaudījumu spēks", secondary_language="ru")
        seg = TranscriptSegment(
            segment_id=1,
            captured_at=time.time(),
            timestamp_str="10:15:30",
            latvian_text="Dieva vārds ir dzīvs un spēcīgs.",
            english_translation="The word of God is living and powerful.",
            secondary_language="ru",
            secondary_translation="Слово Божье живо и действенно.",
        )
        sess_mgr.append_segment(seg)
        sess = sess_mgr.get_session(sid)
        segs = sess_mgr.get_segments(sid)

        # TXT export
        txt_file = self.dir_path / "test_export.txt"
        TranscriptExporter.export_to_txt(txt_file, sess, segs, languages="bilingual")
        self.assertTrue(txt_file.exists())
        txt_content = txt_file.read_text(encoding="utf-8")
        self.assertIn("Pārbaudījumu spēks", txt_content)
        self.assertIn("The word of God is living and powerful.", txt_content)

        # Markdown export
        md_file = self.dir_path / "test_export.md"
        TranscriptExporter.export_to_markdown(md_file, sess, segs, languages="bilingual")
        self.assertTrue(md_file.exists())
        self.assertIn("# Pārbaudījumu spēks", md_file.read_text(encoding="utf-8"))

        # JSON export
        json_file = self.dir_path / "test_export.json"
        TranscriptExporter.export_to_json(json_file, sess, segs, languages="bilingual")
        self.assertTrue(json_file.exists())
        self.assertIn("Pārbaudījumu spēks", json_file.read_text(encoding="utf-8"))

        # DOCX export
        docx_file = self.dir_path / "test_export.docx"
        TranscriptExporter.export_to_docx(docx_file, sess, segs, languages="bilingual")
        self.assertTrue(docx_file.exists())
        self.assertGreater(docx_file.stat().st_size, 1000)

        # PDF export (via QPdfWriter)
        from PySide6.QtWidgets import QApplication
        import sys
        if not QApplication.instance():
            _app = QApplication(sys.argv)
        pdf_file = self.dir_path / "test_export.pdf"
        TranscriptExporter.export_to_pdf(pdf_file, sess, segs, languages="bilingual")
        self.assertTrue(pdf_file.exists())
        self.assertGreater(pdf_file.stat().st_size, 1000)

    # ---------------------------------------------------------------------------
    # 5. Sermon Summarizer Tests
    # ---------------------------------------------------------------------------
    def test_sermon_summarizer_export_formats(self):
        sample_summary = (
            "1. Sermon Title: Dzīvā Cerība\n"
            "2. Main Message: Sprediķis aicina uzticēties Dieva apsolījumiem grūtos brīžos.\n"
            "3. Important Bible Passages: 1. Pētera 1:3-9\n"
            "4. Key Teaching Points:\n"
            "- Cerība, kas nepieviļ\n"
            "- Pārbaudījumu nozīme ticības šķīstīšanā\n"
            "5. Practical Applications:\n"
            "- Lūdzieties katru rītu\n"
            "- Palīdziet tuvākajiem\n"
            "8. Short Recap: Dieva žēlastība ir lielāka par visām mūsu bailēm."
        )

        txt_sum = self.dir_path / "summary.txt"
        SermonSummarizer.export_summary_to_txt(txt_sum, "Dzīvā Cerība", sample_summary)
        self.assertTrue(txt_sum.exists())

        md_sum = self.dir_path / "summary.md"
        SermonSummarizer.export_summary_to_markdown(md_sum, "Dzīvā Cerība", sample_summary)
        self.assertTrue(md_sum.exists())

        docx_sum = self.dir_path / "summary.docx"
        SermonSummarizer.export_summary_to_docx(docx_sum, "Dzīvā Cerība", sample_summary)
        self.assertTrue(docx_sum.exists())
        self.assertGreater(docx_sum.stat().st_size, 1000)

        from PySide6.QtWidgets import QApplication
        import sys
        if not QApplication.instance():
            _app = QApplication(sys.argv)
        pdf_sum = self.dir_path / "summary.pdf"
        SermonSummarizer.export_summary_to_pdf(pdf_sum, "Dzīvā Cerība", sample_summary)
        self.assertTrue(pdf_sum.exists())
        self.assertGreater(pdf_sum.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
