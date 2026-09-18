"""``scripts/download_douyin_video.py`` 的离线判据。

只测**纯函数**（id/短链解析、cookie 压缩、档位选择、文件名清洗），全部不联网 ——
真正的网络路径由端到端手动验收证明（见仓库日志 2026-09-17：auto 档产物与 yt-dlp
产物 md5 逐字节相同 ``e66dccbd4926ceab4cfe4cc585a877ce``）。

本模块还有一条**结构性判据**：脚本自身不得引入任何外部下载器依赖。这条判据的价值在于
「脱离第三方下载器也能下」这个承诺一旦被后来的改动破坏，测试会先红，而不是等到某次
真去下载时才发现。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "download_douyin_video.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("download_douyin_video", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mr = _load_module()


# --------------------------------------------------------------------- id 解析


def test_extract_aweme_id_accepts_full_share_copywrite() -> None:
    text = (
        "5.87 复制打开抖音，看看【红星新闻的作品】AI“劫持”网站偷建“地下论坛”察觉清理 竟快速分... "
        "https://v.douyin.com/opthteffVwo/ 06/29 :5pm D@u.se jPX:/ 0"
    )
    aweme_id, short_link = mr.extract_aweme_id(text)
    assert aweme_id == ""
    assert short_link == "https://v.douyin.com/opthteffVwo/"


def test_extract_aweme_id_from_long_url_ignores_query() -> None:
    aweme_id, short_link = mr.extract_aweme_id(
        "https://www.douyin.com/video/7686317511540821299?previous_page=app_code_link"
    )
    assert aweme_id == "7686317511540821299"
    assert short_link is None


def test_extract_aweme_id_accepts_bare_id_and_note_url() -> None:
    assert mr.extract_aweme_id("7686317511540821299")[0] == "7686317511540821299"
    assert mr.extract_aweme_id("https://www.douyin.com/note/7686317511540821299")[0] == "7686317511540821299"


def test_extract_aweme_id_rejects_unrelated_text() -> None:
    with pytest.raises(mr.ResolveError):
        mr.extract_aweme_id("这不是一个抖音链接")


# ----------------------------------------------------------------------- cookie


def _write_cookie_file(path: Path, rows: str) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        "# a comment that must be ignored\n"
        + rows,
        encoding="utf-8",
    )
    return path


def test_load_cookie_header_skips_comments_expired_and_duplicates(tmp_path: Path) -> None:
    path = _write_cookie_file(
        tmp_path / "c.txt",
        "\n".join(
            [
                ".douyin.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc",
                ".douyin.com\tTRUE\t/\tTRUE\t1\tstale_cookie\tdead",
                ".douyin.com\tTRUE\t/\tFALSE\t0\tttwid\txyz",
                ".douyin.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tshould_not_win",
                "malformed line without tabs",
                "",
            ]
        ),
    )
    header = mr.load_cookie_header(path)
    assert "sessionid=abc" in header
    assert "ttwid=xyz" in header
    assert "stale_cookie" not in header  # 已过期
    assert header.count("sessionid=") == 1  # 去重，且保留先出现的那条
    assert "malformed" not in header


def test_load_cookie_header_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("# nothing usable\n", encoding="utf-8")
    with pytest.raises(mr.ResolveError):
        mr.load_cookie_header(path)


def test_find_cookie_file_reports_missing_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(mr.ResolveError):
        mr.find_cookie_file(str(tmp_path / "nope.txt"))


# --------------------------------------------------------------------- 选档位


def _detail_gears() -> dict:
    """形状取自真实响应（2026-09-17 红星新闻那条），已去掉一切真实直链。"""

    def gear(name: str, definition: str, bitrate: int, is_h265: int, url: str) -> dict:
        return {
            "gear_name": name,
            "bit_rate": bitrate,
            "is_h265": is_h265,
            "is_bytevc1": is_h265,
            "format": "mp4",
            "video_extra": json.dumps({"definition": definition, "format": "mp4"}),
            "play_addr": {"url_list": [url]},
        }

    return {
        "video": {
            "duration": 33792,
            # 真实响应里 video 级 is_h265 与档位不一致，故意也造得「不一致」，
            # 防止后来者又去信这个字段。
            "is_h265": 0,
            "bit_rate": [
                gear("720_1_1", "720p", 624883, 1, "https://cdn.example/720_1_1"),
                gear("540_1_1", "540p", 509831, 1, "https://cdn.example/540_1_1"),
                gear("480_1_1", "480p", 300000, 0, "https://cdn.example/480_1_1"),
            ],
            "play_addr": {"url_list": ["https://cdn.example/play_addr"]},
            "play_addr_h264": {"url_list": ["https://cdn.example/h264"]},
            "play_addr_265": {"url_list": ["https://cdn.example/h265"]},
        }
    }


def test_pick_source_auto_takes_highest_definition() -> None:
    url, gear, field, codec = mr.pick_source(_detail_gears(), codec="auto", quality="best")
    assert url == "https://cdn.example/720_1_1"
    assert gear == "720p@624883"
    assert field == "bit_rate"
    assert codec == "h265"


def test_pick_source_auto_lowest_takes_smallest_definition() -> None:
    url, _, _, codec = mr.pick_source(_detail_gears(), codec="auto", quality="lowest")
    assert url == "https://cdn.example/480_1_1"
    assert codec == "h264"


def test_pick_source_h265_filters_by_gear_flag() -> None:
    url, _, field, codec = mr.pick_source(_detail_gears(), codec="h265", quality="best")
    assert url == "https://cdn.example/720_1_1"
    assert field == "bit_rate"
    assert codec == "h265"


def test_pick_source_h264_uses_h264_gear_when_present() -> None:
    url, _, field, codec = mr.pick_source(_detail_gears(), codec="h264", quality="best")
    assert url == "https://cdn.example/480_1_1"
    assert field == "bit_rate"
    assert codec == "h264"


def test_pick_source_falls_back_to_field_when_requested_codec_has_no_gear() -> None:
    payload = _detail_gears()
    payload["video"]["bit_rate"] = [
        g for g in payload["video"]["bit_rate"] if int(g["is_h265"]) == 1
    ]
    url, gear, field, codec = mr.pick_source(payload, codec="h264", quality="best")
    assert url == "https://cdn.example/h264"
    assert gear == "h264-default"
    assert field == "play_addr_h264"
    assert codec == "h264"


def test_pick_source_raises_when_no_playable_address() -> None:
    with pytest.raises(mr.ResolveError):
        mr.pick_source({"video": {}}, codec="auto", quality="best")


# --------------------------------------------------------------------- 文件名


def test_safe_stem_strips_illegal_characters_and_keeps_id() -> None:
    stem = mr.safe_stem('a/b:c*d?e"f<g>h|i\nj', "123456789012345")
    assert stem.endswith("[123456789012345]")
    for bad in '/\\:*?"<>|':
        assert bad not in stem
    assert "\n" not in stem


def test_safe_stem_falls_back_to_id_for_empty_title() -> None:
    assert mr.safe_stem("   ", "123456789012345") == "123456789012345 [123456789012345]"


# --------------------------------------------------------- 结构性判据（无外部下载器）


def _imported_modules() -> set[str]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_script_imports_no_external_downloader_or_browser_driver() -> None:
    """「脱离 yt-dlp / 脱离浏览器也能下」是这次改动的核心承诺，用导入表锁死。

    只扫 import 语句，不扫正文 —— 注释里出现 "yt-dlp" 是正常的技术说明。
    """
    imported = _imported_modules()
    roots = {name.split(".")[0] for name in imported}
    for forbidden in ("yt_dlp", "subprocess", "playwright", "selenium", "requests", "httpx"):
        assert forbidden not in roots, f"不应依赖 {forbidden}"


def test_script_imports_process_own_download_helper() -> None:
    assert "douyin_intelligence.materials" in _imported_modules()
    assert "douyin_intelligence.config" in _imported_modules()


def test_detail_request_carries_only_aweme_id_and_no_signature(monkeypatch) -> None:
    """把 detail 请求的 URL 截下来看真实参数集。

    这是本方案唯一的脆弱点：抖音当前**不校验** ``a_bogus``，只校验 cookie。
    一旦它开始校验，这个测试会先红 —— 那时该做的是补签名，而不是放宽断言。
    """
    seen: list[str] = []

    def fake_get(url: str, cookie: str | None):
        seen.append(url)
        return url, b'{"status_code": 0, "aweme_detail": {"desc": "x"}}'

    monkeypatch.setattr(mr, "_http_get", fake_get)
    detail = mr.fetch_detail("7686317511540821299", "sessionid=abc")

    assert detail["desc"] == "x"
    assert seen == [f"{mr.DETAIL_API}?aweme_id=7686317511540821299"]
    assert mr.DETAIL_API == "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    assert "a_bogus" not in seen[0]


def test_fetch_detail_surfaces_cookie_failure(monkeypatch) -> None:
    monkeypatch.setattr(mr, "_http_get", lambda url, cookie: (url, b'{"status_code": 8}'))
    with pytest.raises(mr.ResolveError) as excinfo:
        mr.fetch_detail("7686317511540821299", "sessionid=stale")
    assert "status_code=8" in str(excinfo.value)


def test_fetch_detail_rejects_deleted_video(monkeypatch) -> None:
    monkeypatch.setattr(
        mr, "_http_get", lambda url, cookie: (url, b'{"status_code": 0, "aweme_detail": null}')
    )
    with pytest.raises(mr.ResolveError):
        mr.fetch_detail("7686317511540821299", "sessionid=abc")


def test_fetch_detail_rejects_non_json_body(monkeypatch) -> None:
    monkeypatch.setattr(mr, "_http_get", lambda url, cookie: (url, b"<html>verify</html>"))
    with pytest.raises(mr.ResolveError):
        mr.fetch_detail("7686317511540821299", "sessionid=abc")


def test_resolve_aweme_id_follows_short_link(monkeypatch) -> None:
    monkeypatch.setattr(
        mr,
        "_http_get",
        lambda url, cookie: (
            "https://www.douyin.com/video/7686317511540821299?previous_page=app_code_link",
            b"",
        ),
    )
    assert mr.resolve_aweme_id("https://v.douyin.com/opthteffVwo/", "sessionid=abc") == (
        "7686317511540821299"
    )


def test_resolve_aweme_id_reports_link_that_does_not_land_on_a_video(monkeypatch) -> None:
    monkeypatch.setattr(mr, "_http_get", lambda url, cookie: ("https://www.douyin.com/", b""))
    with pytest.raises(mr.ResolveError):
        mr.resolve_aweme_id("https://v.douyin.com/deadbeef/", "sessionid=abc")
