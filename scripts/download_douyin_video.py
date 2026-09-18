#!/usr/bin/env python
"""下载单条抖音视频 —— 不依赖任何外部下载器（无 yt-dlp / 无 MediaCrawler / 无浏览器）。

为什么能不要签名：抖音 web 端 ``/aweme/v1/web/aweme/detail/`` 只校验 **cookie 登录态**
（关键是 ``s_v_web_id`` / ``sessionid`` 那一批），``a_bogus`` 缺失时它**不会**拒绝——
yt-dlp 自身也不计算签名，走的正是同一个端点。实测 ``status_code=0`` 直接返回 ``aweme_detail``。

分工：
* **解析**（短链 → aweme_id → 直链）—— 本脚本用标准库 ``urllib`` 完成；
* **下载**（写盘 + 体积上限 + 重试）—— 复用项目自身能力
  ``douyin_intelligence.materials.download_video``（纯 urllib，无外部依赖）。

用法::

    .venv/Scripts/python.exe scripts/download_douyin_video.py "<分享链接或含链接的分享文案>" \\
        --out downloads

    # 只要 H.265 原始档 / 只要最低档带宽
    ... --codec h265
    ... --codec auto --quality lowest

退出码：0 成功；2 解析失败（cookie 失效 / 视频不存在 / 被风控）；3 下载失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from douyin_intelligence.config import load_config  # noqa: E402
from douyin_intelligence.materials import download_video  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
SHARE_LINK_RE = re.compile(r"https?://v\.douyin\.com/[\w-]+/?")
VIDEO_ID_RE = re.compile(r"douyin\.com/(?:video|note)/(\d+)")
BARE_ID_RE = re.compile(r"^\d{15,25}$")

DEFAULT_COOKIE_CANDIDATES = (
    REPO_ROOT.parent / "www.douyin.com_cookies.txt",
    REPO_ROOT.parent.parent / "www.douyin.com_cookies.txt",
    Path.home() / "www.douyin.com_cookies.txt",
)


class ResolveError(RuntimeError):
    """解析阶段失败（与下载失败区分开，便于调用方判断是否要去刷 cookie）。"""


# --------------------------------------------------------------------------- cookie


def load_cookie_header(path: Path) -> str:
    """把 Netscape cookie 文件压成一行 ``Cookie:`` 头。只取未过期条目。"""
    import time

    now = time.time()
    pairs: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value, expires = parts[5], parts[6], parts[4]
        if name in seen:
            continue
        if expires.isdigit() and int(expires) and int(expires) < now:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    if not pairs:
        raise ResolveError(f"cookie 文件里没有可用条目：{path}")
    return "; ".join(pairs)


def find_cookie_file(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ResolveError(f"--cookies 指向的文件不存在：{path}")
        return path
    for candidate in DEFAULT_COOKIE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise ResolveError("找不到抖音 cookie 文件，请用 --cookies 显式指定（Netscape 格式）")


# --------------------------------------------------------------------------- HTTP


def _http_get(url: str, cookie: str | None) -> tuple[str, bytes]:
    """跟随重定向的 GET。返回 (最终 URL, body)。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.geturl(), response.read()


def extract_aweme_id(text: str) -> tuple[str, str | None]:
    """从「分享文案 / 短链 / 长链 / 纯 id」里抠出 aweme_id。

    返回 (aweme_id, 需要先解析的短链或 None)。
    """
    text = text.strip()
    if BARE_ID_RE.match(text):
        return text, None
    match = VIDEO_ID_RE.search(text)
    if match:
        return match.group(1), None
    short = SHARE_LINK_RE.search(text)
    if short:
        return "", short.group(0)
    raise ResolveError(f"无法从这个输入里认出抖音视频：{text[:120]}")


def resolve_aweme_id(text: str, cookie: str) -> str:
    aweme_id, short_link = extract_aweme_id(text)
    if aweme_id:
        return aweme_id
    final_url, _ = _http_get(short_link, cookie)  # type: ignore[arg-type]
    match = VIDEO_ID_RE.search(final_url)
    if not match:
        raise ResolveError(f"短链 {short_link} 未重定向到视频页，落到：{final_url[:120]}")
    return match.group(1)


def fetch_detail(aweme_id: str, cookie: str) -> dict:
    status, body = _http_get(f"{DETAIL_API}?aweme_id={aweme_id}", cookie)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"detail 接口返回的不是 JSON（HTTP {status}）") from exc
    if payload.get("status_code") != 0:
        raise ResolveError(
            f"detail 接口 status_code={payload.get('status_code')}"
            "（多为 cookie 失效或被风控；重新导出 cookie 再试）"
        )
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict) or not detail:
        raise ResolveError("detail 接口没有返回 aweme_detail（视频可能已删除/仅作者可见）")
    return detail


# --------------------------------------------------------------------------- 选流


def _first_url(node: object) -> str | None:
    if isinstance(node, dict):
        urls = node.get("url_list")
        if isinstance(urls, list) and urls:
            return str(urls[0])
    return None


def _definition_of(entry: dict) -> tuple[int, str]:
    """从档位读 (高度像素, definition 文本)。``video_extra`` 是 JSON 串。"""
    text = str(entry.get("gear_name") or "")
    audio: object = None
    try:
        audio = json.loads(entry.get("video_extra") or "{}").get("definition")
    except (json.JSONDecodeError, AttributeError):
        audio = None
    if isinstance(audio, str) and audio:
        digits = re.match(r"(\d+)", audio)
        if digits:
            return int(digits.group(1)), audio
    digits = re.match(r"(\d+)", text)
    return (int(digits.group(1)) if digits else 0), (text or "?")


def pick_source(detail: dict, *, codec: str, quality: str) -> tuple[str, str, str, str]:
    """挑一条直链。返回 (url, 档位说明, 直链来源字段, 编码)。

    ``bit_rate`` 每个档位自带 ``is_h265`` / ``is_bytevc1`` 与 ``video_extra.definition``，
    所以「按分辨率 + 编码」精确选档是可行的（**不要**信 ``video.is_h265``，实测它与档位不一致）。
    """
    video = detail.get("video") or {}

    gears: list[tuple[int, int, str, str, str]] = []  # (definition, bitrate, name, url, codec)
    for entry in video.get("bit_rate") or []:
        url = _first_url(entry.get("play_addr"))
        if not url:
            continue
        definition, label = _definition_of(entry)
        entry_codec = "h265" if int(entry.get("is_h265") or 0) else "h264"
        gears.append((definition, int(entry.get("bit_rate") or 0), label, url, entry_codec))

    def fallback(field: str, codec_label: str) -> tuple[str, str, str, str] | None:
        url = _first_url(video.get(field)) or _first_url(video.get("play_addr"))
        if not url:
            return None
        return url, f"{codec_label}-default", field, codec_label

    want = {"auto": None, "h264": "h264", "h265": "h265"}[codec]
    matching = [g for g in gears if want is None or g[4] == want]
    if matching:
        matching.sort(key=lambda item: (item[0], item[1]))
        definition, bitrate, label, url, entry_codec = matching[0 if quality == "lowest" else -1]
        return url, f"{label}@{bitrate}", "bit_rate", entry_codec

    # 指定编码没有独立档位时的兜底：用字段直链（通常是该编码的最低可用规格）
    if codec == "h265":
        found = fallback("play_addr_265", "h265")
    elif codec == "h264":
        found = fallback("play_addr_h264", "h264")
    else:
        found = fallback("play_addr", "auto")
    if found:
        return found
    raise ResolveError("aweme_detail 里没有任何可用的播放地址")


# --------------------------------------------------------------------------- 主流程


def safe_stem(text: str, aweme_id: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned[:limit].strip() or aweme_id
    return f"{cleaned} [{aweme_id}]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="下载单条抖音视频（零外部下载器依赖）")
    parser.add_argument("input", help="分享文案 / 短链 / www.douyin.com/video/<id> / 纯 aweme_id")
    parser.add_argument("--out", default="downloads", help="输出目录（默认 ./downloads）")
    parser.add_argument("--cookies", default=None, help="Netscape 格式 cookie 文件；默认自动探测")
    parser.add_argument(
        "--codec",
        choices=("auto", "h264", "h265"),
        default="auto",
        help="auto=按分辨率/码率挑最优档（抖音最优档通常是 H.265，默认）；h264=编辑器兼容优先",
    )
    parser.add_argument(
        "--quality", choices=("best", "lowest"), default="best", help="仅 --codec auto 时有意义"
    )
    parser.add_argument("--name", default=None, help="自定义文件名 stem（不含扩展名）")
    parser.add_argument("--no-info-json", action="store_true", help="不写同名 .info.json")
    parser.add_argument("--list-gears", action="store_true", help="列出全部可用档位后退出")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印，不落盘")
    return parser


def list_gears(detail: dict) -> None:
    video = detail.get("video") or {}
    print("可用档位（按 --codec 过滤前的全集）：")
    for entry in video.get("bit_rate") or []:
        definition, label = _definition_of(entry)
        print(
            "  %-14s %-6s %-9s %-6s %s"
            % (
                entry.get("gear_name"),
                label,
                int(entry.get("bit_rate") or 0),
                "h265" if int(entry.get("is_h265") or 0) else "h264",
                f"definition={definition}px",
            )
        )
    for field in ("play_addr", "play_addr_h264", "play_addr_265"):
        node = video.get(field)
        if isinstance(node, dict) and node.get("url_list"):
            print(f"  [字段直链] {field}  n={len(node['url_list'])}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cookie_path = find_cookie_file(args.cookies)
        cookie = load_cookie_header(cookie_path)
        aweme_id = resolve_aweme_id(args.input, cookie)
        detail = fetch_detail(aweme_id, cookie)
        if args.list_gears:
            list_gears(detail)
            return 0
        url, gear, field, picked_codec = pick_source(detail, codec=args.codec, quality=args.quality)
    except (ResolveError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[解析失败] {exc}", file=sys.stderr)
        return 2

    desc = (detail.get("desc") or "").strip()
    author = ((detail.get("author") or {}).get("nickname")) or "?"
    create_time = detail.get("create_time")
    duration = ((detail.get("video") or {}).get("duration") or 0) / 1000
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    stem = args.name or safe_stem(desc, aweme_id)
    destination = out_dir / f"{stem}.mp4"

    print(f"aweme_id = {aweme_id}")
    print(f"作者     = {author}")
    print(f"标题     = {desc[:70]}")
    print(f"时长     = {duration:.2f}s")
    print(f"直链来源 = {field}  档位 = {gear}  编码 = {picked_codec}")
    print(f"目标文件 = {destination}")

    # 想兼容 H.264 但拿到的是低规格时，必须说清代价（抖音的 h264 直链常是 576p）
    if args.codec == "h264" and not field.startswith("bit_rate"):
        best = pick_source(detail, codec="auto", quality="best")
        if best[2] == "bit_rate":
            print(f"[注意] 本视频没有独立的 H.264 档位，--codec h264 落到 {gear}；最优档为 {best[1]}")

    if args.dry_run:
        print("[dry-run] 未下载")
        return 0

    try:
        download_video(url, destination, load_config())
    except Exception as exc:  # noqa: BLE001 - 对外统一成退出码 3
        print(f"[下载失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    size = destination.stat().st_size
    print(f"[完成] {size} 字节")

    if not args.no_info_json:
        sidecar = destination.with_suffix(".info.json")
        sidecar.write_text(
            json.dumps(
                {
                    "aweme_id": aweme_id,
                    "desc": desc,
                    "author": author,
                    "create_time": create_time,
                    "duration_seconds": round(duration, 3),
                    "gear": gear,
                    "source_field": field,
                    "codec_request": args.codec,
                    "codec_actual": picked_codec,
                    "file": destination.name,
                    "bytes": size,
                    "resolved_by": "douyin_intelligence.scripts.download_douyin_video",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[元数据] {sidecar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
