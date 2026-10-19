"""Verify source parity and public archive privacy without printing secrets."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyInstaller.archive.readers import ZlibArchiveReader
from church_translator.config import load_config


def main():
    root = Path(__file__).resolve().parents[1]
    reader = ZlibArchiveReader(str(root / 'build/pyinstaller/ChurchTranslator/PYZ-00.pyz'))
    modules = []
    for source in sorted((root / 'church_translator').glob('*.py')):
        name = 'church_translator' if source.stem == '__init__' else 'church_translator.' + source.stem
        actual = reader.extract(name) if name in reader.toc else None
        matches = actual is not None and actual == compile(source.read_text(encoding='utf-8'), actual.co_filename, 'exec', dont_inherit=True)
        modules.append({'module': name, 'matches': matches, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    config = load_config()
    secrets = [value.encode() for value in (config.gemini_api_key, config.openai_api_key, config.google_translate_api_key) if value]
    if config.google_application_credentials:
        key = json.loads(Path(config.google_application_credentials).read_text(encoding='utf-8')).get('private_key')
        if key:
            secrets.extend((key.encode(), json.dumps(key)[1:-1].encode()))
    archive = root / 'dist-app/ChurchTranslator-Windows.zip'
    forbidden, leaked = [], []
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if any(part in {'.appdata', 'sessions', 'credentials', 'settings.json', '.git'} for part in Path(name).parts) and not name.endswith('credentials/.gitkeep'):
                forbidden.append(name)
            if any(value in bundle.read(name) for value in secrets):
                leaked.append(name)
        template = bundle.read('ChurchTranslator/.env').decode('utf-8-sig')
        empty_keys = all(not line.split('=', 1)[1].strip() for line in template.splitlines() if line.startswith(('GEMINI_API_KEY=', 'OPENAI_API_KEY=', 'GOOGLE_TRANSLATE_API_KEY=')))
    report = {'ok': all(row['matches'] for row in modules) and not forbidden and not leaked and empty_keys,
              'modules': modules, 'forbidden_paths': forbidden, 'credential_matches': leaked,
              'template_keys_empty': empty_keys, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (root / 'build/improvement-release-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'modules'}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
