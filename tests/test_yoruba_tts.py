"""Local voice consent, integrity, routing and English failure isolation."""
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from PySide6.QtWidgets import QApplication
from church_translator import yoruba_tts
from church_translator.backlog import DurableQueue
from church_translator.config import load_config
from church_translator.engine import TranslationEngine, EngineSettings
from church_translator.services import TextToSpeech
from church_translator.voice_dialog import VoiceSettingsDialog


class YorubaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        env = patch.dict(os.environ, {'CHURCH_TRANSLATOR_APPDATA': folder.name})
        env.start()
        self.addCleanup(env.stop)

    def test_download_requires_consent_and_reuses_only_verified_files(self):
        with patch('church_translator.yoruba_tts.urllib.request.urlopen') as network:
            with self.assertRaisesRegex(RuntimeError, 'consent'):
                yoruba_tts.install()
            network.assert_not_called()
            data = b'verified model fixture'
            manifest = {'model.safetensors': (len(data), hashlib.sha256(data).hexdigest())}
            with patch.dict(yoruba_tts.FILES, manifest, clear=True):
                network.return_value = io.BytesIO(data)
                yoruba_tts.install(consent=True, directory=self.root)
                network.assert_called_once()
                network.reset_mock()
                yoruba_tts.install(consent=True, directory=self.root)
                network.assert_not_called()
                (self.root / 'model.safetensors').write_bytes(b'x' * len(data))
                network.return_value = io.BytesIO(data)
                yoruba_tts.install(consent=True, directory=self.root)
                network.assert_called_once()
                self.assertEqual((self.root / 'model.safetensors').read_bytes(), data)

    def test_interrupted_download_is_never_ready_or_loaded(self):
        data = b'complete model'
        manifest = {'model.safetensors': (len(data), hashlib.sha256(data).hexdigest())}
        with patch.dict(yoruba_tts.FILES, manifest, clear=True), patch.object(
                yoruba_tts, 'install_directory', return_value=self.root), patch(
                'church_translator.yoruba_tts.urllib.request.urlopen', return_value=io.BytesIO(b'partial')):
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                yoruba_tts.install(consent=True)
            self.assertNotEqual(yoruba_tts.installation_status(verify=True), 'Ready')
            self.assertFalse((self.root / 'model.safetensors').exists())

    def test_yoruba_text_is_sent_only_to_local_engine_with_tone_marks(self):
        cfg = replace(load_config(), google_application_credentials=None)
        service = TextToSpeech(cfg)
        with patch.object(yoruba_tts, 'synthesize', return_value=b'local audio') as local, patch.object(
                service, '_free_fallback') as web:
            text = 'Ẹ jẹ́ kí a ní ìfẹ́ àti sùúrù.'
            self.assertEqual(service.synthesize(text, 'yo'), b'local audio')
            self.assertEqual(local.call_args.args[0], text)
            web.assert_not_called()

    def test_model_cannot_silently_delete_digits_or_unknown_name_letters(self):
        tokenizer = MagicMock()
        tokenizer.get_vocab.return_value = {'o': 1, 'r': 2, 'i': 3, ' ': 4}
        for text in ('ori 3:12', 'ori z'):
            with self.subTest(text=text), patch.object(yoruba_tts, '_runtime',
                    return_value=(MagicMock(), tokenizer, MagicMock())):
                with self.assertRaisesRegex(RuntimeError, 'cannot pronounce'):
                    yoruba_tts.synthesize(text)
        tokenizer.assert_not_called()

    def test_optional_yoruba_failure_retains_jobs_and_all_english_continues(self):
        cfg = replace(load_config(), gemini_api_key=None)
        engine = TranslationEngine(cfg, EngineSettings(0, True, True, None, None, lambda: 0, lambda: 0,
                                                       secondary_language='yo'),
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        for queue in (engine._chunks, engine._translations, engine._speech, engine._unrecognized):
            self.addCleanup(queue.close)
        engine._players = {'en': MagicMock(), 'yo': MagicMock()}
        engine._tts = MagicMock()
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros(1600), 16000, format='WAV')
        def synthesize(text, language):
            if language == 'yo':
                raise RuntimeError('voice unavailable')
            return buffer.getvalue()
        engine._tts.synthesize.side_effect = synthesize
        for seq in range(3):
            engine._speech.put({'sequence_id': seq, 'captured_at': 1,
                                'translations': {'en': f'English {seq}', 'yo': f'Yoruba {seq}'}, 'completed': []})
        engine._speech.finish()
        engine._speech_loop()
        self.assertEqual(engine._players['en'].enqueue.call_count, 3)
        engine._players['yo'].enqueue.assert_not_called()
        self.assertEqual(engine._speech.qsize(), 0)
        deferred = DurableQueue(engine.session_dir / 'deferred-speech-yo.sqlite')
        self.addCleanup(deferred.close)
        self.assertEqual(deferred.qsize(), 3)
        self.assertEqual(engine.on_error.call_count, 3)

    def test_install_control_available_without_cloud_credentials(self):
        cfg = replace(load_config(), google_application_credentials=None)
        dialog = VoiceSettingsDialog(cfg, {}, lambda language: None, lambda language: 0)
        try:
            dialog.language.setCurrentIndex(dialog.language.findData('yo'))
            if dialog._task:
                dialog._task.wait(2000)
                self.app.processEvents()
            self.assertFalse(dialog.install_yoruba.isHidden())
            self.assertTrue(dialog.install_yoruba.isEnabled())
            self.assertFalse(dialog.preview.isEnabled())
            self.assertIn('Not installed', dialog.note.text())
        finally:
            if dialog._task:
                dialog._task.wait(2000)
            dialog.close()


if __name__ == '__main__':
    unittest.main()
