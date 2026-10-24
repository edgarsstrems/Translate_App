"""October 18 regressions. Real queues, SQLite and parsers; providers are mocked."""
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import PipelineLatencyTracker, TranslationEngine
from church_translator.glossary import Glossary
from church_translator.reliability import GeminiRetryLater
from church_translator.sermon_context import SermonContextManager
from church_translator.services import Translator
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.transcript_exporter import TranscriptExporter
from church_translator.translation_quality import InvalidTranslationOutput, translation_issue
from scripts.evaluate_sermon import parse_pages, corpus_statistics


class SermonAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = replace(load_config(), gemini_api_key='test', openai_api_key='test',
                              openai_fallback_enabled=False, auto_return_to_gemini=False)

    def translator(self, **settings):
        with patch.object(Translator, '_init_gemini_client'):
            service = Translator(replace(self.config, **settings), Glossary(), status_cb=MagicMock())
        service._wait_for_pacing = MagicMock()
        return service

    def test_fixed_sermon_dataset_and_manual_references(self):
        data = json.loads((Path(__file__).parents[1] / 'verification/translation-quality-fixtures.json').read_text(encoding='utf-8'))
        for case in data['cases']:
            with self.subTest(case=case['id']):
                output = case.get('candidate_en', case.get('historical_en', ''))
                self.assertEqual(translation_issue(case['source_lv'], output, 'en'), case['expected_issue'])
                if 'reference_en' in case:
                    self.assertIsNone(translation_issue(case['source_lv'], case['reference_en'], 'en'))
                    self.assertIn('exported', case['reference_scope'])

    def test_single_echo_retries_once_then_remembers_only_valid_text(self):
        service = self.translator()
        source = 'Mēs šorīt rūpējamies par savu ķermeni.'
        service._call_gemini_api = MagicMock(side_effect=[source, 'This morning we care for our body.'])
        self.assertEqual(service.translate(source, 'en'), 'This morning we care for our body.')
        self.assertEqual(service._call_gemini_api.call_count, 2)
        self.assertEqual(len(service._history), 1)
        self.assertNotIn(source, ' '.join(str(c) for c in service.status_cb.call_args_list))
        service._wait_for_pacing.assert_called_once()

    def test_repeated_invalid_response_retains_pending_without_paid_fallback(self):
        service = self.translator()
        service._call_gemini_api = MagicMock(return_value='Mēs šorīt rūpējamies par savu ķermeni.')
        service._translate_with_openai_fallback = MagicMock()
        with self.assertRaises(GeminiRetryLater):
            service.translate('Mēs šorīt rūpējamies par savu ķermeni.', 'en')
        self.assertEqual(service._call_gemini_api.call_count, 2)
        service._translate_with_openai_fallback.assert_not_called()
        self.assertEqual(service._history, [])

    def test_empty_and_malformed_joint_outputs_repair_atomically(self):
        for bad in ('{"en":"", "lt":"Viltis."}', '{"en":"Hope", "lt":"Vil'):
            with self.subTest(bad=bad):
                service = self.translator()
                service._call_gemini_api = MagicMock(side_effect=[bad, '{"en":"Hope.","lt":"Viltis."}'])
                self.assertEqual(service.translate_joint('Cerība.', ['en', 'lt']), {'en': 'Hope.', 'lt': 'Viltis.'})
                self.assertEqual(service._call_gemini_api.call_count, 2)
                self.assertEqual(len(service._history), 1)

    def test_paid_repair_counts_both_requests_and_rechecks_hard_limit(self):
        service = self.translator(openai_fallback_enabled=True)
        service._openai_client = MagicMock()
        def response(text):
            result = MagicMock()
            result.choices = [MagicMock(message=MagicMock(content=text, refusal=None))]
            result.usage.prompt_tokens, result.usage.completion_tokens = 100, 20
            return result
        service._openai_client.chat.completions.create.side_effect = [response('{"en":""}'), response('{"en":"Hope."}')]
        self.assertEqual(service._translate_with_openai_fallback('Cerība.', ['en']), {'en': 'Hope.'})
        self.assertEqual(service.circuit_breaker.stats.total_fallback_requests, 2)
        self.assertGreater(service.circuit_breaker.stats.estimated_cost_usd, 0)
        service = self.translator(openai_fallback_enabled=True)
        service._openai_client = MagicMock()
        service._openai_client.chat.completions.create.return_value = response('{"en":""}')
        service.circuit_breaker.is_fallback_allowed = MagicMock(side_effect=[True, True, False])
        with self.assertRaisesRegex(RuntimeError, 'hard limit'):
            service._translate_with_openai_fallback('Cerība.', ['en'])
        self.assertEqual(service._openai_client.chat.completions.create.call_count, 1)

    def test_unpaid_fallback_setting_forbids_even_repair_call(self):
        service = self.translator()
        service._openai_client = MagicMock()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            service._translate_with_openai_fallback('Cerība.', ['en'])
        service._openai_client.chat.completions.create.assert_not_called()

    def test_web_translation_echo_never_returned(self):
        service = self.translator()
        source = 'Mēs šorīt rūpējamies par savu ķermeni.'
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps([[[source]]]).encode()
        with patch('urllib.request.urlopen', return_value=response) as request:
            self.assertEqual(service._free_google_translate_fallback(source, 'en'), '')
        self.assertEqual(request.call_count, 3)  # Existing bounded endpoint list.

    def test_latvian_target_shared_names_and_short_speech_are_allowed(self):
        for text, language in [('Āmen.', 'en'), ('Kristaps', 'en'), ('OK', 'en'),
                               ('Mēs rūpējamies par savu ķermeni.', 'lv')]:
            self.assertIsNone(translation_issue(text, text, language))
        self.assertIsNone(translation_issue('Āmen.', '[Amen]', 'en'))

    def test_parsing_page_continuations_preserves_deliberate_repetition(self):
        pages = ['Title\n[12:00:00]\nLatvian: Palīdzi, palīdzi mums.\nEnglish: Help, help\n1',
                 'English: Help, help us.\n[12:00:08]\nLatvian: Āmen.\nEnglish:\n2']
        rows = parse_pages(pages)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['en'], 'Help, help us.')
        self.assertEqual(rows[0]['lv'], 'Palīdzi, palīdzi mums.')
        self.assertEqual(rows[1]['en'], '')
        self.assertEqual(corpus_statistics(rows)['quality_issues'][0]['reason'], 'empty')

    def test_reference_variants_preserve_spoken_numbers_and_unicode(self):
        ctx = SermonContextManager(self.root / 'ctx.json')
        text = '1.Kor. 6:9–20\nPsalms 127:2\nMt 4:4\n1. Timotejam 4:8\n1. Korintiešiem 10:31'
        ctx.set_text(text, save=False)
        for reference in text.splitlines():
            self.assertIn(reference, ctx.bible_references)
        ctx.set_text('Mūsu ķermenis ir Dieva dots. Rūpēties par savu ķermeni.', save=False)
        self.assertEqual(ctx.get_compact_prompt_hint('Autobuss izbrauks rīt pulksten deviņos.'), '')
        import unicodedata
        self.assertEqual(ctx.get_compact_prompt_hint('Mūsu ķermenis ir Dieva dots.'),
                         ctx.get_compact_prompt_hint(unicodedata.normalize('NFD', 'Mūsu ķermenis ir Dieva dots.')))
        self.assertEqual(ctx.get_compact_prompt_hint('no 2. nodaļas 19. panta.'), '')

    def test_group_reopens_aligns_exports_and_keeps_raw_source_rows(self):
        manager = SessionManager(self.root / 'sessions')
        sid = manager.start_session()
        for i, text in enumerate(['Nesāc ar nosodīšanu.', 'Nāc pie Dieva.', 'Āmen.']):
            manager.append_segment(TranscriptSegment(i, time.time(), f'12:00:0{i}', text, ''))
        manager.update_translation_group([0, 1], 'Do not condemn yourself. Come to God.', session_id=sid)
        reopened = SessionManager(self.root / 'sessions')
        rows = reopened.get_segments(sid)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1].status, 'translated_in_group')
        aligned = TranscriptExporter._aligned_segments(rows)
        self.assertEqual(len(aligned), 2)
        self.assertIn(rows[1].latvian_text, aligned[0].latvian_text)
        self.assertEqual(aligned[0].english_translation, rows[0].english_translation)
        self.assertEqual(rows[1].english_translation, '')
        for exporter, suffix in [(TranscriptExporter.export_to_txt, 'txt'), (TranscriptExporter.export_to_markdown, 'md')]:
            path = self.root / ('export.' + suffix)
            exporter(path, manager.get_session(sid), rows)
            result = path.read_text(encoding='utf-8')
            self.assertEqual(result.count('Come to God.'), 1)
            self.assertIn('[No English translation saved]', result)
        path = self.root / 'export.json'
        TranscriptExporter.export_to_json(path, manager.get_session(sid), rows)
        raw = json.loads(path.read_text(encoding='utf-8'))['segments']
        self.assertEqual(len(raw), 3)
        self.assertEqual(raw[1]['translation_group_id'], 0)
        self.assertEqual(raw[1]['en'], '')

    def test_existing_database_migration_is_nondestructive_and_idempotent(self):
        manager = SessionManager(self.root)
        sid = manager.start_session()
        manager.append_segment(TranscriptSegment(0, 1, '12:00:00', 'Āmen.', 'Amen.'))
        with manager._conn() as conn:
            conn.execute('ALTER TABLE segments DROP COLUMN translation_group_id')
            conn.commit()
        migrated = SessionManager(self.root)
        row = migrated.get_segments(sid)[0]
        self.assertIsNone(row.translation_group_id)
        self.assertEqual(row.english_translation, 'Amen.')
        self.assertEqual(SessionManager(self.root).get_segments(sid)[0].latvian_text, 'Āmen.')

    def test_group_update_is_scoped_to_originating_session(self):
        manager = SessionManager(self.root)
        first = manager.start_session()
        manager.append_segment(TranscriptSegment(0, 1, '', 'Pirmais.', ''))
        manager.end_session()
        second = manager.start_session()
        manager.append_segment(TranscriptSegment(0, 1, '', 'Otrais.', ''))
        manager.update_translation_group([0], 'First.', session_id=first)
        self.assertEqual(manager.get_segments(first)[0].english_translation, 'First.')
        self.assertEqual(manager.get_segments(second)[0].english_translation, '')

    def test_failed_voice_does_not_block_other_language_and_reopens_pending(self):
        engine = TranslationEngine.__new__(TranslationEngine)
        engine.config = self.config
        engine._tts = MagicMock()
        engine._tts.synthesize.side_effect = [RuntimeError('voice unavailable'), b'audio']
        engine._players = {'en': MagicMock(), 'lt': MagicMock()}
        engine._speech = DurableQueue(self.root / 'speech.sqlite')
        engine._speech.put({'sequence_id': 1, 'captured_at': time.monotonic(),
                            'translations': {'en': 'Hope.', 'lt': 'Viltis.'}, 'completed': []})
        engine.on_error, engine.on_status = MagicMock(), MagicMock()
        engine._speech_loop()
        engine._players['en'].enqueue.assert_not_called()
        engine._players['lt'].enqueue.assert_called_once()
        engine.on_error.assert_called_once()
        engine._speech.close()
        restored = DurableQueue(self.root / 'speech.sqlite')
        self.assertEqual(restored.get()['completed'], ['lt'])
        restored.close()
        engine._speech = DurableQueue(self.root / 'speech.sqlite')
        self.addCleanup(engine._speech.close)
        engine._speech.finish()
        engine._tts.synthesize.side_effect = None
        engine._tts.synthesize.return_value = b'recovered audio'
        engine.latency_tracker = PipelineLatencyTracker()
        engine.on_latency = MagicMock()
        engine.active_speed_settings = {'mode': 'balanced'}
        engine._speech_loop()
        engine._players['en'].enqueue.assert_called_once()
        engine._players['lt'].enqueue.assert_called_once()
        self.assertEqual(engine._speech.qsize(), 0)


if __name__ == '__main__':
    unittest.main()
