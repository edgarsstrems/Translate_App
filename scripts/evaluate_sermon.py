"""Offline sermon audit. PDF/DOCX stay local; no API calls or accuracy scores.

Run with the bundled document Python (pdfplumber and python-docx), or install
those optional analysis tools separately. They are not app runtime dependencies.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.translation_quality import translation_issue
from church_translator.sermon_context import SermonContextManager


def _continuation(old: str, new: str) -> str:
    """Qt's paginated export can repeat the beginning of a split text block."""
    if not old:
        return new
    if new.startswith(old):
        return new
    if old.startswith(new):
        return old
    return old + ' ' + new


def parse_pages(pages: list[str]) -> list[dict]:
    records = []
    current, language, block, block_page = None, None, [], 0

    def flush():
        nonlocal block
        if current is not None and language and block:
            text = ' '.join(block).strip()
            old = current[language]
            if old and current.get('_last_page') != block_page:
                current[language] = _continuation(old, text)
            else:
                current[language] = (old + ' ' + text).strip()
            current['_last_page'] = block_page
        block = []

    for page_number, page in enumerate(pages, 1):
        lines = page.splitlines()
        # Remove only the exact trailing page number, not numeric spoken lines.
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == str(page_number):
            lines.pop()
        for line in lines:
            line = line.strip()
            stamp = re.fullmatch(r'\[(\d{2}:\d{2}:\d{2})\]', line)
            if stamp:
                flush()
                current = {'id': len(records), 'timestamp': stamp[1], 'page': page_number,
                           'lv': '', 'en': ''}
                records.append(current)
                language = None
            elif line.startswith(('Latvian:', 'English:')) and current is not None:
                flush()
                label, content = line.split(':', 1)
                language, block_page = ('lv' if label == 'Latvian' else 'en'), page_number
                block = [content.strip()] if content.strip() else []
            elif language and line:
                block.append(line)
        # Finish blocks at page boundaries but retain current language for
        # continuations without a repeated label on the following page.
        flush()
        block_page = page_number + 1
    flush()
    for record in records:
        record.pop('_last_page', None)
    return records


def corpus_statistics(records: list[dict]) -> dict:
    times = [sum(int(n) * factor for n, factor in zip(r['timestamp'].split(':'), (3600, 60, 1))) for r in records]
    gaps = [b-a for a, b in zip(times, times[1:])]
    issues = [{'timestamp': r['timestamp'], 'page': r['page'],
               'reason': translation_issue(r['lv'], r['en'], 'en')}
              for r in records if translation_issue(r['lv'], r['en'], 'en')]
    return {'segments': len(records), 'first_timestamp': records[0]['timestamp'] if records else None,
            'last_timestamp': records[-1]['timestamp'] if records else None,
            'timestamp_span_seconds': times[-1]-times[0] if times else 0,
            'median_interval_seconds': statistics.median(gaps) if gaps else 0,
            'max_interval_seconds': max(gaps, default=0), 'interval_histogram': dict(sorted(Counter(gaps).items())),
            'nonincreasing_timestamps': sum(g <= 0 for g in gaps),
            'intervals_over_12_seconds': [{'after': records[i]['timestamp'], 'seconds': gap}
                                          for i, gap in enumerate(gaps) if gap > 12],
            'source_fragments_at_most_3_words': sum(len(r['lv'].split()) <= 3 for r in records),
            'source_ellipsis_endings': sum(r['lv'].endswith(('...', '…')) for r in records),
            'exact_repeated_source_adjacent': sum(a['lv'] == b['lv'] for a, b in zip(records, records[1:])),
            'quality_issues': issues,
            'limits': 'Timestamp intervals are export timing, not measured chunk duration or proof of audio gaps. No audio ground truth.'}


def parse_context(path: Path) -> dict:
    from docx import Document
    paragraphs = [{'index': i, 'style': p.style.name, 'text': p.text}
                  for i, p in enumerate(Document(path).paragraphs) if p.text.strip()]
    manager = SermonContextManager()
    manager.set_text('\n\n'.join(p['text'] for p in paragraphs), save=False)
    return {'paragraphs': paragraphs, 'title': manager.title,
            'references': manager.bible_references, 'key_terms': manager.key_terms,
            'sections': [{'index': s.index, 'heading': s.heading, 'text': s.text} for s in manager.sections]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--docx', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='Local output directory (normally build/)')
    args = parser.parse_args()
    import pdfplumber
    with pdfplumber.open(args.pdf) as pdf:
        pages = [page.extract_text() or '' for page in pdf.pages]
    records = parse_pages(pages)
    if not records:
        parser.error('No timestamped bilingual records found; inspect the PDF layout.')
    args.output.mkdir(parents=True, exist_ok=True)
    stats = corpus_statistics(records)
    stats.update(pages=len(pages), input_sha256={p.suffix: hashlib.sha256(p.read_bytes()).hexdigest()
                                                for p in (args.pdf, args.docx)})
    for filename, data in [('segments.json', records), ('context.json', parse_context(args.docx)), ('statistics.json', stats)]:
        (args.output / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
