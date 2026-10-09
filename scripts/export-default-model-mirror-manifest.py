#!/usr/bin/env python3
"""Export the pinned initialization models without importing client runtime code.

The exporter is the release contract for the model mirror. A default-model
change in the client therefore changes the next sync input automatically.
Python 3.9 compatible; it never reads user data or model contents.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path


def constants(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [item.id for item in node.targets if isinstance(item, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        for name in targets:
            if name in names:
                values[name] = ast.literal_eval(node.value)
    missing = set(names) - set(values)
    if missing:
        raise ValueError("missing pinned model constants in %s: %s" % (path, sorted(missing)))
    return values


def build_manifest(root):
    capture = constants(root / "ai-sidecar" / "initialization_manager.py", (
        "_CAPTURE_MODEL_NAME", "_CAPTURE_MODEL_REPOSITORY", "_CAPTURE_MODEL_TAG",
        "_CAPTURE_MODEL_BLOBS",
    ))
    embedding = constants(root / "ai-sidecar" / "embedding" / "model_sources.py", (
        "MODEL_REPO_ID", "MODEL_REVISION", "MODEL_FILES", "MODEL_FILE_SHA256",
    ))
    licenses = json.loads((root / "shared" / "model-mirror" / "licenses.json").read_text(encoding="utf-8"))
    version = json.loads((root / "desktop-ui" / "package.json").read_text(encoding="utf-8"))["version"]
    assets = []
    repository = capture["_CAPTURE_MODEL_REPOSITORY"]
    tag = capture["_CAPTURE_MODEL_TAG"]
    capture_id = capture["_CAPTURE_MODEL_NAME"]
    for digest, size, kind in capture["_CAPTURE_MODEL_BLOBS"]:
        if kind not in ("config", "model", "license", "params") or len(digest) != 64 or size <= 0:
            raise ValueError("invalid pinned capture model blob")
        assets.append({
            "family": "capture",
            "model_id": capture_id,
            "key": "downloads/models/ollama/%s/%s/blobs/sha256:%s" % (repository, tag, digest),
            "source_url": "https://registry.ollama.ai/v2/library/%s/blobs/sha256:%s" % (repository, digest),
            "local_relative_path": "sha256-" + digest,
            "sha256": digest,
            "size_bytes": size,
        })
    repo_id = embedding["MODEL_REPO_ID"]
    revision = embedding["MODEL_REVISION"]
    for name in embedding["MODEL_FILES"]:
        digest = embedding["MODEL_FILE_SHA256"].get(name)
        if not digest or len(digest) != 64 or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("invalid pinned embedding model file: " + name)
        assets.append({
            "family": "embedding",
            "model_id": repo_id,
            "key": "downloads/models/%s/%s/%s" % (repo_id, revision, name),
            "source_url": "https://huggingface.co/%s/resolve/%s/%s" % (repo_id, revision, name),
            "local_relative_path": name,
            "sha256": digest,
            "size_bytes": None,
        })
    notice_relative_path = licenses[repo_id].get("notice_local_path")
    if not notice_relative_path or ".." in Path(notice_relative_path).parts:
        raise ValueError("embedding model license notice is missing")
    notice_path = root / notice_relative_path
    notice_bytes = notice_path.read_bytes()
    assets.append({
        "family": "notice",
        "model_id": repo_id,
        "key": "downloads/models/%s/%s/LICENSE" % (repo_id, revision),
        "source_url": licenses[repo_id]["license_source"],
        "local_relative_path": notice_relative_path,
        "sha256": hashlib.sha256(notice_bytes).hexdigest(),
        "size_bytes": len(notice_bytes),
    })
    model_ids = {item["model_id"] for item in assets}
    if not model_ids.issubset(licenses):
        raise ValueError("default model lacks redistribution evidence: %s" % sorted(model_ids - set(licenses)))
    if any(
        not licenses[model_id].get("redistribution_permitted_by_license")
        or not licenses[model_id].get("license")
        or not str(licenses[model_id].get("license_source", "")).startswith("https://")
        for model_id in model_ids
    ):
        raise ValueError("default model redistribution evidence is incomplete")
    return {
        "schema_version": "memorybread.model-mirror.v1",
        "client_version": version,
        "models": {model_id: licenses[model_id] for model_id in sorted(model_ids)},
        "assets": assets,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(build_manifest(args.root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
