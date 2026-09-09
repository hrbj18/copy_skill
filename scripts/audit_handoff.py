"""Audit the project's task-routed handoff package and context policy."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Result:
    path: str
    exists: bool
    characters: int
    limit: int

    @property
    def ok(self) -> bool:
        return self.exists and self.characters <= self.limit


def count_non_whitespace(text: str) -> int:
    return sum(not character.isspace() for character in text)


def _safe_relative_path(root: Path, raw_path: Any, label: str) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{label} must contain non-empty string paths")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} contains an unsafe path: {raw_path}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes the project root: {raw_path}") from error
    return relative.as_posix(), target


def _string_list(policy: dict[str, Any], key: str) -> list[str]:
    value = policy.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"context policy must define a non-empty {key} array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must contain non-empty strings")
    return value


def audit(root: Path, policy_path: Path | None = None) -> list[Result]:
    root = root.resolve()
    policy_path = policy_path or root / "docs" / "handoff" / "context-policy.json"
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("version") != 1:
        raise ValueError("context policy version must be 1")
    if policy.get("counting") != "non_whitespace_characters":
        raise ValueError("context policy must count non-whitespace characters")

    required = policy.get("required_files")
    if not isinstance(required, dict) or not required:
        raise ValueError("context policy must define a non-empty required_files object")

    required_paths: dict[str, Path] = {}
    results: list[Result] = []
    for raw_path, raw_limit in required.items():
        relative, target = _safe_relative_path(root, raw_path, "required_files")
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit <= 0:
            raise ValueError(f"invalid character limit for {relative}")
        required_paths[relative] = target
        exists = target.is_file()
        text = target.read_text(encoding="utf-8") if exists else ""
        results.append(Result(relative, exists, count_non_whitespace(text), raw_limit))

    read_sets = _string_list(policy, "default_read_set") + _string_list(policy, "project_task_read_set")
    normalized_read_set = {_safe_relative_path(root, item, "read sets")[0] for item in read_sets}
    unknown_reads = normalized_read_set.difference(required_paths)
    if unknown_reads:
        raise ValueError(f"read sets reference files outside required_files: {sorted(unknown_reads)}")

    forbidden = {
        _safe_relative_path(root, item, "forbidden_context_inputs")[0]
        for item in _string_list(policy, "forbidden_context_inputs")
    }
    overlap = forbidden.intersection(normalized_read_set)
    if overlap:
        raise ValueError(f"forbidden inputs are present in read sets: {sorted(overlap)}")
    if "docs/项目开发过程文档.md" not in forbidden:
        raise ValueError("append-only process history must be a forbidden context input")

    current_task_raw = policy.get("current_task")
    current_task, current_task_path = _safe_relative_path(root, current_task_raw, "current_task")
    if not current_task_path.is_file():
        raise ValueError(f"current task guide is missing: {current_task}")

    router = required_paths.get("docs/handoff/README.md")
    if router is None or current_task not in router.read_text(encoding="utf-8"):
        raise ValueError("handoff README must name the current task guide")
    agents = required_paths.get("AGENTS.md")
    if agents is None:
        raise ValueError("AGENTS.md must be a required context file")
    agents_text = agents.read_text(encoding="utf-8")
    if "docs/项目开发过程文档.md" not in agents_text or "scripts/audit_handoff.py" not in agents_text:
        raise ValueError("AGENTS.md must preserve the history prohibition and audit route")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the copy_skill handoff package")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    try:
        results = audit(args.root, args.policy)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[FAIL] {error}")
        return 1

    if args.as_json:
        print(json.dumps([asdict(result) | {"ok": result.ok} for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            state = "OK" if result.ok else "FAIL"
            print(f"[{state}] {result.path}: {result.characters}/{result.limit} chars")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

