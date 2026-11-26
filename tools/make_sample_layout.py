#!/usr/bin/env python3
"""Generate the sample OCI layout used by the docker-compose demo."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.sampledata import build_sample_layout  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="目标目录（OCI Image Layout）")
    parser.add_argument("--if-empty", action="store_true",
                        help="目录已有内容时跳过生成")
    args = parser.parse_args()

    if args.if_empty and os.path.isdir(args.path) and os.listdir(args.path):
        print(f"[sampledata] {args.path} 已有内容，跳过样例生成")
        return
    info = build_sample_layout(args.path)
    print(f"[sampledata] 样例 OCI layout 已生成于 {args.path}")
    print(f"[sampledata] 现役标签 demo:latest -> {info['latest']}")
    print(f"[sampledata] 可发布候选: demo:1.1.0-amd64 + demo:1.1.0-arm64 "
          f"(revision {info['revisions']['v1.1.0'][:12]}…)")


if __name__ == "__main__":
    main()
