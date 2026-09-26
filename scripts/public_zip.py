"""Package binaries/resources, excluding installed local credentials."""
import pathlib
import sys
import zipfile

root, output = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if not path.is_file() or rel.parts[0] == 'credentials' or path.name == '.env' or '__pycache__' in rel.parts:
            continue
        archive.write(path, str(pathlib.Path('ChurchTranslator') / rel))
    archive.write(root / '.env.example', 'ChurchTranslator/.env')
    archive.writestr('ChurchTranslator/credentials/.gitkeep', '')
