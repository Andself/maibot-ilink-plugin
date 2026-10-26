"""Build a credential-free source package using an explicit allowlist."""
import json
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
version = json.loads((root / '_manifest.json').read_text(encoding='utf-8'))['version']
files = ['_manifest.json', 'plugin.py', 'runtime_v2.py', 'transport.py', 'accounts.py',
         'authorization.py', 'media.py', 'images.py', 'nicknames.py', 'profiles.py', 'bindings.py', 'context_hooks.py', 'relay.py', 'reply_contexts.py', 'welcome.py', 'login.py', 'webui.json',
         'webui/nicknames.html', 'requirements.txt', 'README.md', 'test_adapter.py',
         'build_release.py', 'relay_memory.py', 'docs/assets/banner.svg']
target = root / 'dist' / ('anse.ilink-' + version + '.zip')
target.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, 'anse.ilink/' + name)
print('Built:', target)
print('Files:', len(files), '(explicit allowlist; no credentials or runtime config)')
