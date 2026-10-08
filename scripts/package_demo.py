"""Build source and optional offline Windows archives; check all hashes and ZIP CRCs."""
from pathlib import Path
import argparse, hashlib, json, zipfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--portable', action='store_true', help='also include already-prepared _runtime binaries')
args = parser.parse_args()
version = '1.5.0'
prefix = 'TangdaoBay_3D_Demo'
excluded_dirs = {'__pycache__', 'runs', 'qa', '.venv', '.git', 'downloads'}
excluded_names = {'delivery_manifest.json', 'portable_manifest.json', 'package_verification.json', 'three-0.180.0.tgz'}
def allowed(p):
    parts = p.relative_to(root).parts
    return (p.is_file() and not set(parts) & excluded_dirs and p.name not in excluded_names
            and not p.name.startswith('~$') and p.suffix not in ('.pyc', '.pyo')
            and not any(part.startswith('.staging-') or '.partial.' in part for part in parts))
core = sorted(p for p in root.rglob('*') if allowed(p) and '_runtime' not in p.relative_to(root).parts)
runtime = sorted(p for p in (root / '_runtime').rglob('*') if allowed(p)) if args.portable else []
if args.portable and not all((root / p).is_file() for p in ('_runtime/python-3.12.10/python.exe', '_runtime/sumo-1.25.0/bin/sumo.exe', '_runtime/numpy-1.26.4-cp312/numpy/__init__.py')):
    raise SystemExit('Prepare portable dependencies before packaging: Start_Demo.ps1 -PrepareOnly -PortableOnly')
def inventory(files):
    return [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
manifest = {'packageVersion': version, 'runtimeMode': 'auto-prepare missing dependencies',
            'files': inventory(core)}
manifest_path = root / 'delivery_manifest.json'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
results = []
for kind, files in [('Source', core), *([('Windows_x64', core + runtime)] if args.portable else [])]:
    output = root.parent / f'TangdaoBay_3D_Demo_v{version.replace(".", "_")}_{kind}.zip'
    if output.exists(): raise SystemExit(f'Archive exists; choose a new version/name: {output.name}')
    check = manifest if kind == 'Source' else {'packageVersion': version, 'runtimeMode': 'offline-windows-x64', 'files': inventory(files + [manifest_path])}
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files + [manifest_path]: z.write(p, f'{prefix}/{p.relative_to(root).as_posix()}')
        if kind != 'Source': z.writestr(f'{prefix}/portable_manifest.json', json.dumps(check, ensure_ascii=False, indent=2).encode())
    with zipfile.ZipFile(output) as z:
        assert z.testzip() is None
        for item in check['files']:
            assert hashlib.sha256(z.read(f"{prefix}/{item['path']}")).hexdigest() == item['sha256'], item['path']
        count = len(z.infolist())
    result = {'version': version, 'archive': output.name, 'files': count, 'bytes': output.stat().st_size,
              'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'crcAndAllManifestHashesVerified': True}
    results.append(result)
    output.with_suffix('.sha256.txt').write_text(result['sha256'] + '  ' + output.name + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
(root / 'evidence/delivery/package_verification.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
