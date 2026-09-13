from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from douyin_intelligence.exporter import atomic_write_json
from douyin_intelligence.replication_delivery import (
    build_manifest,
    ensure_delivery_tree,
    publish_directory,
    render_delivery_readme,
    validate_delivery_manifest,
)


def _manifest(**overrides) -> dict:
    # ``keywords_requested`` / ``search_attribution`` must reach build_manifest
    # itself (not be post-applied) so derived fields stay consistent.
    forwarded = {key: overrides.pop(key) for key in ("keywords_requested", "search_attribution") if key in overrides}
    manifest = build_manifest(
        theme="苹果折叠屏手机",
        folder="9.12苹果折叠屏复刻视频",
        business_date="2026-09-12",
        generated_at="2026-09-12T21:40:00+08:00",
        keywords_used=["苹果折叠屏手机", "折叠屏 折痕"],
        candidate_pool_size=80,
        script_replica={"status": "found", "video_id": "7304", "author": "何同学", "heat_score": 0.91, "skeleton": "01-脚本思路/脚本骨架.json", "script_notes": "01-脚本思路/脚本思路.md"},
        material_replica_sources=[],
        main_materials=[{"clip_id": "main-01", "file": "02-主素材/a.mp4", "duration": 6.5, "face_class": "face_free", "suggested_use": "hook"}],
        supporting_materials=[{"clip_id": "support-01", "file": "03-辅助素材/b.mp4", "duration": 5.2, "face_class": "low_face", "suggested_use": "key_points[1]"}],
        counters={"candidates": 80, "clips_exported": 2},
        face_backend="opencv_yunet",
        face_backend_status="ok",
        ffmpeg_status="ok",
        degraded=False,
        insufficient=False,
        warnings=[],
        **forwarded,
    )
    manifest.update(overrides)
    return manifest


def test_manifest_has_all_required_keys_and_disclaimer() -> None:
    manifest = _manifest()
    for key in ("schema_version", "evidence_disclaimer", "degraded", "insufficient", "warnings"):
        assert key in manifest
    assert manifest["evidence_disclaimer"]


def test_validate_manifest_passes(tmp_path: Path) -> None:
    path = tmp_path / "清单.json"
    atomic_write_json(path, _manifest())
    result = validate_delivery_manifest(path)
    assert result["status"] == "pass"
    assert result["errors"] == []


def test_validate_manifest_rejects_face_heavy_and_low_face_main(tmp_path: Path) -> None:
    path = tmp_path / "清单.json"
    atomic_write_json(path, _manifest(
        main_materials=[{"clip_id": "main-01", "file": "a.mp4", "duration": 6.0, "face_class": "low_face"}],
        supporting_materials=[{"clip_id": "support-01", "file": "b.mp4", "duration": 5.0, "face_class": "face_heavy"}],
    ))
    result = validate_delivery_manifest(path)
    assert result["status"] == "fail"
    assert any("face_heavy" in error for error in result["errors"])
    assert any("非 face_free" in error for error in result["errors"])


def test_validate_manifest_handles_missing_file(tmp_path: Path) -> None:
    result = validate_delivery_manifest(tmp_path / "nope.json")
    assert result["status"] == "fail"


def test_publish_directory_replaces_atomically(tmp_path: Path) -> None:
    stage = tmp_path / ".stage"
    ensure_delivery_tree(stage)
    (stage / "清单.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    publish_directory(stage, destination)
    assert (destination / "清单.json").is_file()
    assert not (destination / "old.txt").exists()
    assert not stage.exists()
    assert not list(tmp_path.glob(".*backup*"))


def test_render_readme_contains_disclaimer_and_sections() -> None:
    text = render_delivery_readme(_manifest())
    assert "不得作为事实依据" in text
    assert "脚本复刻视频" in text
    assert "主素材" in text and "辅助素材" in text


def test_manifest_separates_requested_from_searched_keywords_default() -> None:
    # When no explicit request list is supplied the searched list is authoritative
    # and nothing is flagged as truncated.
    manifest = _manifest()
    assert manifest["keywords_requested"] == manifest["keywords_used"]
    assert manifest["keywords_truncated"] is False
    assert manifest["search_attribution"] == {}


def test_readme_reports_keyword_coverage_and_pool_target_when_truncated() -> None:
    manifest = _manifest(
        keywords_used=["苹果折叠屏", "Apple折叠屏", "iPhone折叠屏", "折叠屏 折痕"],
        keywords_requested=[
            "苹果折叠屏", "Apple折叠屏", "iPhone折叠屏", "折叠屏 折痕", "折叠屏 铰链", "折叠屏 开合",
        ],
        search_attribution={"min_pool_size": 40, "search_report_path": "05-过程数据/search_report.json"},
    )
    assert manifest["keywords_truncated"] is True
    text = render_delivery_readme(manifest)
    assert "关键词覆盖：请求 6 个 / 实际搜索 4 个（发生关键词截断，仅搜索前 4 个）" in text
    assert "候选池规模：80（最小目标 40，达标）" in text
    assert "搜索报告：05-过程数据/search_report.json" in text


def test_readme_states_no_truncation_when_full_list_searched() -> None:
    text = render_delivery_readme(_manifest())
    assert "关键词覆盖：请求 2 个 / 实际搜索 2 个" in text
    assert "发生关键词截断" not in text


def test_readme_renders_download_section_for_download_only() -> None:
    manifest = _manifest(
        mode="download_only",
        downloads=[
            {
                "video_id": "7301", "author": "作者A", "title": "标题" * 20, "duration_seconds": 60.0,
                "size_bytes": 1536, "file": "04-原片/作者A_作品_7301.mp4",
            },
        ],
        download_failures=[{"video_id": "7302", "stage": "not_video", "reason": "图文作品"}],
    )
    text = render_delivery_readme(manifest)
    assert "模式：仅采集与下载（未做人脸筛选/切片/脚本复刻）" in text
    assert "## 下载清单" in text
    assert "7301｜作者A｜" in text
    assert "1.5 KB" in text
    assert "## 下载失败" in text
    assert "not_video" in text


def test_readme_download_section_absent_without_key() -> None:
    text = render_delivery_readme(_manifest())
    assert "## 下载清单" not in text
    assert "模式：仅采集与下载" not in text


def test_publish_directory_retries_on_transient_permission_error(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / ".stage"
    ensure_delivery_tree(stage)
    (stage / "清单.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "9.12苹果折叠屏复刻视频"

    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    slept: list[float] = []
    publish_directory(stage, destination, sleep=slept.append)

    assert (destination / "清单.json").is_file()
    assert not stage.exists()
    assert calls["count"] == 3
    assert len(slept) >= 2
    assert slept == sorted(slept)
    assert slept[1] > slept[0]


def test_publish_directory_keeps_stage_when_permanently_locked(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / ".stage"
    ensure_delivery_tree(stage)
    (stage / "清单.json").write_text("stage-content", encoding="utf-8")
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")

    def locked_replace(src, dst):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(os, "replace", locked_replace)
    with pytest.raises(RuntimeError) as captured:
        publish_directory(stage, destination, sleep=lambda _: None)

    message = str(captured.value)
    assert str(destination) in message
    assert "占用" in message
    assert str(stage) in message
    # Invariants: stage survives untouched so the caller can retry for free,
    # and destination is never a half state (here: still the old version).
    assert stage.exists()
    assert (stage / "清单.json").read_text(encoding="utf-8") == "stage-content"
    assert (destination / "old.txt").is_file()


def test_publish_directory_rolls_back_when_second_replace_fails(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / ".stage"
    ensure_delivery_tree(stage)
    (stage / "清单.json").write_text("new", encoding="utf-8")
    destination = tmp_path / "9.12苹果折叠屏复刻视频"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")

    real_replace = os.replace

    def fail_publishing_stage(src, dst):
        # dest -> backup and the backup rollback both succeed; only stage -> dest fails.
        if Path(src) == stage and Path(dst) == destination:
            raise PermissionError(5, "拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_publishing_stage)
    with pytest.raises(RuntimeError):
        publish_directory(stage, destination, sleep=lambda _: None)

    # Rollback restored the old complete version -- no half state.
    assert (destination / "old.txt").is_file()
    assert not (destination / "清单.json").exists()
    # stage kept intact for a zero-cost retry.
    assert stage.exists()
    assert (stage / "清单.json").is_file()
    assert not (tmp_path / ".9.12苹果折叠屏复刻视频.backup").exists()


# --------------------------------------------------------------------------- #
# P6: 「入选」 must list one row per distinct video (never double-count).
# --------------------------------------------------------------------------- #
def _budget_block_with_duplicates() -> dict:
    row = {
        "video_id": "v1", "title": "标题A", "author": "作者A",
        "heat_score": 1.0, "relevance_score": 0.3, "size_bytes": 100,
    }
    return {
        "enabled": True,
        "limits": {"max_count": 12, "max_bytes": 157286400, "max_item_bytes": 31457280},
        "used": {"count": 2, "bytes": 300, "delivered_bytes": 300, "transferred_bytes": 500},
        "ranking": {"order": ["relevance", "heat_score", "video_id"], "note": "画面质量留待下载后"},
        "stopped_by": "count",
        "selected": [
            {**row, "stage": "script", "stages": ["script"]},
            {**row, "stage": "material", "stages": ["material"]},  # exact duplicate video
            {"video_id": "v2", "title": "标题B", "author": "作者B", "heat_score": 0.5,
             "relevance_score": 0.1, "size_bytes": 200, "stage": "material", "stages": ["material"]},
        ],
        "skipped": [],
    }


def test_readme_selection_list_is_deduped_by_video_id() -> None:
    """Renderer dedupes independently of the budget (P6): one row per video."""
    manifest = _manifest(download_budget=_budget_block_with_duplicates())
    text = render_delivery_readme(manifest)
    selected_rows = [line for line in text.splitlines() if line.startswith("- v")]
    assert len(selected_rows) == 2, selected_rows
    # The listed count equals the distinct count, not the raw row count.
    assert "下载预算入选（2 条" in text
    # The duplicate's stage tag is merged onto the single surviving row.
    assert "［脚本］［素材］" in text
    assert text.count("｜相关度") == 2


def test_readme_selection_dedupe_does_not_depend_on_budget_idempotence() -> None:
    """Feed *raw* duplicate rows (pre-P1 manifest) -- still one row each."""
    block = _budget_block_with_duplicates()
    block["used"]["count"] = 3  # a legacy ledger that double-counted
    manifest = _manifest(download_budget=block)
    text = render_delivery_readme(manifest)
    assert "下载预算入选（2 条" in text
    assert len([line for line in text.splitlines() if line.startswith("- v1")]) == 1


# --------------------------------------------------------------------------- #
# P7: every cross-reference the readme emits must resolve to a real section.
# --------------------------------------------------------------------------- #
def _validation_block(*, duration_checked: int) -> dict:
    return {
        "enabled": True,
        "config": {
            "duration_tolerance": 0.05, "full_decode": True, "decode_time_budget_seconds": 20,
            "require_metadata_duration": False, "cache_attestation": True,
        },
        "counts": {
            "validated": 3, "rejected": 0, "cache_attested": 0, "cache_attested_bad": 0,
            "duration_checked": duration_checked, "by_conclusion": {},
        },
        "by_stage": {},
    }


def test_validation_duration_pointer_resolves_in_full_chain() -> None:
    """Full-chain has no 「下载失败」 section -> the pointer must not dangle."""
    manifest = _manifest(
        validation=_validation_block(duration_checked=0),
        counters={"candidates": 80, "validation_duration_window_rejected": 2},
    )
    text = render_delivery_readme(manifest)
    assert "## 下载失败" not in text  # indeed no such section
    assert "时长比对已跳过（元数据无时长）" in text
    assert "见「下载失败」" not in text            # dangling reference removed
    assert "时长窗口剔除 2 条" in text             # outcome stated inline
    assert "05-过程数据/validation.json" in text    # points at a real artifact


def test_validation_duration_pointer_uses_download_section_when_present() -> None:
    """Download-only renders 「下载失败」 -> the pointer is valid there."""
    manifest = _manifest(
        mode="download_only",
        validation=_validation_block(duration_checked=0),
        downloads=[{"video_id": "7301", "author": "作者A", "title": "t", "duration_seconds": 60.0,
                    "size_bytes": 1536, "file": "04-原片/a.mp4"}],
        download_failures=[{"video_id": "7302", "stage": "duration_post", "reason": "时长不符"}],
    )
    text = render_delivery_readme(manifest)
    assert "## 下载失败" in text
    assert "见「下载失败」" in text


# --------------------------------------------------------------------------- #
# V5: the dangling-pointer class must be closed *generally*, not case by case.
# A ``见「X」`` cross-reference is only valid if ``## X`` is actually rendered;
# these tests assert that invariant over every readme shape that can emit it.
# --------------------------------------------------------------------------- #
_POINTER_RE = re.compile(r"见「([^」]+)」")


def _missing_pointer_targets(readme: str) -> list[str]:
    """Names referenced by ``见「X」`` that have no matching ``## X`` heading."""
    headings = {line[3:].strip() for line in readme.splitlines() if line.startswith("## ")}
    return [name for name in _POINTER_RE.findall(readme) if name not in headings]


def test_readme_cross_references_never_dangle() -> None:
    """Universal invariant: every ``见「X」`` resolves to a rendered ``## X``.

    Covers every readme shape whose section set differs, so a future section that
    becomes conditional cannot silently leave a pointer behind.
    """
    download = {"video_id": "7301", "author": "作者A", "title": "t",
                "duration_seconds": 60.0, "size_bytes": 1536, "file": "04-原片/a.mp4"}
    failure = {"video_id": "7302", "stage": "duration_post", "reason": "时长不符"}
    pointer_validation = _validation_block(duration_checked=0)  # forces the pointer branch
    variants = {
        "full_chain": _manifest(validation=pointer_validation),
        "download_only_no_downloads": _manifest(mode="download_only", validation=pointer_validation),
        "download_only_no_failures": _manifest(
            mode="download_only", validation=pointer_validation, downloads=[download],
        ),
        "download_only_with_failures": _manifest(
            mode="download_only", validation=pointer_validation,
            downloads=[download], download_failures=[failure],
        ),
        "download_only_failures_only": _manifest(
            mode="download_only", validation=pointer_validation, download_failures=[failure],
        ),
    }
    for name, manifest in variants.items():
        readme = render_delivery_readme(manifest)
        dangling = _missing_pointer_targets(readme)
        assert dangling == [], f"{name}: 悬空指针 {dangling}"


def test_download_only_without_failures_does_not_point_at_absent_section() -> None:
    """V5 regression: successes but *no* failures -> 「下载失败」 is not rendered.

    The pointer used to fire on ``downloads OR download_failures`` while the
    section renders only for failures, so this exact shape dangled.
    """
    manifest = _manifest(
        mode="download_only",
        validation=_validation_block(duration_checked=0),
        downloads=[{"video_id": "7301", "author": "作者A", "title": "t",
                    "duration_seconds": 60.0, "size_bytes": 1536, "file": "04-原片/a.mp4"}],
    )
    text = render_delivery_readme(manifest)
    assert "## 下载清单" in text
    assert "## 下载失败" not in text       # no failures -> no such section
    assert "见「下载失败」" not in text      # ... therefore no such pointer
    assert "05-过程数据/validation.json" in text  # points at a real artifact instead
    assert _missing_pointer_targets(text) == []


# --------------------------------------------------------------------------- #
# P4: the 04-原片 retention rule must be stated explicitly, not implicit.
# --------------------------------------------------------------------------- #
def test_readme_states_source_retention_rule() -> None:
    manifest = _manifest(source_retention={
        "keep_source_video": True,
        "kept_count": 2,
        "selected_count": 4,
        "note": "04-原片 仅收录最终选用素材源片（每个最终选用源 1 份）；未选用的下载原片保留在持久化媒体库，不进入交付目录",
        "persistent_store": "data/media/material-replication",
    })
    text = render_delivery_readme(manifest)
    assert "## 原片保留" in text
    assert "retention.keep_source_video：True" in text
    assert "04-原片 收录 2 份" in text
    assert "data/media/material-replication" in text
    assert "仅收录最终选用素材源片" in text
    # A: the retention note must not reuse the ambiguous 「入选」 -- in this same
    # document 「入选」 names the *download budget's* set (7), a different set from
    # the *material sources* kept in 04-原片 (4).
    note_line = next(line for line in text.splitlines() if line.startswith("- 说明："))
    assert "入选" not in note_line, note_line


def test_source_retention_section_absent_without_block() -> None:
    text = render_delivery_readme(_manifest())
    assert "## 原片保留" not in text


# --------------------------------------------------------------------------- #
# P2 (follow-up): the delivery must state the visual gate's *real* semantics.
# --------------------------------------------------------------------------- #
def test_readme_states_visual_gate_only_blocks_near_static() -> None:
    """The readme must not let a reader think the gate is a quality filter."""
    manifest = _manifest(material_replica={"status": "done", "selected": 0, "rejected": []})
    text = render_delivery_readme(manifest)
    assert "## 画面代理判据" in text
    assert "视觉合格 = 运动达标" in text
    assert "只拦「近静止」片" in text
    assert "不再拦「文字覆盖重」的片" in text


def test_visual_proxy_section_absent_for_download_only() -> None:
    """A download-only run never evaluates the visual proxy -> no section."""
    manifest = _manifest(material_replica={"status": "skipped", "rejected": []})
    text = render_delivery_readme(manifest)
    assert "## 画面代理判据" not in text


# --------------------------------------------------------------------------- #
# C: a multi-line source title must never break a Markdown list.
# The 9.13 delivery had the title ``"…帮助到大家\n如果记不住…"`` -- its 30th
# character is ``\n`` -- so ``title[:30] + "…"`` rendered a bare ``…`` line that
# severed the list.  The real fix is to fold whitespace *before* truncating.
# --------------------------------------------------------------------------- #
_MULTILINE_TITLE = (
    "小米澎程N70/N90验车教学 希望这个视频能够帮助到大家\n"
    "如果记不住的话车友们可以关注点赞，收藏起来！\n"
    "#小米 #验车"
)
# A short multi-line title never reaches the 30-char cut, so it isolates the
# other half of the bug: even an untruncated title kept its raw newline.
_SHORT_MULTILINE_TITLE = "第一行标题\n第二行标题"


def _sel_row(video_id: str, title: str) -> dict:
    return {
        "video_id": video_id, "title": title, "author": "小***0",
        "heat_score": 0.2, "relevance_score": 0.5, "size_bytes": 1024,
        "stage": "material", "stages": ["material"],
    }


def test_multiline_title_never_breaks_any_list() -> None:
    """All three title-bearing row renderers must emit a single clean line."""
    manifest = _manifest(
        mode="download_only",
        download_budget={
            "enabled": True,
            "limits": {"max_count": 12, "max_bytes": 0, "max_item_bytes": 0},
            "used": {"count": 3, "bytes": 0, "delivered_bytes": 0, "transferred_bytes": 0},
            "ranking": {"order": ["relevance"], "note": ""},
            "stopped_by": "count",
            "selected": [
                _sel_row("v0", _MULTILINE_TITLE),
                _sel_row("v1", _MULTILINE_TITLE),
                _sel_row("v2", _SHORT_MULTILINE_TITLE),
            ],
            "skipped": [],
        },
        downloads=[
            {"video_id": f"d{i}", "author": "小***0", "title": _MULTILINE_TITLE,
             "duration_seconds": 23.8, "size_bytes": 1024, "file": f"04-原片/d{i}.mp4"}
            for i in range(2)
        ],
        prefilter={
            "enabled": True,
            "config": {"exclude_terms": [], "min_seconds": 30, "max_seconds": 300,
                       "heat_gate_percentile": 10, "allow_unknown_duration": True,
                       "drop_non_video": True},
            "pool_size": 4, "passed": 0, "rejected": 2,
            "rejections": [
                {"video_id": f"r{i}", "stage": "pre_duration", "reason": "时长不符",
                 "duration_seconds": 10, "heat_score": 0.1, "author": "小***0",
                 "title": _MULTILINE_TITLE}
                for i in range(2)
            ],
        },
    )
    text = render_delivery_readme(manifest)
    lines = text.splitlines()
    # 1) no orphan 「…」 line anywhere -- the exact 9.13 defect.
    assert [line for line in lines if line.strip() == "…"] == []
    # 2) no list is severed: exactly one row per selected / download / rejected row.
    assert len([line for line in lines if line.startswith("- v")]) == 3
    assert len([line for line in lines if line.startswith("- d")]) == 2
    assert len([line for line in lines if line.startswith("- 剔除 r")]) == 2
    # 3) the newline became a single space (folding, not just truncation).
    assert "第一行标题 第二行标题" in text
    assert "\n第一行标题" not in text
    assert "\n如果记不住" not in text
    assert "\n#小米" not in text


# --------------------------------------------------------------------------- #
# A / B / D: one clear name per quantity in the download-cost sections.
# --------------------------------------------------------------------------- #
def _budget_wording_block() -> dict:
    row = {"video_id": "v1", "title": "标题A", "author": "作者A",
           "heat_score": 1.0, "relevance_score": 0.3, "size_bytes": 100,
           "stage": "material", "stages": ["material"]}
    return {
        "enabled": True,
        "limits": {"max_count": 12, "max_bytes": 157286400, "max_item_bytes": 31457280},
        "used": {"count": 2, "bytes": 300, "delivered_bytes": 300, "transferred_bytes": 500},
        "ranking": {"order": ["relevance"], "note": ""},
        "stopped_by": "count",
        "selected": [row],
        "skipped": [],
    }


def _budget_wording_validation() -> dict:
    by_stage = {
        "script": {"validated": 1, "passed": 1, "rejected": 0},
        "material": {"validated": 8, "passed": 8, "rejected": 0},
    }
    return {
        "enabled": True,
        "config": {"duration_tolerance": 0.05, "full_decode": True,
                   "decode_time_budget_seconds": 20, "require_metadata_duration": False,
                   "cache_attestation": True},
        "counts": {"validated": 9, "rejected": 0, "cache_attested": 0,
                   "cache_attested_bad": 0, "duration_checked": 1,
                   "by_conclusion": {"ok": 9}, "by_stage": by_stage},
        "by_stage": by_stage,
    }


def test_download_and_validation_wording_is_unambiguous() -> None:
    manifest = _manifest(download_budget=_budget_wording_block(),
                         validation=_budget_wording_validation())
    text = render_delivery_readme(manifest)
    # A: the budget list names its own scope, so 1 (budget) vs N (sources kept)
    # can no longer read as a self-contradiction.
    assert "下载预算入选（1 条，为什么这几条值得下）：" in text
    # B: the delivered ledger is named a *download* ledger, not a directory size.
    assert "实际使用：2 条 / 交付 300 B（下载文件账）/ 真实传输 500 B" in text
    # D: validated counts *calls* and breaks down so it reconciles with 分阶段.
    assert "校验 9 次（脚本 1 + 素材 8）：全片解码通过 9 条" in text
    assert "脚本 校验 1 次（通过 1 / 剔除 0）；素材 校验 8 次（通过 8 / 剔除 0）" in text
