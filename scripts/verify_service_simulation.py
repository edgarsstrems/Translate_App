"""Accelerated 60-minute text-pipeline soak; all providers/audio are simulated.

Runs production translation, validation, batching, SQLite history and durable
synthesis queues. It does not measure natural ASR, audible quality or live latency.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.config import load_config
from church_translator.engine import EngineSettings, TranslationEngine, TranslationItem
from church_translator.services import Translator
from church_translator.session_manager import SessionManager, TranscriptSegment
from church_translator.summarizer import SermonSummarizer
from church_translator.transcript_exporter import TranscriptExporter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.report.parent) as directory:
        os.environ['CHURCH_TRANSLATOR_APPDATA'] = directory
        cfg = replace(load_config(), gemini_api_key='test', openai_api_key=None,
                      openai_fallback_enabled=False, auto_return_to_gemini=False,
                      google_application_credentials=None)
        manager = SessionManager(Path(directory) / 'sessions')
        sid = manager.start_session(secondary_language='lt')
        logs, errors, delivered, queued = [], [], [], []
        settings = EngineSettings(0, True, True, 0, 0, lambda: 1., lambda: 1., 'lt')
        with patch.object(Translator, '_init_gemini_client'):
            engine = TranslationEngine(cfg, settings, logs.append, errors.append, lambda _: None,
                lambda _: None, lambda lang, text: delivered.append((lang, text)), session_manager=manager)
        engine._translator.refresh_available_models = MagicMock()
        # Virtual pacing removes deliberate inter-request waits in this accelerated
        # test. Actual pacing/deadlines are covered by separate wire/unit tests.
        engine._translator._wait_for_pacing = lambda **_: setattr(engine._translator, '_last_call_time', time.monotonic()-5)
        calls, rejected = [0], [0]
        def generate(model, prompt):
            calls[0] += 1
            time.sleep(.002)  # Small simulated provider delay.
            if calls[0] % 37 == 1:
                rejected[0] += 1
                return '{"en":"", "lt":""}'
            source = prompt.split('Latvian Sermon Text to Translate into ', 1)[1].split(':\n', 1)[1].split('\n\nReturn ONLY', 1)[0]
            numbers = re.findall(r'Frāze (\d+)', source)
            return json.dumps({'en': '\n'.join(f'Body care item {n}.' for n in numbers),
                               'lt': '\n'.join(f'Kūno priežiūra {n}.' for n in numbers)})
        engine._translator._call_gemini_api = generate
        engine._tts = MagicMock()
        engine._tts.synthesize.side_effect = lambda text, lang: text.encode('utf-8')
        for language in ('en', 'lt'):
            player = MagicMock()
            player.enqueue.side_effect = lambda audio, language, **kw: queued.append((language, audio.decode('utf-8'), kw['sequence_id']))
            engine._players[language] = player
        count = 450
        for i in range(count):
            text = f'Mēs rūpējamies par savu ķermeni. Frāze {i}.'
            manager.append_segment(TranscriptSegment(i, time.time(), f'{i//450+12:02d}:{i//60%60:02d}:{i%60:02d}', text, ''))
            engine._translations.put(TranslationItem(time.monotonic(), transcript=text,
                sequence_id=i, source_segment_ids=[i], enqueued_at=time.monotonic()))
        engine._translations.finish()
        tracemalloc.start()
        started = time.monotonic()
        workers = [threading.Thread(target=engine._translation_loop), threading.Thread(target=engine._speech_loop)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(40)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert all(not w.is_alive() for w in workers), 'Simulation worker failed to finish'
        assert not errors, errors
        for language, prefix in [('en', 'Body care item'), ('lt', 'Kūno priežiūra')]:
            expected = [f'{prefix} {i}.' for i in range(count)]
            assert [line for lang, text in delivered if lang == language for line in text.splitlines()] == expected
            assert [line for lang, text, seq in queued if lang == language for line in text.splitlines()] == expected
        rows = manager.get_segments(sid)
        assert len(rows) == count
        aligned = TranscriptExporter._aligned_segments(rows)
        assert len(aligned) == 150
        assert all('[No English' not in r.english_translation for r in aligned)
        summary = SermonSummarizer(cfg)
        summary._call_ai = MagicMock(return_value=json.dumps({'title': 'Rūpes par ķermeni',
            'main_message': 'Runātājs runā par rūpēm par ķermeni.',
            'key_teachings': ['Rūpēties par savu ķermeni.'], 'highlights': []}))
        text = summary.generate_summary(rows, language='lv')
        prompt = summary._call_ai.call_args.args[0]
        assert all(f'Frāze {i}.' in prompt for i in range(count))
        manager.save_session_summary(sid, text, 'lv')
        manager.end_session()
        assert SessionManager(manager.db_dir).get_session(sid).summary_text_lv == text
        requests = [float(m[1]) for line in logs if (m := re.search(r'stage=translation .*processing=([\d.]+)s', line))]
        report = {'ok': True, 'kind': 'accelerated simulated providers, no physical playback',
            'represented_service_seconds': 3600, 'source_rows': count, 'translation_batches': len(aligned),
            'provider_calls': calls[0], 'injected_invalid_responses_repaired': rejected[0],
            'translated_rows_per_language': count, 'queued_speech_rows_per_language': count,
            'workers_finished': True, 'summary_prompt_covers_all_source_rows': True,
            'summary_history_reopened': True, 'remaining_translation_jobs': engine._translations.qsize(),
            'remaining_synthesis_jobs': engine._speech.qsize(), 'bounded_history_rows': len(engine._translator._history),
            'peak_traced_python_bytes_during_drain': peak, 'wall_seconds': round(time.monotonic()-started, 3),
            'simulated_batch_processing_median_seconds': statistics.median(requests) if requests else None,
            'limits': 'Virtual pacing; mocked STT/provider/TTS. Traced Python allocation is not total process RSS or a wall-clock hour stability measurement.'}
        for queue in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            queue.close()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
