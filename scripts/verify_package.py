"""Read-only verification of the unpacked delivery manifest. Standard library only."""
from pathlib import Path
import hashlib, json

root = Path(__file__).resolve().parents[1]
manifest_path = root / ('portable_manifest.json' if (root / 'portable_manifest.json').exists() else 'delivery_manifest.json')
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
errors = []
for item in manifest['files']:
    path = (root / item['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        errors.append({'path': item['path'], 'error': 'missing or outside root'})
    elif hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
        errors.append({'path': item['path'], 'error': 'hash mismatch'})
print(json.dumps({'version': manifest['packageVersion'], 'checked': len(manifest['files']), 'ok': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
raise SystemExit(bool(errors))
