import importlib.util
import json
from pathlib import Path

import pytest


def _exporter():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'export-default-model-mirror-manifest.py'
    spec = importlib.util.spec_from_file_location('model_mirror_manifest', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_model_switch_changes_mirror_keys_and_requires_license_evidence(tmp_path):
    root = tmp_path
    sidecar = root / 'ai-sidecar'
    (sidecar / 'embedding').mkdir(parents=True)
    (root / 'desktop-ui').mkdir()
    notices = root / 'shared' / 'model-mirror'
    notices.mkdir(parents=True)
    (root / 'desktop-ui' / 'package.json').write_text('{"version":"1.0.0"}')
    (sidecar / 'initialization_manager.py').write_text(
        '_CAPTURE_MODEL_NAME="new:1b"\n'
        '_CAPTURE_MODEL_REPOSITORY="new"\n'
        '_CAPTURE_MODEL_TAG="1b"\n'
        '_CAPTURE_MODEL_BLOBS=(("' + 'a' * 64 + '", 7, "model"),)\n')
    (sidecar / 'embedding' / 'model_sources.py').write_text(
        'MODEL_REPO_ID="Org/vector"\n'
        'MODEL_REVISION="revision"\n'
        'MODEL_FILES=("model.safetensors",)\n'
        'MODEL_FILE_SHA256={"model.safetensors":"' + 'b' * 64 + '"}\n')
    (notices / 'LICENSE').write_text('MIT license notice')
    licenses = {'new:1b': {'redistribution_permitted_by_license': True,
                           'license': 'Apache-2.0', 'license_source': 'https://example.invalid/license'},
                'Org/vector': {'redistribution_permitted_by_license': True,
                               'license': 'MIT',
                               'notice_local_path': 'shared/model-mirror/LICENSE',
                               'license_source': 'https://example.invalid/license'}}
    (notices / 'licenses.json').write_text(json.dumps(licenses))

    manifest = _exporter().build_manifest(root)
    keys = [asset['key'] for asset in manifest['assets']]
    assert any('/ollama/new/1b/' in key for key in keys)
    assert any('/Org/vector/revision/model.safetensors' in key for key in keys)
    assert manifest['client_version'] == '1.0.0'

    licenses.pop('new:1b')
    (notices / 'licenses.json').write_text(json.dumps(licenses))
    with pytest.raises(ValueError, match='redistribution evidence'):
        _exporter().build_manifest(root)
