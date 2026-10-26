"""Build an allowlisted release and deploy using an ignored .env.local file."""
import argparse
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def target_directory():
    env_file = ROOT / '.env.local'
    if not env_file.is_file():
        raise ValueError('Copy .env.example to .env.local and set MAIBOT_PLUGINS_DIR first.')
    values = {}
    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator:
            raise ValueError('Invalid .env.local line: expected KEY=VALUE')
        values[key.strip()] = value.strip().strip('"').strip("'")
    value = values.get('MAIBOT_PLUGINS_DIR', '')
    if not value:
        raise ValueError('MAIBOT_PLUGINS_DIR is required in .env.local')
    parent = Path(value).expanduser()
    if not parent.is_absolute():
        parent = ROOT / parent
    parent = parent.resolve()
    if not parent.is_dir() or parent.name != 'plugins':
        raise ValueError('MAIBOT_PLUGINS_DIR must be an existing MaiBot plugins directory')
    target = parent / PLUGIN_ID
    if target.is_symlink() or (target.exists() and target.is_junction()):
        raise ValueError('Deployment target must not be a symlink or junction')
    target = target.resolve()
    if target == ROOT or target.is_relative_to(ROOT) or ROOT.is_relative_to(target):
        raise ValueError('Source and deployment directories must be separate')
    if (target / '.git').exists():
        raise ValueError('Deployment still contains a Git repository; archive it before deploying')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-only', action='store_true', help='Build ZIP without deployment or local configuration')
    parser.add_argument('--dry-run', action='store_true', help='Validate and show destination without writing files')
    args = parser.parse_args()
    manifest = json.loads((ROOT / '_manifest.json').read_text(encoding='utf-8'))
    global PLUGIN_ID
    PLUGIN_ID = manifest['id']
    specification = json.loads((ROOT / 'build-files.json').read_text(encoding='utf-8'))
    runtime_files = specification['runtime']
    package_files = runtime_files + specification['package_extra']
    for name in package_files:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not (ROOT / path).is_file():
            raise ValueError('Invalid or missing build input: ' + name)
        if (ROOT / path).is_symlink() or not (ROOT / path).resolve().is_relative_to(ROOT):
            raise ValueError('Build input must stay inside source directory: ' + name)
    target = None if args.package_only else target_directory()
    archive_path = ROOT / 'dist' / (PLUGIN_ID + '-' + manifest['version'] + '.zip')
    if args.dry_run:
        print('Package:', archive_path)
        print('Deploy:', target or '(package only)')
        return
    archive_path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in package_files:
            archive.write(ROOT / name, PLUGIN_ID + '/' + name)
    print('Built:', archive_path)
    if target is None:
        return
    target.mkdir(exist_ok=True)
    marker = target / '.build-managed.json'
    previous = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else []
    # Validate old ownership metadata before touching any deployed files.
    for name in set(previous) | set(runtime_files):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name == 'config.toml':
            raise ValueError('Unsafe managed file: ' + name)
        if not (target / path).resolve().is_relative_to(target):
            raise ValueError('Managed path escapes deployment: ' + name)
    with tempfile.TemporaryDirectory(prefix='.plugin-build-', dir=target.parent) as temporary:
        staging = Path(temporary)
        for name in runtime_files:
            staged = staging / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, staged)
        for name in runtime_files:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / name, destination)
        for name in set(previous) - set(runtime_files):
            (target / name).unlink(missing_ok=True)
        staged_marker = staging / '.build-managed.json'
        staged_marker.write_text(json.dumps(runtime_files, indent=2) + '\n', encoding='utf-8')
        os.replace(staged_marker, marker)
    print('Deployed:', target)
    print('Local config and runtime data preserved. Reload the plugin or restart MaiBot to activate.')


if __name__ == '__main__':
    main()
