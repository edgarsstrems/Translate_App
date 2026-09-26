"""Export unfinished session data without modifying it or replaying partial audio.

python scripts/export_backlog.py <session-folder> <export-folder>
"""
import json
import sqlite3
import sys
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from church_translator.backlog import DurableQueue

session, target = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
target.mkdir(parents=True, exist_ok=True)
manifest = []
for source in sorted(session.glob('*.sqlite')):
    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    for row_id, payload in db.execute('SELECT id, payload FROM work ORDER BY id'):
        item = json.loads(payload, object_hook=DurableQueue._decode)
        name = f'{source.stem}-{row_id:06d}'
        if isinstance(item, bytes):
            (target / (name + '.wav')).write_bytes(item)
            manifest.append({'file': name + '.wav', 'stage': source.stem,
                             'note': 'First pending playback item may already have partially played. Review before replay.'})
        elif isinstance(item, dict):
            audio = item.pop('audio', None)
            if audio is not None:
                sf.write(target / (name + '.wav'), audio, 16000, subtype='FLOAT')
                item['audio_file'] = name + '.wav'
            manifest.append({'stage': source.stem, 'row': row_id, **item})
    db.close()
(target / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
print(f'Exported {len(manifest)} pending items to {target}')
