"""Flask release page for promoting verified multi-arch images."""

from __future__ import annotations

import argparse

from flask import Flask, jsonify, render_template, request

from .oci import (
    DEFAULT_CHUNK_SIZE,
    INDEX_MEDIA_TYPES,
    MANIFEST_MEDIA_TYPES,
    OciError,
    OciLayout,
)
from .releases import ReleaseManager


def create_app(layout_path, chunk_size=DEFAULT_CHUNK_SIZE, pre_commit_hook=None):
    app = Flask(__name__)
    layout = OciLayout(layout_path)
    manager = ReleaseManager(layout, chunk_size=chunk_size,
                             pre_commit_hook=pre_commit_hook)
    app.extensions["oci_layout"] = layout
    app.extensions["release_manager"] = manager

    @app.get("/")
    def index_page():
        return render_template("index.html")

    @app.get("/api/overview")
    def overview():
        return jsonify({
            "tags": _tag_summaries(layout),
            "candidates": _candidates(layout),
        })

    @app.get("/api/tags/<path:name>")
    def tag_detail(name):
        tags = layout.tags()
        if name not in tags:
            return jsonify({"error": f"标签不存在: {name}"}), 404
        desc = tags[name]
        body = {"tag": name, "descriptor": desc}
        media_type = desc.get("mediaType")
        if media_type in INDEX_MEDIA_TYPES:
            body["index"] = layout.read_json_blob(desc)
            manifests = []
            for entry in body["index"].get("manifests", []):
                item = {
                    "digest": entry.get("digest"),
                    "size": entry.get("size"),
                    "platform": entry.get("platform"),
                }
                try:
                    info = layout.inspect_manifest(entry)
                    item["architecture"] = info["architecture"]
                    item["revision"] = info["revision"]
                except OciError as exc:
                    item["error"] = str(exc)
                manifests.append(item)
            body["manifests"] = manifests
        elif media_type in MANIFEST_MEDIA_TYPES:
            info = layout.inspect_manifest(desc)
            body["manifest"] = {
                "digest": desc["digest"],
                "architecture": info["architecture"],
                "revision": info["revision"],
            }
        return jsonify(body)

    @app.post("/api/releases")
    def create_release():
        data = request.get_json(force=True, silent=True) or {}
        try:
            job = manager.submit(
                tag=data.get("tag") or "",
                amd64_digest=data.get("amd64") or "",
                arm64_digest=data.get("arm64") or "",
                expected_tag_digest=data.get("expected_tag_digest") or None,
            )
        except OciError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"job": job.to_dict()}), 202

    @app.get("/api/releases")
    def list_releases():
        return jsonify({"jobs": [j.to_dict() for j in manager.list_jobs()]})

    @app.get("/api/releases/<job_id>")
    def release_detail(job_id):
        job = manager.get(job_id)
        if job is None:
            return jsonify({"error": f"发布任务不存在: {job_id}"}), 404
        return jsonify({"job": job.to_dict()})

    @app.errorhandler(OciError)
    def oci_error(exc):
        return jsonify({"error": str(exc)}), 500

    return app


def _tag_summaries(layout: OciLayout) -> list:
    summaries = []
    for ref, desc in sorted(layout.tags().items()):
        item = {
            "tag": ref,
            "digest": desc.get("digest"),
            "mediaType": desc.get("mediaType"),
        }
        try:
            media_type = desc.get("mediaType")
            if media_type in INDEX_MEDIA_TYPES:
                index = layout.read_json_blob(desc)
                item["kind"] = "index"
                arches, revisions = set(), set()
                for entry in index.get("manifests", []):
                    arch = (entry.get("platform") or {}).get("architecture")
                    if arch:
                        arches.add(arch)
                    try:
                        rev = layout.inspect_manifest(entry)["revision"]
                    except OciError:
                        rev = None
                    if rev:
                        revisions.add(rev)
                item["arches"] = sorted(arches)
                item["revisions"] = sorted(revisions)
            elif media_type in MANIFEST_MEDIA_TYPES:
                info = layout.inspect_manifest(desc)
                item["kind"] = "manifest"
                item["arches"] = [info["architecture"]] if info["architecture"] else []
                item["revisions"] = [info["revision"]] if info["revision"] else []
            else:
                item["kind"] = "other"
        except OciError as exc:
            item["kind"] = "error"
            item["error"] = str(exc)
        summaries.append(item)
    return summaries


def _candidates(layout: OciLayout) -> list:
    tags_by_digest = {}
    for ref, desc in layout.tags().items():
        tags_by_digest.setdefault(desc.get("digest"), []).append(ref)
    candidates = []
    for desc in layout.manifest_descriptors():
        item = {
            "digest": desc["digest"],
            "size": desc.get("size"),
            "tags": sorted(tags_by_digest.get(desc["digest"], [])),
        }
        try:
            info = layout.inspect_manifest(desc)
            item["architecture"] = info["architecture"]
            item["os"] = info["os"]
            item["revision"] = info["revision"]
        except OciError as exc:
            item["error"] = str(exc)
        candidates.append(item)
    candidates.sort(key=lambda c: (c.get("revision") or "",
                                   c.get("architecture") or ""))
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description="OCI multi-arch release page")
    parser.add_argument("--layout", required=True, help="OCI Image Layout 目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    app = create_app(args.layout)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
