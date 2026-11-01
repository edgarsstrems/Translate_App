"""Local, streaming multi-segment diagnostic export. No providers are called."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import zipfile

from .recording_guard import recording_lease
from .speech_audit import SpeechAudit


def parse_segments(value, maximum_count=100000):
    result = set()
    for token in value.split(','):
        match = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?', token)
        if not match:
            raise ValueError('Use segment numbers or inclusive ranges, for example 185-191, 193.')
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first or last > 2147483647 or last - first + 1 > maximum_count:
            raise ValueError('Invalid or excessively large segment range.')
        result.update(range(first, last + 1))
        if len(result) > maximum_count:
            raise ValueError('Select at most 100,000 segments per archive.')
    return sorted(result)


def recording_info(database):
    audit = SpeechAudit.existing(database)
    db = audit._connect()
    try:
        rows = db.execute("SELECT segment,at,details FROM events WHERE kind='captured_segment' ORDER BY id").fetchall()
        expired = bool(db.execute("SELECT 1 FROM events WHERE kind='diagnostic_audio_expired' LIMIT 1").fetchone())
    finally:
        db.close()
    return {'recording_id': Path(database).parent.name,
            'session_ids': sorted({json.loads(row[2]).get('session_id') for row in rows
                                   if json.loads(row[2]).get('session_id')}),
            'first_segment_utc': datetime.fromtimestamp(rows[0][1], timezone.utc).isoformat() if rows else None,
            'segments': list(dict.fromkeys(row[0] for row in rows)) if not expired else [],
            'audio_expired': expired}


def segment_ranges(segments):
    groups = []
    for number in sorted(segments):
        if groups and number == groups[-1][-1] + 1:
            groups[-1].append(number)
        else:
            groups.append([number])
    return ', '.join(str(g[0]) if len(g) == 1 else f'{g[0]}-{g[-1]}' for g in groups)


def export_segments(database, destination, segments):
    from scripts.export_speech_diagnostic import export_segment
    database, destination = Path(database), Path(destination)
    requested = sorted(set(segments))
    if not requested:
        raise ValueError('Choose at least one segment.')
    with recording_lease(database.parent):
        info = recording_info(database)
        available = set(info['segments'])
        manifest = {**info, 'private': True, 'verified_reference': False,
                    'requested_segments': requested, 'exported_segments': [],
                    'missing_segments': sorted(set(requested) - available), 'failures': [],
                    'created_utc': datetime.now(timezone.utc).isoformat()}
        destination.parent.mkdir(parents=True, exist_ok=True)
        # One temporary segment at a time; never collect a service's WAVs in RAM.
        with tempfile.TemporaryDirectory() as temporary, zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
            for segment in requested:
                if segment not in available:
                    continue
                single = Path(temporary) / f'{segment}.zip'
                try:
                    export_segment(database, single, segment=segment)
                    with zipfile.ZipFile(single) as source:
                        for entry in source.infolist():
                            with source.open(entry) as reader, bundle.open(f'segment-{segment}/{entry.filename}', 'w') as writer:
                                import shutil
                                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    manifest['exported_segments'].append(segment)
                except Exception as exc:
                    manifest['failures'].append({'segment': segment, 'error': type(exc).__name__})
                finally:
                    single.unlink(missing_ok=True)
            bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        return manifest
