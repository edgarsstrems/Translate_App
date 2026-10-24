"""Package binaries/resources, excluding installed local credentials."""
import pathlib
import sys
import zipfile

root, output = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
allowed_roots = {'_internal', 'assets', 'scripts', 'verification', 'ChurchTranslator.exe', 'README.md',
                 'AUDIT_REPORT.md', 'RELEASE_REPORT_2026-10-17.md', 'RELEASE_REPORT_2026-10-20.md',
                 'RELEASE_REPORT_2026-10-24.md', 'RELEASE_REPORT_2026-10-24_CONTEXT_FIX.md', '.env.example', 'glossary.json', 'icon.png'}
private_names = {'sermon-2026-10-18.json', 'SERMON_ANALYSIS_2026-10-18.md'}
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if (not path.is_file() or rel.parts[0] not in allowed_roots or path.name == '.env'
                or '__pycache__' in rel.parts or 'private-audit' in rel.parts or path.name in private_names):
            continue
        archive.write(path, str(pathlib.Path('ChurchTranslator') / rel))
    archive.write(root / '.env.example', 'ChurchTranslator/.env')
    archive.writestr('ChurchTranslator/credentials/.gitkeep', '')
