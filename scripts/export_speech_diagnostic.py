"""Export one local segment and its audit trail. Never makes network requests.

The ZIP contains private audio/text; choose a private destination. Use --tail
to recover raw callbacks committed before a crash but not yet segmented.
"""
import argparse
import io
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import soundfile as sf
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.speech_audit import SpeechAudit, sample_hash
from church_translator.recording_guard import recording_lease


def export_segment(database, destination, segment=None, tail=None):
    with recording_lease(Path(database).parent):
        return _export_segment(database, destination, segment, tail)


def _export_segment(database, destination, segment=None, tail=None):
    audit = SpeechAudit.existing(database)
    db = audit._connect()
    try:
        events = [{'kind': kind, 'segment': seq, 'at': at, 'details': json.loads(details)}
                  for kind, seq, at, details in db.execute(
                      'SELECT kind,segment,at,details FROM events WHERE (? OR segment=?) ORDER BY id',
                      (bool(tail), segment))]
        if tail:
            capture_id = tail
            captured = [e['details']['capture']['end_sample'] for e in events
                        if e['kind'] == 'captured_segment' and e['details'].get('capture', {}).get('capture_id') == tail]
            start = max(captured, default=0)
            end = db.execute('SELECT MAX(end_sample) FROM capture WHERE capture_id=?', (tail,)).fetchone()[0]
            selected = [e for e in events if e['kind'] in {'recognition_failure', 'completion'}]
        else:
            selected = [e for e in events if e['segment'] == segment]
            capture = next(e['details']['capture'] for e in selected if e['kind'] == 'captured_segment')
            capture_id, start, end = capture['capture_id'], capture['start_sample'], capture['end_sample']
    finally:
        db.close()
    if end is None or end <= start:
        raise ValueError('No journaled audio in the requested range')
    audio = audit.audio_range(capture_id, start, end)
    db = audit._connect()
    try:
        submissions = (db.execute('SELECT id,audio,metadata,status,response,error FROM submissions '
                                  'WHERE segment=? ORDER BY id', (segment,)).fetchall()
                       if not tail and db.execute("SELECT 1 FROM sqlite_master WHERE name='submissions'").fetchone()
                       else [])
    finally:
        db.close()
    wav = io.BytesIO()
    sf.write(wav, audio, 16000, format='WAV', subtype='FLOAT')
    metadata = {'private': True, 'verified_reference': False, 'capture_id': capture_id,
                'start_sample': start, 'end_sample': end, 'sample_rate': 16000,
                'original_float32_sha256': sample_hash(audio), 'events': selected}
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('original.wav', wav.getvalue())
        bundle.writestr('audit.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        realtime = Path(database).parent / 'realtime.sqlite'
        if realtime.exists() and not tail:
            rt = sqlite3.connect(realtime.resolve().as_uri() + '?mode=ro', uri=True)
            rt.row_factory = sqlite3.Row
            try:
                turns = rt.execute('SELECT * FROM turns WHERE sequence_id=?', (segment,)).fetchall()
                manifest = {'turns': [dict(row) for row in turns], 'packets': [], 'events': []}
                for turn in turns:
                    for packet in rt.execute('SELECT * FROM packets WHERE turn_id=? ORDER BY id', (turn['id'],)):
                        info = {key: packet[key] for key in packet.keys() if key != 'pcm'}
                        info['metadata'] = json.loads(info['metadata'])
                        if packet['pcm'] is not None:
                            buffer = io.BytesIO()
                            import numpy as np
                            sf.write(buffer, np.frombuffer(packet['pcm'], '<i2'), 24000, format='WAV', subtype='PCM_16')
                            filename = f'realtime-packet-{packet["id"]:06d}.wav'
                            bundle.writestr(filename, buffer.getvalue())
                            info['audio_file'] = filename
                        manifest['packets'].append(info)
                    manifest['events'].extend({**dict(event), 'payload': json.loads(event['payload'])} for event in
                        rt.execute('SELECT * FROM events WHERE turn_id=? ORDER BY id', (turn['id'],)))
                bundle.writestr('realtime.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            finally:
                rt.close()
        for index, (receipt, encoded, details, status, response, error) in enumerate(submissions, 1):
            details = json.loads(details)
            suffix = '.flac' if str(details.get('filename', '')).endswith('.flac') else '.wav'
            bundle.writestr(f'submitted-{index:03d}{suffix}', encoded)
            bundle.writestr(f'submitted-{index:03d}.json', json.dumps({
                'receipt': receipt, 'request': details, 'status': status,
                'raw_provider_response': json.loads(response) if response else None,
                'error': error}, ensure_ascii=False, indent=2))
        bundle.writestr('README.txt', 'Private local diagnostic. Original audio + AI hypotheses. '
                        'No human-verified reference. No automatic provider uploads. '
                        'Sample ranges are half-open, scoped to capture_id. WAV preserves float32 samples.\n')
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('destination', type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--segment', type=int)
    group.add_argument('--tail', help='capture_id whose unsegmented crash tail should be exported')
    args = parser.parse_args()
    result = export_segment(args.database, args.destination, args.segment, args.tail)
    print(json.dumps({key: result[key] for key in ('capture_id', 'start_sample', 'end_sample')}))
