"""Append one bounded record without loading existing process history."""

from __future__ import annotations

import argparse
from pathlib import Path


PROCESS_DOCUMENT = Path("docs") / "项目开发过程文档.md"
MAX_RECORD_CHARACTERS = 8_000


def append_record(root: Path, record: str) -> Path:
    root = root.resolve()
    target = (root / PROCESS_DOCUMENT).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("process document escapes the project root") from error
    if not target.is_file():
        raise FileNotFoundError(f"process document is missing: {target}")

    normalized = record.strip()
    if not normalized.startswith("## "):
        raise ValueError("record must start with a level-two Markdown heading")
    if len(normalized) > MAX_RECORD_CHARACTERS:
        raise ValueError(f"record exceeds {MAX_RECORD_CHARACTERS} characters")

    # Append mode intentionally avoids reading or rewriting historical content.
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"\n\n{normalized}\n")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Append one project process record without reading history")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--record", required=True)
    args = parser.parse_args()
    try:
        target = append_record(args.root, args.record)
    except (OSError, ValueError) as error:
        print(f"[FAIL] {error}")
        return 1
    print(f"[OK] appended process record: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

