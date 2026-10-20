"""Opt-in live translation check. Sends only these fixed public test sentences.

OpenAI is called only after a genuine Gemini request failure, and only with the
explicit command flag. Reports include fixture text and timings, never keys.
"""
import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator import config
from church_translator.glossary import load_glossary
from church_translator.reliability import safe_error
from church_translator.services import Translator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--allow-openai-fallback', action='store_true')
    parser.add_argument('--gemini-model', choices=('auto', 'gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'))
    args = parser.parse_args()
    root = args.root.resolve()
    config.project_root = lambda: root
    cfg = replace(config.load_config(), translation_provider='gemini',
                  openai_fallback_enabled=args.allow_openai_fallback,
                  auto_return_to_gemini=False)
    if args.gemini_model:
        cfg = replace(cfg, gemini_model=args.gemini_model)
    fixtures = ['Jēzus sacīja mācekļiem, lai viņi seko Viņam.',
                'Viņi atstāja visu un gāja Viņam līdzi.',
                'Mēs lūdzam mieru un cerību mūsu ģimenēm.']
    results = []
    for source in fixtures:
        logs = []
        service = Translator(cfg, load_glossary(root), status_cb=logs.append)
        started = time.monotonic()
        result = {'source': source}
        try:
            translations = service.translate_joint(source, ['en', 'lt'])
            expected_providers = {'gemini', 'openai'} if args.allow_openai_fallback else {'gemini'}
            result.update(ok=all(translations.get(lang, '').strip() for lang in ('en', 'lt'))
                          and service.last_provider_used in expected_providers,
                          provider=service.last_provider_used,
                          model=getattr(service, '_working_model', None), translations=translations,
                          fallback_requests=service.circuit_breaker.stats.total_fallback_requests)
        except Exception as exc:
            result.update(ok=False, error=safe_error(exc))
        finally:
            result['seconds'] = round(time.monotonic() - started, 3)
            result['diagnostics'] = logs
            for client in (service._genai_client, service._openai_client):
                if client:
                    client.close()
        results.append(result)
    report = {'ok': all(result['ok'] for result in results),
              'fixture_kind': 'fixed synthetic text; no microphone or sermon uploaded',
              'gemini_timeout_seconds': cfg.gemini_timeout_seconds,
              'requested_model': cfg.gemini_model,
              'languages': ['en', 'lt'], 'results': results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': report['ok'], 'report': str(args.report),
                      'samples': [{key: row.get(key) for key in ('ok', 'provider', 'seconds', 'error')}
                                  for row in results]}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
