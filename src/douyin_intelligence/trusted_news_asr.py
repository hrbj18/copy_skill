from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .exporter import atomic_write_json
from .media_processing import CheckpointTranscriber


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    output = Path(args.output).resolve()
    root = Path(args.temp_root).resolve()
    result = CheckpointTranscriber(config).run(Path(args.video).resolve(), root / "cache", root / "work")
    atomic_write_json(output, result)
    return 0 if result.get("status") in {"success", "no_speech"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
