"""Opt-in packaged diagnostic: isolated settings, real Qt and audio enumeration.

Run ChurchTranslator.exe --smoke-test <report.json>. No credential contents are
written, and no external API is called. User settings are never changed.
"""
import json
import os
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
        with tempfile.TemporaryDirectory(dir=report_path.parent) as temp, patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': temp}), patch('church_translator.config.settings_path', return_value=Path(temp) / 'settings.json'):
            window = MainWindow()
            window.show()
            app.processEvents()
            panels = (window.latvian_text, window.english_text, window.russian_text)
            for measure in (lambda p: p.parentWidget().width(), lambda p: p.parentWidget().height(),
                            lambda p: p.width(), lambda p: p.height(),
                            lambda p: p.viewport().width(), lambda p: p.viewport().height()):
                sizes = [measure(p) for p in panels]
                assert max(sizes) - min(sizes) <= 1, sizes
            selector = window.secondary_language_combo
            assert selector.lineEdit().height() >= selector.fontMetrics().height()
            result['equal_panel_geometry'] = True
            # Exercise the actual packaged navigation at narrow logical widths,
            # including latency values wider than a normal service measurement.
            from PySide6.QtWidgets import QToolButton
            from PySide6.QtCore import Qt, QPoint
            result['responsive_navigation'] = []
            for width, height in ((1280, 860), (960, 700), (720, 540), (640, 480)):
                window._latency_samples.clear()
                window.resize(width, height)
                app.processEvents()
                page_geometry = window.main_scroll.geometry()
                for latency in ((10.0, 99999.9, .2) if width >= 960 else (10.0, 13.2, .2)):
                    window.set_latency(latency)
                    window.set_input_level(.016, .01)
                    app.processEvents()
                    assert window.width() == width
                    assert window.main_scroll.geometry() == page_geometry
                    for button in (window.start_button, window.pause_button, window.stop_button,
                                   window.context_button, window.export_button, window.summary_button,
                                   window.history_button, window.refresh_button, window.clear_text_button):
                        assert button.width() >= button.sizeHint().width(), button.text()
                    assert window.tabs.cornerWidget(Qt.TopRightCorner) is window.status_row
                    assert window.tabs.rect().contains(window.status_row.geometry())
                    assert not window.tabs.tabBar().geometry().intersects(window.status_row.geometry())
                    assert window.status_row.geometry().bottom() < window.main_scroll.mapTo(window.tabs, QPoint(0, 0)).y()
                    for label in (window.status_pill, window.speed_badge, window.latency_label, window.input_level):
                        assert window.status_row.rect().contains(label.geometry())
                    for index in range(window.tabs.count()):
                        window.tabs.setCurrentIndex(index)
                        app.processEvents()
                        bar = window.tabs.tabBar()
                        tab = bar.tabRect(index)
                        assert bar.rect().contains(tab), (width, index)
                        assert all(not tab.intersects(b.geometry()) for b in bar.findChildren(QToolButton) if b.isVisible())
                window.tabs.setCurrentIndex(0)
                app.processEvents()
                window.grab().save(str(report_path.with_name(f'{report_path.stem}-navigation-{width}.png')))
                result['responsive_navigation'].append({'width': width, 'height': height,
                    'all_four_tabs_visible_when_selected': True, 'page_geometry_stable': True,
                    'status_beside_tabs': True})
            window.resize(1280, 860)
            window._latency_samples.clear()
            window.latency_label.setText('⚡ Latency: --')
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
            assert window.secondary_dashboard_toggle.isChecked()
            window.russian_enabled.setChecked(False)
            assert not window.secondary_dashboard_toggle.isChecked()
            assert 'output is off' in window.russian_text.placeholderText()
            window.secondary_dashboard_toggle.click()
            assert window.russian_enabled.isChecked()
            result['dashboard_output_toggle'] = True
            window.secondary_language_combo.setCurrentIndex(window.secondary_language_combo.findData('lt'))
            window.openai_model_combo.setCurrentIndex(window.openai_model_combo.findData('gpt-4o-transcribe'))
            window.speed_mode_combo.setCurrentIndex(window.speed_mode_combo.findData('dynamic'))
            window._voice_settings = {'lt': {'voice': 'lt-LT-Standard-B', 'rate': .95, 'pitch': 1.0, 'gain': -2.0}}
            window._save_user_settings()
            window.close()
            window = MainWindow()
            assert window.english_volume.value() == 63
            assert window.russian_volume.value() == 41
            assert window.russian_enabled.isChecked()
            assert window.secondary_language_combo.currentData() == 'lt'
            assert window.openai_model_combo.currentData() == 'gpt-4o-transcribe'
            assert window.speed_mode_combo.currentData() == 'dynamic'
            assert window._voice_settings['lt']['rate'] == .95
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
            window.append_translation('lt', 'Paulius kalbėjo apie tikėjimą ir viltį.')
            window.set_status('Diagnostic <text> is escaped; no keys displayed.')
            app.processEvents()
            window.grab().save(str(report_path.with_suffix('.png')))
            selector = window.secondary_language_combo
            assert selector.count() == 62
            selector.setEditText('Japanese')
            selector._commit_search()
            window.append_translation('ja', 'パウロは信仰と希望について語りました。')
            assert window.russian_text.toPlainText() == 'パウロは信仰と希望について語りました。'
            for _ in range(3):
                app.processEvents()
            window.grab().save(str(report_path.with_name(f'{report_path.stem}-languages.png')))
            selector.showPopup()
            app.processEvents()
            selector.view().grab().save(str(report_path.with_name(f'{report_path.stem}-language-list.png')))
            selector.hidePopup()
            selector.setCurrentIndex(selector.findData('lt'))
            result['selectable_audio_languages'] = {'alternatives': selector.count(), 'search_routing': True}
            window.clear_text_button.click()
            window.clear_log_btn.click()
            assert not window.latvian_text.toPlainText()
            assert not window.log.toPlainText()
            # Exercise actual Start/Stop GUI wiring with a bounded fake service.
            with patch('church_translator.main.TranslationEngine') as fake, patch('church_translator.main.QMessageBox.question', return_value=65536):
                window.config = replace(window.config, openai_api_key='test', gemini_api_key='test')
                fake.return_value._translator.config = window.config
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
            from .dialogs import AISummaryDialog
            from .summarizer import SermonSummarizer, StructuredSummary
            from .session_manager import TranscriptSegment
            from .voice_dialog import VoiceSettingsDialog
            sid = window.session_manager.start_session('Diagnostic sermon')
            window.session_manager.append_segment(TranscriptSegment(0, time.time(), '10:00',
                'Es šodien domāju par bagātību un dzīves piepildījumu. Mantiskās vērtības vien nenosaka, '
                'vai dzīve ir piepildīta. Es atzīstu savas grūtības un nepilnības; bagātība nav bijusi '
                'mana galvenā uzmanība. Katram cilvēkam ticības ceļš ir atšķirīgs, un tā ilgums pats par sevi '
                'nenosaka cilvēka attiecības ar Dievu. Iedomāsimies sarunu ar Jēzu: ko mēs viņam jautātu '
                'par savu dzīvi? Šis jautājums aicina pārdomāt personīgo ticību un to, kas mūsu dzīvei piešķir jēgu.', ''))
            window.session_manager.end_session()
            summary = StructuredSummary('Wealth, Faith & Personal Fulfilment',
                "The speaker reflects on wealth and personal fulfilment, emphasizing that a meaningful life is not defined solely by possessions. They consider the individual nature of faith and a person's relationship with God.",
                ['Wealth is not the only measure of a fulfilling life.',
                 'Personal struggles and imperfections are acknowledged.',
                 "Each person's spiritual journey is unique."],
                highlights=['The speaker admits that wealth has not been their focus.',
                            'A question about a conversation with Jesus invites reflection.'], report_style=True)
            # A fresh sermon defaults to Latvian regardless of a global preference;
            # a sermon with a saved English report restores its own language.
            fresh = AISummaryDialog(SermonSummarizer(cfg), window.session_manager.get_session(sid),
                window.session_manager.get_segments(sid), window.session_manager, window)
            assert fresh.lang_combo.currentData() == 'lv'
            fresh.close()
            window.session_manager.save_session_summary(sid, summary.to_clean_text(), 'en')
            dialog = AISummaryDialog(SermonSummarizer(cfg), window.session_manager.get_session(sid),
                window.session_manager.get_segments(sid), window.session_manager, window)
            assert dialog.lang_combo.currentData() == 'en'
            result['summary_language_session_default_and_restore'] = True
            dialog.lang_combo.setCurrentIndex(0)
            dialog.show()
            app.processEvents()
            for width, height in ((860, 720), (800, 800), (640, 560)):
                dialog.resize(width, height)
                app.processEvents()
                for button in (dialog.generate_btn, dialog.copy_btn, dialog.download_btn, dialog.save_btn, dialog.edit_btn, dialog.close_btn):
                    assert button.width() >= button.minimumSizeHint().width(), button.text()
                dialog.grab().save(str(report_path.with_name(f'{report_path.stem}-summary-{width}.png')))
            dialog.edit_btn.click()
            assert not dialog.card_main_msg.isReadOnly()
            dialog.card_main_msg.setPlainText('Edited diagnostic message.')
            dialog._save_summary()
            assert 'Edited diagnostic message.' in window.session_manager.get_session(sid).summary_text_en
            dialog.edit_btn.click()
            localized = StructuredSummary('Bagātība, ticība un dzīves piepildījums',
                'Runātājs pārdomā bagātību un dzīves piepildījumu, uzsverot, ka mantiskās vērtības vien nenosaka dzīves jēgu. Viņš pievēršas katra cilvēka ticības ceļam un attiecībām ar Dievu.',
                ['Bagātība nav vienīgais piepildītas dzīves mērs.', 'Runātājs atzīst personīgās grūtības un nepilnības.', 'Katra cilvēka ticības ceļš ir atšķirīgs.'],
                highlights=['Runātājs atzīst, ka bagātība nav bijusi viņa uzmanības centrā.', 'Jautājums par sarunu ar Jēzu aicina pārdomāt ticību.'], report_style=True)
            window.session_manager.save_session_summary(sid, localized.to_report_text('lv'), 'lv')
            dialog.lang_combo.setCurrentIndex(1)
            for _ in range(3):
                app.processEvents()
            for button in (dialog.generate_btn, dialog.copy_btn, dialog.download_btn, dialog.save_btn, dialog.edit_btn, dialog.close_btn):
                assert button.width() >= button.minimumSizeHint().width(), button.text()
            dialog.grab().save(str(report_path.with_name(f'{report_path.stem}-summary-lv-640.png')))
            assert dialog.provider_info_label.height() >= dialog.provider_info_label.heightForWidth(dialog.provider_info_label.width())
            dialog.edit_btn.click()
            for _ in range(3):
                app.processEvents()
            dialog.grab().save(str(report_path.with_name(f'{report_path.stem}-summary-edit-640.png')))
            dialog.close()
            result['summary_edit_save_resize'] = True
            voice_dialog = VoiceSettingsDialog(replace(cfg, google_application_credentials=None), {}, lambda _: None, lambda _: 1, window)
            voice_dialog.show()
            app.processEvents()
            voice_dialog.grab().save(str(report_path.with_name(f'{report_path.stem}-voices.png')))
            voice_dialog.close()
            result['voice_dialog_no_network'] = True
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
