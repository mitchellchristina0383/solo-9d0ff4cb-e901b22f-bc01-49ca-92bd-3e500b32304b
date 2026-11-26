"""Deterministic sample OCI Image Layout used by the demo and the tests.

Produces a ``demo`` repository with:

* ``demo:1.0.0-amd64`` / ``demo:1.0.0-arm64`` — revision A, and a
  multi-arch index ``demo:latest`` pointing at them (the live tag)
* ``demo:1.1.0-amd64`` / ``demo:1.1.0-arm64`` — revision B, the happy-path
  release candidates
* ``demo:1.2.0-amd64`` / ``demo:1.2.0-arm64`` — *different* revisions, to
  demonstrate the same-commit check
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile

from .oci import (
    LAYOUT_VERSION,
    MT_OCI_INDEX,
    MT_OCI_MANIFEST,
    REF_NAME_ANNOTATION,
    REVISION_ANNOTATION,
    OciLayout,
    build_image_index,
    canonical_json,
)

CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"

REVISIONS = {
    "v1.0.0": hashlib.sha1(b"demo v1.0.0").hexdigest(),
    "v1.1.0": hashlib.sha1(b"demo v1.1.0").hexdigest(),
    "v1.2.0-amd64": hashlib.sha1(b"demo v1.2.0 amd64").hexdigest(),
    "v1.2.0-arm64": hashlib.sha1(b"demo v1.2.0 arm64").hexdigest(),
}

BIG_LAYER_SIZE = 1536 * 1024  # 1.5 MiB, so chunked hashing has real work


def _tar_layer(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1700000000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fill(seed: str, size: int) -> bytes:
    block = hashlib.sha256(seed.encode()).digest()
    return (block * (size // len(block) + 1))[:size]


def _write_image(layout: OciLayout, arch: str, revision: str, version: str):
    """Write layer/config/manifest blobs for one arch; return the manifest
    descriptor (without any tag annotation)."""
    payloads = [
        _tar_layer({
            "app/version.txt": f"{version} {revision}\n".encode(),
            "app/arch.txt": f"{arch}\n".encode(),
        }),
        _tar_layer({"app/blob.bin": _fill(f"{version}-{arch}", BIG_LAYER_SIZE)}),
    ]
    layer_descriptors, diff_ids = [], []
    for data in payloads:
        digest = layout.write_blob(data)
        layer_descriptors.append(
            {"mediaType": LAYER_MEDIA_TYPE, "digest": digest, "size": len(data)}
        )
        diff_ids.append("sha256:" + hashlib.sha256(data).hexdigest())

    config = {
        "created": "2026-01-01T00:00:00Z",
        "architecture": arch,
        "os": "linux",
        "config": {
            "Labels": {
                REVISION_ANNOTATION: revision,
                "org.opencontainers.image.version": version,
            }
        },
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [
            {"created": "2026-01-01T00:00:00Z", "created_by": "sampledata"}
        ],
    }
    config_bytes = canonical_json(config)
    config_digest = layout.write_blob(config_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": MT_OCI_MANIFEST,
        "config": {
            "mediaType": CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": layer_descriptors,
        "annotations": {REVISION_ANNOTATION: revision},
    }
    manifest_bytes = canonical_json(manifest)
    manifest_digest = layout.write_blob(manifest_bytes)
    return {
        "mediaType": MT_OCI_MANIFEST,
        "digest": manifest_digest,
        "size": len(manifest_bytes),
    }


def build_sample_layout(path) -> dict:
    """Create the sample layout in ``path`` (must be empty/missing)."""
    os.makedirs(path, exist_ok=True)
    if os.listdir(path):
        raise RuntimeError(f"目录非空，拒绝覆盖: {path}")
    with open(os.path.join(path, "oci-layout"), "w") as fh:
        json.dump({"imageLayoutVersion": LAYOUT_VERSION}, fh)
    os.makedirs(os.path.join(path, "blobs", "sha256"))
    # OciLayout requires index.json to exist; start with an empty one.
    with open(os.path.join(path, "index.json"), "w") as fh:
        json.dump({"schemaVersion": 2, "manifests": []}, fh)
    layout = OciLayout(path)

    entries = []
    images = {}
    versions = {
        "1.0.0": {"amd64": REVISIONS["v1.0.0"], "arm64": REVISIONS["v1.0.0"]},
        "1.1.0": {"amd64": REVISIONS["v1.1.0"], "arm64": REVISIONS["v1.1.0"]},
        "1.2.0": {
            "amd64": REVISIONS["v1.2.0-amd64"],
            "arm64": REVISIONS["v1.2.0-arm64"],  # 故意与 amd64 不同提交
        },
    }
    for version, arches in versions.items():
        images[version] = {}
        for arch, revision in arches.items():
            descriptor = _write_image(layout, arch, revision, version)
            images[version][arch] = descriptor
            entry = dict(descriptor)
            entry["annotations"] = {
                REF_NAME_ANNOTATION: f"demo:{version}-{arch}",
                REVISION_ANNOTATION: revision,
            }
            entries.append(entry)

    # Live tag: multi-arch index of v1.0.0.
    index_obj, index_bytes = build_image_index(
        [
            {"descriptor": images["1.0.0"][arch],
             "platform": {"architecture": arch, "os": "linux"}}
            for arch in ("amd64", "arm64")
        ],
        REVISIONS["v1.0.0"],
    )
    index_digest = layout.write_blob(index_bytes)
    entries.append({
        "mediaType": MT_OCI_INDEX,
        "digest": index_digest,
        "size": len(index_bytes),
        "annotations": {
            REF_NAME_ANNOTATION: "demo:latest",
            REVISION_ANNOTATION: REVISIONS["v1.0.0"],
        },
    })
    layout.write_index({"schemaVersion": 2, "manifests": entries})
    return {"revisions": REVISIONS, "images": images, "latest": index_digest}
