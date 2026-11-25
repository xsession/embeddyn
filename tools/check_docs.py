#!/usr/bin/env python3
"""Lightweight offline documentation consistency checks for Embeddyn.

Checks local Markdown links and the presence of the public documentation entry
points. It deliberately ignores HTTP(S), mailto, anchors-only links, and links
inside fenced code blocks.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / "CONTRIBUTING.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "user" / "README.md",
    ROOT / "docs" / "contributing" / "README.md",
    ROOT / "docs" / "c4" / "README.md",
]
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    # docs/chats holds rendered session transcripts: records of tool calls, not documentation
    docs = [p for p in sorted((ROOT / "docs").rglob("*.md")) if "chats" not in p.relative_to(ROOT / "docs").parts[:1]]
    return [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *docs]


def strip_fenced_code(text: str) -> str:
    out: list[str] = []
    in_fence = False
    marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            marker = stripped[:3]
            continue
        if in_fence and stripped.startswith(marker):
            in_fence = False
            marker = ""
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def local_target(source: Path, raw: str) -> Path | None:
    target = raw.strip().split(maxsplit=1)[0].strip("<>")
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    target = unquote(target.split("#", 1)[0])
    if not target:
        return None
    return (source.parent / target).resolve()


def main() -> int:
    failures: list[str] = []
    for required in REQUIRED:
        if not required.exists():
            failures.append(f"missing required document: {required.relative_to(ROOT)}")

    for path in markdown_files():
        if not path.exists():
            continue
        text = strip_fenced_code(path.read_text(encoding="utf-8"))
        for match in LINK_RE.finditer(text):
            target = local_target(path, match.group(1))
            if target is None:
                continue
            try:
                target.relative_to(ROOT)
            except ValueError:
                failures.append(f"{path.relative_to(ROOT)}: link escapes repository: {match.group(1)}")
                continue
            if not target.exists():
                failures.append(f"{path.relative_to(ROOT)}: missing link target: {match.group(1)}")

    if failures:
        print("Documentation checks failed:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)
        return 1
    print(f"Documentation checks passed for {len(markdown_files())} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
