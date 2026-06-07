import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from church_translator.config import load_config, model_root
from church_translator.glossary import load_glossary
from church_translator.services import LocalWhisperTranscriber


def main() -> int:
    config = load_config()
    glossary = load_glossary(PROJECT_ROOT)
    print(f"Preparing Whisper model '{config.whisper_model_size}' in {model_root()}...")
    transcriber = LocalWhisperTranscriber(config, glossary, print)
    transcriber.ensure_model()
    print("Whisper model is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
