#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_version.py — 릴리즈용 version.json 생성 (build.bat 마지막 단계에서 호출).

gui.py 의 VERSION 을 읽고 dist/PongDu.exe 를 해시해서 dist/version.json 을 만든다.
GitHub 릴리즈에 exe 와 이 json 을 함께 올리면 런처 자동 업데이터가 이걸 보고 동작한다.

  · 릴리즈 태그는 VERSION 과 정확히 같아야 한다 (url 이 태그 경로로 만들어지므로)
  · notes 는 비어서 나오므로 릴리즈 올리기 전에 채울 것
  · min_supported 는 기본으로 넣지 않는다. 구버전을 강제로 막아야 할 때만
    (치지직 API 스펙 변경 등) 손으로 추가한다 — 넣는 순간 그 미만 버전은 연동 불가.

수동 실행:  py make_version.py
"""

import hashlib
import json
import pathlib
import re
import sys

REPO = "Project-PongDu/Launcher"
EXE = pathlib.Path("dist/PongDu.exe")
OUT = pathlib.Path("dist/version.json")


def read_version() -> str:
    src = pathlib.Path("gui.py").read_text(encoding="utf-8")
    m = re.search(r"""^VERSION\s*=\s*["'](.+?)["']""", src, re.M)
    if not m:
        raise SystemExit("[!] gui.py 에서 VERSION 을 찾지 못했습니다.")
    return m.group(1)


def main() -> int:
    if not EXE.exists():
        print("[!] %s 가 없습니다. 먼저 빌드하세요." % EXE)
        return 1
    version = read_version()
    digest = hashlib.sha256(EXE.read_bytes()).hexdigest()
    manifest = {
        "version": version,
        "url": "https://github.com/%s/releases/download/%s/PongDu.exe" % (REPO, version),
        "sha256": digest,
        "notes": "",
    }
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("  version = %s" % version)
    print("  sha256  = %s" % digest)
    print("  size    = %.1f MB" % (EXE.stat().st_size / 1048576.0))
    print("  wrote   = %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
