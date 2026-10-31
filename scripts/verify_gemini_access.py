"""Small opt-in Gemini check using fixed public fixtures; never calls OpenAI."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.config import load_config
from church_translator.glossary import Glossary
from church_translator.reliability import classify_gemini_error, safe_error
from church_translator.services import Translator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--free-tier-confirmed', action='store_true', required=True)
    parser.add_argument('--models', nargs='+', default=['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.6-flash'])
    parser.add_argument('--languages', nargs='+', choices=['en', 'yo'], default=['en'])
    args = parser.parse_args()
    config = replace(load_config(), openai_fallback_enabled=False, gemini_timeout_seconds=15)
    logs = []
    service = Translator(config, Glossary(), logs.append)
    report = {'api_version': 'v1beta', 'openai_requests': 0,
              'billing': 'User confirmed free-tier project; no billing/account change.', 'calls': []}
    try:
        started = time.monotonic()
        catalog = list(service._genai_client.models.list())
        available = [m.name.removeprefix('models/') for m in catalog
                     if 'generateContent' in (m.supported_actions or [])]
        report['discovery_seconds'] = round(time.monotonic() - started, 3)
        report['flash_models'] = [m for m in available if 'flash' in m and
                                  not any(t in m for t in ('image', 'live', 'tts', 'audio', 'preview'))]
        # Only endpoints whose published Standard pricing includes a free tier.
        eligible = {'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.6-flash', 'gemini-2.5-flash'}
        models = [m for m in args.models if m in available and m in eligible][:3]
        fixture = ('Apvelciet sirsnīgu līdzjūtību, laipnību un pazemību. '
                   'Vai mēs to darām? Nē, ne vienmēr. Pāvils to raksta Kolosiešiem 3:12.')
        for model in models:
            started = time.monotonic()
            row = {'model': model}
            try:
                raw = service._call_gemini_api(model, service._build_translation_prompt(fixture, args.languages))
                row.update(status=200, response=raw)
            except Exception as exc:
                row.update(status=getattr(exc, 'code', None) or getattr(exc, 'status_code', None),
                           category=classify_gemini_error(exc)[0], error=safe_error(exc))
            row['elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['calls'].append(row)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        report['discovery_error'] = safe_error(exc)
    finally:
        report['logs'] = logs
        report['limitation'] = 'Small access/payload check, not a representative success rate or accuracy benchmark.'
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True, indent=2))
        if service._genai_client:
            service._genai_client.close()


if __name__ == '__main__':
    main()
