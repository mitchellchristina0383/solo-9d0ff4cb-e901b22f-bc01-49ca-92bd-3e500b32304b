"""End-to-end tests for the release page.

Covers the happy path plus the required failure modes:
missing/corrupt layers, revision mismatch, wrong architecture and
interleaved releases racing for the same tag.
"""

import os
import threading
import time

import pytest

from app.oci import OciLayout
from app.sampledata import REVISIONS, build_sample_layout
from app.server import create_app

TERMINAL = {"done", "failed", "conflict"}


@pytest.fixture()
def layout_path(tmp_path):
    path = tmp_path / "layout"
    build_sample_layout(str(path))
    return str(path)


@pytest.fixture()
def client(layout_path):
    # Tiny chunks so the chunked verification loop is really exercised.
    return create_app(layout_path, chunk_size=64).test_client()


def post_release(client, tag, amd64, arm64, expected):
    return client.post("/api/releases", json={
        "tag": tag, "amd64": amd64, "arm64": arm64,
        "expected_tag_digest": expected,
    })


def wait_job(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/releases/{job_id}").get_json()["job"]
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} still running")


def digest_by_tag(client):
    """Map of ref name -> manifest digest, from the candidate list."""
    out = {}
    for cand in client.get("/api/overview").get_json()["candidates"]:
        for tag in cand["tags"]:
            out[tag] = cand["digest"]
    return out


def tag_digest(client, name="demo:latest"):
    return client.get(f"/api/tags/{name}").get_json()["descriptor"]["digest"]


def layer_digests(layout_path, manifest_digest):
    layout = OciLayout(layout_path)
    manifest = layout.read_json_blob(layout.find_descriptor(manifest_digest))
    return [l["digest"] for l in manifest["layers"]]


# ---------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------

def test_index_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "多架构镜像发布" in resp.get_data(as_text=True)


def test_overview_lists_tags_and_candidates(client):
    data = client.get("/api/overview").get_json()
    tags = {t["tag"]: t for t in data["tags"]}
    assert tags["demo:latest"]["kind"] == "index"
    assert set(tags["demo:latest"]["arches"]) == {"amd64", "arm64"}
    assert tags["demo:latest"]["revisions"] == [REVISIONS["v1.0.0"]]
    by_arch = {}
    for cand in data["candidates"]:
        by_arch.setdefault(cand.get("architecture"), []).append(cand)
    assert set(by_arch) == {"amd64", "arm64"}
    revisions = {c.get("revision") for c in data["candidates"]}
    assert REVISIONS["v1.1.0"] in revisions


def test_tag_detail_shows_index_and_both_manifests(client):
    detail = client.get("/api/tags/demo:latest").get_json()
    assert detail["index"]["mediaType"] == "application/vnd.oci.image.index.v1+json"
    by_arch = {m["platform"]["architecture"]: m for m in detail["manifests"]}
    assert set(by_arch) == {"amd64", "arm64"}
    assert all(m["digest"].startswith("sha256:") for m in by_arch.values())
    assert {m["revision"] for m in by_arch.values()} == {REVISIONS["v1.0.0"]}


# ---------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------

def test_release_same_commit_updates_tag(client, layout_path):
    digests = digest_by_tag(client)
    old = tag_digest(client)
    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                        old)
    assert resp.status_code == 202
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "done", job
    assert job["result"]["revision"] == REVISIONS["v1.1.0"]
    assert job["progress"]["files_done"] == job["progress"]["files_total"] > 0

    detail = client.get("/api/tags/demo:latest").get_json()
    assert detail["descriptor"]["digest"] == job["result"]["index_digest"]
    by_arch = {m["platform"]["architecture"]: m for m in detail["index"]["manifests"]}
    assert by_arch["amd64"]["digest"] == digests["demo:1.1.0-amd64"]
    assert by_arch["arm64"]["digest"] == digests["demo:1.1.0-arm64"]

    # The written index blob itself verifies (size + digest).
    layout = OciLayout(layout_path)
    layout.verify_blob(job["result"]["index_digest"], detail["descriptor"]["size"])

    # Per-arch tags and other entries are untouched.
    after = digest_by_tag(client)
    assert after["demo:1.0.0-amd64"] == digests["demo:1.0.0-amd64"]
    assert after["demo:1.1.0-arm64"] == digests["demo:1.1.0-arm64"]


# ---------------------------------------------------------------------
# verification failures keep the live tag
# ---------------------------------------------------------------------

def test_revision_mismatch_keeps_tag(client):
    digests = digest_by_tag(client)
    old = tag_digest(client)
    resp = post_release(client, "demo:latest",
                        digests["demo:1.2.0-amd64"], digests["demo:1.2.0-arm64"],
                        old)
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "failed"
    assert "同一提交" in job["message"]
    assert tag_digest(client) == old


def test_wrong_architecture_keeps_tag(client):
    digests = digest_by_tag(client)
    old = tag_digest(client)
    # amd64/arm64 swapped on purpose
    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-arm64"], digests["demo:1.1.0-amd64"],
                        old)
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "failed"
    assert "架构" in job["message"]
    assert tag_digest(client) == old


def test_missing_layer_keeps_tag(client, layout_path):
    digests = digest_by_tag(client)
    victim = layer_digests(layout_path, digests["demo:1.1.0-arm64"])[0]
    os.remove(OciLayout(layout_path).blob_path(victim))

    old = tag_digest(client)
    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                        old)
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "failed"
    assert "missing" in job["message"] and victim in job["message"]
    assert tag_digest(client) == old


def test_corrupt_layer_keeps_tag(client, layout_path):
    digests = digest_by_tag(client)
    layout = OciLayout(layout_path)
    victim = layer_digests(layout_path, digests["demo:1.1.0-amd64"])[-1]
    path = layout.blob_path(victim)
    with open(path, "r+b") as fh:  # flip one byte, keep the size
        fh.seek(100)
        fh.write(bytes([fh.read(1)[0] ^ 0xFF]))

    old = tag_digest(client)
    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                        old)
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "failed"
    assert "digest_mismatch" in job["message"]
    assert tag_digest(client) == old


def test_unknown_manifest_rejected(client):
    resp = post_release(client, "demo:latest",
                        "sha256:" + "0" * 64, "sha256:" + "1" * 64, "")
    assert resp.status_code == 400
    assert "不在 index.json" in resp.get_json()["error"]


# ---------------------------------------------------------------------
# interleaved releases / optimistic concurrency
# ---------------------------------------------------------------------

def test_interleaved_release_conflicts_and_live_tag_stays_readable(layout_path):
    """Job A verifies, then stalls before commit; meanwhile the tag moves
    (a competing release). A must end in CONFLICT and the tag must keep the
    competing value. While A is verifying, the live tag stays queryable and
    other releases commit normally."""
    entered_gate = threading.Event()
    release_gate = threading.Event()

    def hook(job):
        if job.tag == "demo:latest":
            entered_gate.set()
            assert release_gate.wait(15)

    app = create_app(layout_path, chunk_size=1024, pre_commit_hook=hook)
    client = app.test_client()
    digests = digest_by_tag(client)
    old = tag_digest(client)

    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                        old)
    assert resp.status_code == 202
    job_a = resp.get_json()["job"]["id"]
    assert entered_gate.wait(15), "job A never reached the commit gate"

    # 验层期间：现役标签可查
    assert tag_digest(client) == old
    # 其他发布照常提交（新标签，expected="" 表示要求此前不存在）
    resp_b = post_release(client, "demo:next",
                          digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                          "")
    assert resp_b.status_code == 202
    job_b = wait_job(client, resp_b.get_json()["job"]["id"])
    assert job_b["status"] == "done"
    assert client.get("/api/tags/demo:next").status_code == 200

    # 另一人抢先发布了 demo:latest（这里直接把标签拨到 1.0.0-amd64 模拟）
    layout = OciLayout(layout_path)
    competing = layout.find_descriptor(digests["demo:1.0.0-amd64"])
    layout.update_tag("demo:latest", competing, expected_digest=old)

    release_gate.set()
    job_a = wait_job(client, job_a)
    assert job_a["status"] == "conflict"
    assert "刷新" in job_a["message"]
    # 标签保持为抢先提交的值，A 的索引没有生效
    assert tag_digest(client) == competing["digest"]


def test_stale_expected_digest_rejected_even_without_race(client):
    digests = digest_by_tag(client)
    old = tag_digest(client)
    stale = "sha256:" + "0" * 64
    resp = post_release(client, "demo:latest",
                        digests["demo:1.1.0-amd64"], digests["demo:1.1.0-arm64"],
                        stale)
    job = wait_job(client, resp.get_json()["job"]["id"])
    assert job["status"] == "conflict"
    assert tag_digest(client) == old
