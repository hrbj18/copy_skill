from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from douyin_intelligence.human_brief import render_human_brief


def main() -> int:
    parser = argparse.ArgumentParser(description="从候选池 JSON 生成包外人类可读图文简报")
    parser.add_argument("--input", required=True, type=Path, help="candidate-pool.json 路径")
    parser.add_argument("--output", required=True, type=Path, help="输出 Markdown 路径")
    args = parser.parse_args()

    source = args.input.resolve(strict=True)
    output = args.output.resolve()
    pack = json.loads(source.read_text(encoding="utf-8"))
    if pack.get("schema") != "daily-hot-candidate-pool-v2":
        parser.error("输入不是 V2 候选池合同")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        artifact_prefix = os.path.relpath(source.parent, output.parent).replace(os.sep, "/")
    except ValueError:
        parser.error("输出和输入包必须位于同一个磁盘")
    if ".." in Path(artifact_prefix).parts:
        parser.error("包外预览必须写在输入包的同级或上级目录，避免生成越界素材路径")
    output.write_text(render_human_brief(pack, artifact_prefix=artifact_prefix), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
