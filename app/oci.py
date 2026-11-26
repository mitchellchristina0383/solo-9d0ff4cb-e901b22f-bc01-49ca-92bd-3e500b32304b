"""Access to a local OCI Image Layout directory.

Only the subset of the OCI Image spec that the release page needs:

* read ``index.json`` and resolve tags (``org.opencontainers.image.ref.name``)
* read blobs and verify digest/size while streaming them in chunks
* write new blobs atomically (content addressed, hence idempotent)
* move a tag in ``index.json`` atomically, guarded by an optimistic check
  on the digest that was current when the operator picked the manifests
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading

LAYOUT_VERSION = "1.0.0"
REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
REVISION_ANNOTATION = "org.opencontainers.image.revision"

MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MT_DOCKER_INDEX = "application/vnd.docker.manifest.list.v2+json"

MANIFEST_MEDIA_TYPES = {MT_OCI_MANIFEST, MT_DOCKER_MANIFEST}
INDEX_MEDIA_TYPES = {MT_OCI_INDEX, MT_DOCKER_INDEX}

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB per read while hashing blobs

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OciError(Exception):
    """Base error for OCI layout problems."""


class BlobVerificationError(OciError):
    """A blob is missing or does not match its descriptor."""

    def __init__(self, kind: str, digest: str, detail: str):
        self.kind = kind  # "missing" | "size_mismatch" | "digest_mismatch"
        self.digest = digest
        self.detail = detail
        super().__init__(f"{kind} {digest}: {detail}")


class TagConflictError(OciError):
    """The tag moved between manifest selection and commit."""

    def __init__(self, ref_name: str, expected, current):
        self.ref_name = ref_name
        self.expected = expected
        self.current = current
        super().__init__(
            f"tag {ref_name!r} changed: expected {expected!r}, found {current!r}"
        )


def validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise OciError(f"invalid digest: {digest!r}")
    return digest


def canonical_json(obj) -> bytes:
    """Deterministic JSON encoding used for every object we store."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_image_index(manifests, revision: str):
    """Build a multi-arch image index.

    ``manifests`` is a list of ``{"descriptor": ..., "platform": ...}``.
    Returns ``(index_dict, canonical_bytes)``.
    """
    entries = []
    for item in manifests:
        desc = item["descriptor"]
        entries.append(
            {
                "mediaType": desc.get("mediaType", MT_OCI_MANIFEST),
                "digest": desc["digest"],
                "size": desc["size"],
                "platform": item["platform"],
            }
        )
    index = {
        "schemaVersion": 2,
        "mediaType": MT_OCI_INDEX,
        "manifests": entries,
        "annotations": {REVISION_ANNOTATION: revision},
    }
    return index, canonical_json(index)


class OciLayout:
    """A local OCI Image Layout directory."""

    def __init__(self, path: str):
        self.path = os.fspath(path)
        if not os.path.isfile(os.path.join(self.path, "index.json")):
            raise OciError(f"not an OCI layout (index.json missing): {self.path}")
        self._index_lock = threading.RLock()

    # ------------------------------------------------------------------
    # blobs
    # ------------------------------------------------------------------

    def blob_path(self, digest: str) -> str:
        validate_digest(digest)
        algo, hexd = digest.split(":", 1)
        return os.path.join(self.path, "blobs", algo, hexd)

    def verify_blob(self, digest, expected_size=None,
                    chunk_size=DEFAULT_CHUNK_SIZE, progress=None) -> int:
        """Stream the blob in chunks, verifying size and digest.

        ``progress(done_bytes, total_bytes)`` is called after every chunk.
        Returns the blob size. Raises :class:`BlobVerificationError`.
        """
        path = self.blob_path(digest)
        if not os.path.isfile(path):
            raise BlobVerificationError("missing", digest, "blob 文件不存在")
        algo, _ = digest.split(":", 1)
        hasher = hashlib.new(algo)
        total = os.path.getsize(path)
        done = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if expected_size is not None and done != expected_size:
            raise BlobVerificationError(
                "size_mismatch", digest,
                f"描述大小 {expected_size} != 实际 {done}",
            )
        actual = f"{algo}:{hasher.hexdigest()}"
        if actual != digest:
            raise BlobVerificationError(
                "digest_mismatch", digest, f"实际摘要 {actual}"
            )
        return done

    def read_blob(self, digest, expected_size=None) -> bytes:
        self.verify_blob(digest, expected_size)
        with open(self.blob_path(digest), "rb") as fh:
            return fh.read()

    def read_json_blob(self, descriptor) -> dict:
        data = self.read_blob(descriptor["digest"], descriptor.get("size"))
        return json.loads(data.decode("utf-8"))

    def write_blob(self, data: bytes) -> str:
        """Store ``data`` content-addressed; returns its digest.

        Writes go to a unique temp file followed by ``os.replace`` so
        concurrent readers never see a partial blob, and writing the same
        content twice is a no-op.
        """
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        path = self.blob_path(digest)
        if os.path.isfile(path):
            return digest
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return digest

    # ------------------------------------------------------------------
    # index.json / tags
    # ------------------------------------------------------------------

    def read_index(self) -> dict:
        with open(os.path.join(self.path, "index.json"), "rb") as fh:
            return json.load(fh)

    def write_index(self, index: dict) -> None:
        """Atomically replace index.json (temp file + rename)."""
        data = canonical_json(index)
        tmp = os.path.join(self.path, f".index.json.tmp-{os.getpid()}-{threading.get_ident()}")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(self.path, "index.json"))

    def tags(self) -> dict:
        """Map of ref name -> descriptor for every tagged index entry."""
        result = {}
        for desc in self.read_index().get("manifests", []):
            ref = (desc.get("annotations") or {}).get(REF_NAME_ANNOTATION)
            if ref:
                result[ref] = desc
        return result

    def manifest_descriptors(self) -> list:
        """All image-manifest descriptors listed in index.json."""
        return [
            d for d in self.read_index().get("manifests", [])
            if d.get("mediaType") in MANIFEST_MEDIA_TYPES
        ]

    def find_descriptor(self, digest: str):
        for desc in self.read_index().get("manifests", []):
            if desc.get("digest") == digest:
                return desc
        return None

    def update_tag(self, ref_name: str, descriptor: dict, expected_digest):
        """Atomically point ``ref_name`` at ``descriptor``.

        ``expected_digest`` is the digest the tag had when the operator
        selected the manifests (``None`` means "the tag must not exist").
        Raises :class:`TagConflictError` if the tag moved in the meantime;
        index.json is left untouched in that case.
        """
        with self._index_lock:
            index = self.read_index()
            current = None
            kept = []
            for desc in index.get("manifests", []):
                ref = (desc.get("annotations") or {}).get(REF_NAME_ANNOTATION)
                if ref == ref_name:
                    if current is None:
                        current = desc.get("digest")
                    continue  # drop old entry (and duplicates); re-added below
                kept.append(desc)
            if current != expected_digest:
                raise TagConflictError(ref_name, expected_digest, current)
            entry = dict(descriptor)
            annotations = dict(entry.get("annotations") or {})
            annotations[REF_NAME_ANNOTATION] = ref_name
            entry["annotations"] = annotations
            kept.append(entry)
            index["manifests"] = kept
            self.write_index(index)
            return entry

    # ------------------------------------------------------------------
    # manifest inspection / verification
    # ------------------------------------------------------------------

    def inspect_manifest(self, descriptor) -> dict:
        """Verify manifest+config blobs and extract arch/os/revision.

        The revision is read from the image config label
        ``org.opencontainers.image.revision``, falling back to the manifest
        annotation of the same name.
        """
        manifest = self.read_json_blob(descriptor)
        config = self.read_json_blob(manifest.get("config") or {})
        labels = (config.get("config") or {}).get("Labels") or {}
        annotations = manifest.get("annotations") or {}
        return {
            "manifest": manifest,
            "config": config,
            "architecture": config.get("architecture"),
            "os": config.get("os"),
            "revision": labels.get(REVISION_ANNOTATION)
            or annotations.get(REVISION_ANNOTATION),
        }
