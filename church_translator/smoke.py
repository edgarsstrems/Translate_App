"""Opt-in packaged diagnostic: isolated settings, real Qt and audio enumeration.

Run ChurchTranslator.exe --smoke-test <report.json>. No credential contents are
written, and no external API is called. User settings are never changed.
"""
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock


def run_smoke(report_path: Path) -> int:
    from PySide6.QtWidgets import QApplication
    from .main import MainWindow
    from .audio import AudioDevice
    from .services import OpenAITranscriber, Translator, TextToSpeech
    from .glossary import Glossary
    from .config import inspect_service_account_file
    from .reliability import safe_error
    result = {"ok": False}
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    import faulthandler
    trace = report_path.with_suffix(".trace").open("w")
    faulthandler.dump_traceback_later(20, file=trace)
    try:
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(dir=report_path.parent) as temp, patch('church_translator.config.settings_path', return_value=Path(temp) / 'settings.json'):
            window = MainWindow()
            window.show()
            app.processEvents()
            result['tabs'] = []
            for index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(index)
                app.processEvents()
                result['tabs'].append(window.tabs.tabText(index))
            result['input_devices'] = sum(d.max_input_channels > 0 for d in window.devices)
            result['output_devices'] = sum(d.max_output_channels > 0 for d in window.devices)
            # Real device/DLL smoke: no captured audio is saved or uploaded, and
            # output uses digital silence so diagnostics do not interrupt a room.
            import sounddevice as sd
            import numpy as np
            result['hardware'] = {}
            try:
                frame_count = [0]
                def count_frames(indata, frames, timing, status):
                    frame_count[0] += frames
                with sd.InputStream(samplerate=16000, channels=1, dtype='float32', callback=count_frames):
                    time.sleep(.5)
                assert frame_count[0] > 0
                result['hardware']['microphone_frames'] = frame_count[0]
            except Exception as exc:
                result['hardware']['input_error'] = safe_error(exc)
            try:
                with sd.OutputStream(samplerate=16000, channels=1, dtype='float32') as stream:
                    stream.write(np.zeros((1600, 1), dtype='float32'))
                result['hardware']['silent_output'] = True
            except Exception as exc:
                result['hardware']['output_error'] = safe_error(exc)
            window.english_volume.setValue(63)
            window.russian_volume.setValue(41)
            window.russian_enabled.setChecked(True)
            window.openai_model_combo.setCurrentIndex(window.openai_model_combo.findData('gpt-4o-transcribe'))
            window._save_user_settings()
            window.close()
            window = MainWindow()
            assert window.english_volume.value() == 63
            assert window.russian_volume.value() == 41
            assert window.russian_enabled.isChecked()
            assert window.openai_model_combo.currentData() == 'gpt-4o-transcribe'
            result['settings_restart'] = True
            devices = [AudioDevice(99, 'Test USB', 1, 0, 'WASAPI')]
            saved = {'index': 3, 'name': 'Test USB', 'identity': devices[0].identity}
            window._fill_combo(window.input_combo, devices, False, saved)
            assert window.input_combo.currentData() == 99
            window._fill_combo(window.input_combo, [AudioDevice(3, 'Other', 1, 0)], False, saved)
            assert window.input_combo.currentData()['missing']
            window._save_user_settings()
            assert window.user_settings['devices']['input']['identity'] == saved['identity']
            result['routing_reindex_disconnect'] = True
            window.refresh_devices()
            window.tabs.setCurrentIndex(0)
            window.show()
            window.append_transcript('Pāvils runāja par ticību un cerību.')
            window.append_translation('en', 'Paul spoke about faith and hope.')
            window.set_status('Diagnostic <text> is escaped; no keys displayed.')
            app.processEvents()
            window.grab().save(str(report_path.with_suffix('.png')))
            window.clear_text_button.click()
            window.clear_log_btn.click()
            assert not window.latvian_text.toPlainText()
            assert not window.log.toPlainText()
            # Exercise actual Start/Stop GUI wiring with a bounded fake service.
            with patch('church_translator.main.TranslationEngine') as fake, patch('church_translator.main.QMessageBox.question', return_value=65536):
                window.config = replace(window.config, openai_api_key='test', gemini_api_key='test')
                window._active_config = lambda: window.config
                window.input_combo.clear()
                window.input_combo.addItem('Test Input', 0)
                for _ in range(3):
                    window.start_button.click()
                    assert window.engine is not None
                    window.stop_button.click()
                    deadline = time.monotonic() + 3
                    while window.engine is not None and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(.01)
                    assert window.engine is None
                    assert window.start_button.isEnabled()
                assert fake.return_value.start.call_count == 3
                assert fake.return_value.stop.call_count == 3
            result['start_stop_cycles_mocked_services'] = 3
            cfg = window.config
            stt = OpenAITranscriber(cfg, Glossary(), lambda _: None)
            stt.ensure_model()
            stt._client.close()
            translator = Translator(cfg, Glossary())
            assert translator._genai_client is not None
            translator._genai_client.close()
            TextToSpeech(cfg)
            import google.cloud.texttospeech, soundfile, sounddevice, openai, google.genai
            import faster_whisper, ctranslate2, av
            result['packaged_imports'] = True
            result['sdk_initialization_no_network'] = True
            window.close()
            result['ok'] = True
    except Exception as exc:
        import traceback
        result['error'] = safe_error(exc)
        result['traceback'] = safe_error(traceback.format_exc())
    faulthandler.cancel_dump_traceback_later()
    trace.close()
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    return 0 if result['ok'] else 1
