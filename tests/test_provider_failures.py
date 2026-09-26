import io
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.services import OpenAITranscriber, Translator
from church_translator.reliability import GeminiAccessError


class ProviderFailureTests(unittest.TestCase):
    def test_upload_trims_only_outer_digital_silence_with_guard(self):
        audio = np.concatenate([np.zeros(16000), np.array([.00001, -.00001], dtype='float32'),
                                np.zeros(16000)]).astype('float32')
        optimized = OpenAITranscriber._trim_outer_digital_silence(audio)
        self.assertEqual(len(optimized), 8002)
        np.testing.assert_array_equal(optimized[4000:4002], audio[16000:16002])
        self.assertEqual(np.count_nonzero(optimized), np.count_nonzero(audio))

    def test_lossless_flac_is_compact_and_decodes_every_sample(self):
        config = replace(load_config(), openai_api_key="test-key")
        service = OpenAITranscriber(config, Glossary({}, {}), lambda _: None)
        service._client = MagicMock()
        audio = (.01 * np.sin(np.arange(80000) * .07)).astype('float32')
        encoded_size = []
        def respond(**kwargs):
            payload = kwargs['file'].read()
            encoded_size.append(len(payload))
            decoded, rate = sf.read(io.BytesIO(payload))
            self.assertEqual(rate, 16000)
            np.testing.assert_allclose(decoded, audio, atol=1 / 32768)
            return MagicMock(text='')
        service._client.audio.transcriptions.create.side_effect = respond
        service.transcribe(audio)
        self.assertLess(encoded_size[0], audio.nbytes * .55)

    def test_short_stop_tail_preserves_samples_and_pads_request(self):
        config = replace(load_config(), openai_api_key="test-key")
        service = OpenAITranscriber(config, Glossary({}, {}), lambda _: None)
        service._client = MagicMock()
        audio = np.linspace(-0.2, 0.2, 1280, dtype=np.float32)

        def respond(**kwargs):
            decoded, rate = sf.read(io.BytesIO(kwargs["file"].read()))
            self.assertEqual(rate, 16000)
            self.assertEqual(len(decoded), 8000)
            np.testing.assert_allclose(decoded[:1280], audio, atol=1 / 32768)
            np.testing.assert_array_equal(decoded[1280:], 0)
            return MagicMock(text="")

        service._client.audio.transcriptions.create.side_effect = respond
        service.transcribe(audio)
        service._client.audio.transcriptions.create.assert_called_once()

    def test_project_denial_does_not_try_other_models(self):
        config = replace(load_config(), gemini_api_key="test-key")
        with patch.object(Translator, "_init_gemini_client"):
            service = Translator(config, Glossary({}, {}))
        service._gemini_model_name = config.gemini_model
        service._call_gemini_api = MagicMock(side_effect=RuntimeError(
            "403 PERMISSION_DENIED Your project has been denied access. Please contact support."))
        with self.assertRaisesRegex(GeminiAccessError, "Changing models will not fix"):
            service.check_access()
        service._call_gemini_api.assert_called_once()
        self.assertEqual(service._history, [])

    def test_access_check_does_not_add_sermon_history(self):
        config = replace(load_config(), gemini_api_key="test-key")
        with patch.object(Translator, "_init_gemini_client"):
            service = Translator(config, Glossary({}, {}))
        service._gemini_model_name = config.gemini_model
        service._call_gemini_api = MagicMock(return_value="Hello.")
        service.check_access()
        self.assertEqual(service._history, [])
