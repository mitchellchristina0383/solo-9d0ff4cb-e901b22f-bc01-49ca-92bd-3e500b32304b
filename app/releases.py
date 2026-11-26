"""Background release pipeline.

A release verifies two per-architecture manifests (chunked blob checks),
makes sure both image configs carry the same
``org.opencontainers.image.revision`` and only then builds a multi-arch
index and moves the tag.

Verification runs on a worker thread without holding any lock, so the
live tag stays readable and further releases can be submitted while
layers are being hashed.  The only mutation is the final atomic tag
update, guarded by the digest the operator saw when selecting the
manifests; if verification or the index write fails, the live tag is
left untouched.
"""

from __future__ import annotations

import threading
import time
import uuid

from .oci import (
    DEFAULT_CHUNK_SIZE,
    MT_OCI_INDEX,
    REVISION_ANNOTATION,
    BlobVerificationError,
    OciError,
    OciLayout,
    TagConflictError,
    build_image_index,
    validate_digest,
)

EXPECTED_ARCHES = ("amd64", "arm64")

VERIFYING = "verifying"
COMMITTING = "committing"
DONE = "done"
FAILED = "failed"
CONFLICT = "conflict"
TERMINAL = {DONE, FAILED, CONFLICT}


class ReleaseJob:
    def __init__(self, tag: str, amd64: str, arm64: str, expected_tag_digest):
        self.id = uuid.uuid4().hex[:12]
        self.tag = tag
        self.amd64 = amd64
        self.arm64 = arm64
        self.expected_tag_digest = expected_tag_digest
        self.status = VERIFYING
        self.message = "校验清单与层"
        self.created = time.time()
        self.finished = None
        self.progress = {
            "files_total": 0,
            "files_done": 0,
            "current": None,
            "bytes_done": 0,
            "bytes_total": 0,
        }
        self.result = None
        self.lock = threading.Lock()

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "tag": self.tag,
                "amd64": self.amd64,
                "arm64": self.arm64,
                "expected_tag_digest": self.expected_tag_digest,
                "status": self.status,
                "message": self.message,
                "created": self.created,
                "finished": self.finished,
                "progress": dict(self.progress),
                "result": self.result,
            }


class ReleaseManager:
    def __init__(self, layout: OciLayout, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 pre_commit_hook=None):
        self.layout = layout
        self.chunk_size = chunk_size
        # Test/demo hook invoked after successful verification, just before
        # the index is written and the tag is moved.
        self.pre_commit_hook = pre_commit_hook
        self._jobs: dict[str, ReleaseJob] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # job registry
    # ------------------------------------------------------------------

    def submit(self, tag: str, amd64_digest: str, arm64_digest: str,
               expected_tag_digest) -> ReleaseJob:
        tag = (tag or "").strip()
        if not tag:
            raise OciError("缺少 tag")
        validate_digest(amd64_digest)
        validate_digest(arm64_digest)
        if amd64_digest == arm64_digest:
            raise OciError("两份清单摘要相同，无法构成多架构发布")
        for digest in (amd64_digest, arm64_digest):
            if self.layout.find_descriptor(digest) is None:
                raise OciError(f"清单不在 index.json 中: {digest}")
        job = ReleaseJob(tag, amd64_digest, arm64_digest, expected_tag_digest)
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,),
                         name=f"release-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str):
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self):
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created,
                          reverse=True)

    # ------------------------------------------------------------------
    # pipeline
    # ------------------------------------------------------------------

    def _set(self, job: ReleaseJob, status: str, message: str = None):
        with job.lock:
            job.status = status
            if message is not None:
                job.message = message
            if status in TERMINAL:
                job.finished = time.time()

    def _fail(self, job: ReleaseJob, message: str):
        self._set(job, FAILED, message)

    def _run(self, job: ReleaseJob):
        try:
            self._execute(job)
        except BlobVerificationError as exc:
            self._fail(job, f"校验失败: {exc}")
        except (OciError, ValueError, KeyError) as exc:
            self._fail(job, f"发布失败: {exc}")
        except Exception as exc:  # never take the live tag down on bugs
            self._fail(job, f"内部错误: {exc!r}")

    def _execute(self, job: ReleaseJob):
        # 1. Inspect both manifests: architecture from the image config and
        #    org.opencontainers.image.revision must match expectations.
        infos = {}
        for arch, digest in (("amd64", job.amd64), ("arm64", job.arm64)):
            descriptor = self.layout.find_descriptor(digest)
            if descriptor is None:
                raise OciError(f"清单不在 index.json 中: {digest}")
            info = self.layout.inspect_manifest(descriptor)
            actual = info["architecture"]
            if actual != arch:
                raise OciError(
                    f"清单 {digest} 的架构为 {actual!r}，与目标 {arch!r} 不符"
                )
            if not info["revision"]:
                raise OciError(
                    f"清单 {digest} 缺少 {REVISION_ANNOTATION}，无法确认源码提交"
                )
            infos[arch] = (descriptor, info)

        rev_amd64 = infos["amd64"][1]["revision"]
        rev_arm64 = infos["arm64"][1]["revision"]
        if rev_amd64 != rev_arm64:
            raise OciError(
                "两个架构不是同一提交: "
                f"amd64={rev_amd64} arm64={rev_arm64}"
            )
        revision = rev_amd64

        # 2. Chunked verification of each manifest and every blob it
        #    references (config + layers): size and digest.
        files = []
        for arch in EXPECTED_ARCHES:
            descriptor, info = infos[arch]
            manifest = info["manifest"]
            files.append(descriptor)
            files.append(manifest["config"])
            files.extend(manifest.get("layers", []))
        with job.lock:
            job.progress["files_total"] = len(files)
        for index, fdesc in enumerate(files):
            with job.lock:
                job.progress.update(
                    current=fdesc["digest"],
                    bytes_done=0,
                    bytes_total=fdesc.get("size") or 0,
                )

            def on_chunk(done, total, job=job):
                with job.lock:
                    job.progress.update(bytes_done=done, bytes_total=total)

            self.layout.verify_blob(
                fdesc["digest"], fdesc.get("size"),
                chunk_size=self.chunk_size, progress=on_chunk,
            )
            with job.lock:
                job.progress["files_done"] = index + 1

        # 3. Both arches verified and from the same commit: build the
        #    multi-arch index, store it, then move the tag.
        self._set(job, COMMITTING, "生成多架构索引并更新标签")
        if self.pre_commit_hook:
            self.pre_commit_hook(job)
        index_obj, index_bytes = build_image_index(
            [
                {
                    "descriptor": infos[arch][0],
                    "platform": {
                        "architecture": arch,
                        "os": infos[arch][1]["os"] or "linux",
                    },
                }
                for arch in EXPECTED_ARCHES
            ],
            revision,
        )
        index_digest = self.layout.write_blob(index_bytes)
        tag_descriptor = {
            "mediaType": MT_OCI_INDEX,
            "digest": index_digest,
            "size": len(index_bytes),
            "annotations": {REVISION_ANNOTATION: revision},
        }
        try:
            self.layout.update_tag(job.tag, tag_descriptor,
                                   job.expected_tag_digest)
        except TagConflictError as exc:
            self._set(
                job, CONFLICT,
                f"标签 {job.tag} 的摘要已变为 "
                f"{exc.current or '(不存在)'}，与选择清单时所见的 "
                f"{exc.expected or '(不存在)'} 不同，请刷新后重试",
            )
            return
        with job.lock:
            job.result = {
                "index_digest": index_digest,
                "revision": revision,
                "manifests": {"amd64": job.amd64, "arm64": job.arm64},
            }
        self._set(job, DONE, f"已发布 {job.tag} -> {index_digest}")
