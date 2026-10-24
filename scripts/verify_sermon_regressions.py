"""Replay fixed text diagnostics offline, without treating faulty output as truth."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.translation_quality import translation_issue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--dataset', type=Path, help='Optional local private evaluation set')
    args = parser.parse_args()
    dataset = args.dataset or Path(__file__).resolve().parents[1] / 'verification/translation-quality-fixtures.json'
    cases = json.loads(dataset.read_text(encoding='utf-8'))['cases']
    results, timings = [], []
    for case in cases:
        candidate = case.get('candidate_en', case.get('historical_en', ''))
        start = time.perf_counter()
        actual = translation_issue(case['source_lv'], candidate, 'en')
        timings.append((time.perf_counter() - start) * 1000)
        results.append({'id': case['id'], 'expected': case['expected_issue'], 'actual': actual,
                        'passed': actual == case['expected_issue'],
                        'previous_nonempty_gate_rejected': not bool(candidate.strip())})
    report = {'ok': all(r['passed'] for r in results), 'cases': results,
              'validator_median_ms': statistics.median(timings), 'validator_max_ms': max(timings),
              'previous_nonempty_gate_rejections': sum(r['previous_nonempty_gate_rejected'] for r in results),
              'current_quality_rejections': sum(bool(r['actual']) for r in results),
              'manually_justified_reference_translations': sum('reference_en' in c for c in cases),
              'limits': 'Deterministic response diagnostics only. No ASR, translation accuracy or model improvement score.'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
