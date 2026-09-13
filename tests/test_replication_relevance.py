"""Download-time match quality: token-AND relevance, live-term denominator, exclude gate.

Covers the four defects fixed here, all model-free (no LLM, no embeddings):

* a multi-word phrase such as ``苹果折叠屏 实测`` must hit only when **every**
  token appears in the title -- a whole-substring test never matched contiguous
  Chinese and silently killed those terms;
* the candidate's ``source_keyword`` (the query that surfaced it) must **not**
  count, otherwise every candidate self-certifies and none can score zero;
* the denominator is the *live-term* count (terms that hit at least one title),
  not the raw term count, so pool-independent dead terms cannot depress scores;
* a metadata-only **exclude-term** gate drops matching titles before any download,
  judged before the duration/heat gates.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from douyin_intelligence.cli import build_parser
from douyin_intelligence.config import load_config
from douyin_intelligence.replication_candidates import Candidate, collect_candidate_pool
from douyin_intelligence.replication_pipeline import (
    ReplicationDeps,
    _config_with_extra_excludes,
    run_material_replication,
)
from douyin_intelligence.replication_selection import (
    build_relevance_index,
    candidate_relevance,
    prefilter_active,
    prefilter_candidates,
    prefilter_exclude_terms,
    relevance_report,
    term_hits_title,
)

import types


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "apple_fold_pool.json"
# The real delivery artifact (``output/`` is gitignored, so it is only used as an
# *extra* cross-check when present; the controlled fixture above is authoritative).
REAL_POOL = ROOT / "output" / "复刻视频" / "9.12苹果折叠屏复刻视频" / "05-过程数据" / "candidate_pool.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _candidate(video_id: str, *, title: str = "", source_keyword: str = "", duration: float = 60.0, digg: int = 100) -> Candidate:
    candidate = Candidate(video_id=video_id, title=title, source_keyword=source_keyword, duration_seconds=duration)
    candidate.heat_score = 0.0
    return candidate


def _row(video_id: str, author: str, *, title: str = "", duration: float = 60.0, digg: int = 100, url: str = "") -> dict:
    row = {
        "aweme_id": video_id,
        "desc": title or f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }
    if url:
        row["video_download_url"] = url
    return row


def _config(tmp_path: Path, *, prefilter: dict | None = None, budget: dict | None = None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    if prefilter is None:
        config["jobs"]["material_replication"].pop("prefilter", None)
    else:
        config["jobs"]["material_replication"]["prefilter"] = prefilter
    if budget is None:
        config["jobs"]["material_replication"].pop("download_budget", None)
    else:
        config["jobs"]["material_replication"]["download_budget"] = budget
    # Isolate from the real ffprobe/ffmpeg validation layer (its own module).
    config["jobs"]["material_replication"]["validation"] = {"enabled": False}
    return config


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        assert before_sanitize is not None
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _deps(tmp_path: Path, rows: list[dict]):
    downloaded: list[str] = []

    def downloader(url, destination, config):
        downloaded.append(Path(destination).stem)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"video-bytes")

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober)
    return deps, downloaded


# --------------------------------------------------------------------------- #
# A/B: token-AND matching + live-term denominator
# --------------------------------------------------------------------------- #
def test_multi_word_phrase_requires_every_token() -> None:
    # The whole phrase "苹果折叠屏 实测" never occurs in contiguous Chinese ...
    assert "苹果折叠屏 实测" not in "苹果折叠屏实测视频"
    # ... but the token-AND test recognises both tokens' co-occurrence.
    assert term_hits_title("苹果折叠屏 实测", "苹果折叠屏实测视频") is True
    # A missing token means no hit.
    assert term_hits_title("苹果折叠屏 实测", "苹果折叠屏开箱视频") is False
    # Casefold makes a mixed-case alias match a lowercase title.
    assert term_hits_title("Apple折叠屏", "apple折叠屏上手") is True
    # A blank term never hits.
    assert term_hits_title("   ", "任何标题") is False


def test_dead_terms_are_excluded_from_the_denominator() -> None:
    candidates = [_candidate("a", title="苹果折叠屏开箱")]
    keywords = ["苹果折叠屏", "苹果折叠屏 实测", "Apple折叠屏"]
    report = relevance_report(candidates, "苹果折叠屏", keywords)
    # Only the plain term hits; the phrase (no 实测) and the alias (no apple) are dead.
    assert report["live_terms"] == ["苹果折叠屏"]
    assert set(report["dead_terms"]) == {"苹果折叠屏 实测", "Apple折叠屏"}
    assert report["live_count"] == 1 and report["dead_count"] == 2
    assert report["degraded"] is False
    # 1 hit over the *live* denominator 1 -> 1.0 (the raw denominator would be 3).
    assert report["scores"]["a"] == 1.0
    assert build_relevance_index(candidates, "苹果折叠屏", keywords) == report["scores"]


def test_source_keyword_no_longer_self_certifies() -> None:
    # Title carries no theme token, but the surfacing query does: it must not count.
    candidate = _candidate(
        "x", title="手把手教你用豆包抢苹果18现货", source_keyword="苹果折叠屏 实测",
    )
    assert candidate_relevance(candidate, ["苹果折叠屏"]) == 0.0
    assert build_relevance_index([candidate], "苹果折叠屏", ["苹果折叠屏"])["x"] == 0.0


def test_zero_live_terms_is_degraded_without_division_error() -> None:
    candidates = [_candidate("a", title="与题材完全无关的视频"), _candidate("b", title="另一条")]
    report = relevance_report(candidates, "苹果折叠屏", ["苹果折叠屏"])
    assert report["live_count"] == 0
    assert report["degraded"] is True
    # No ZeroDivisionError; every score is a plain 0.0.
    assert report["scores"] == {"a": 0.0, "b": 0.0}
    assert build_relevance_index(candidates, "苹果折叠屏", ["苹果折叠屏"]) == {"a": 0.0, "b": 0.0}


# --------------------------------------------------------------------------- #
# C: exclude-term gate
# --------------------------------------------------------------------------- #
def test_exclude_terms_helper_reads_dedups_and_accepts_a_bare_string() -> None:
    config = {
        "jobs": {"material_replication": {"prefilter": {"exclude_terms": [" 豆包 ", "豆包", "抢购"]}}}
    }
    assert prefilter_exclude_terms(config) == ["豆包", "抢购"]
    assert prefilter_exclude_terms({"jobs": {"material_replication": {"prefilter": {"exclude_terms": "带货"}}}}) == ["带货"]
    assert prefilter_exclude_terms({"jobs": {"material_replication": {"prefilter": {}}}}) == []


def test_prefilter_exclude_gate_drops_titles_containing_a_term() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 0, "max_seconds": 0, "exclude_terms": ["豆包", "抢购"],
    }
    rows = [
        _candidate("keep", title="苹果折叠屏实测"),
        _candidate("drop1", title="用豆包抢苹果18现货"),
        _candidate("drop2", title="抢购折叠屏真机"),
    ]
    passed, rejected = prefilter_candidates(rows, config)
    assert [c.video_id for c in passed] == ["keep"]
    assert {r["video_id"] for r in rejected} == {"drop1", "drop2"}
    assert {r["stage"] for r in rejected} == {"pre_exclude"}
    assert all("排除词" in r["reason"] for r in rejected)


def test_exclude_is_judged_before_duration() -> None:
    config = load_config()
    config["jobs"]["material_replication"]["prefilter"] = {
        "enabled": True, "min_seconds": 10, "max_seconds": 300,
        "allow_unknown_duration": True, "exclude_terms": ["豆包"],
    }
    # Too short AND contains an exclude term: the exclude gate must claim it first.
    rows = [_candidate("x", title="豆包带货", duration=1)]
    passed, rejected = prefilter_candidates(rows, config)
    assert passed == []
    assert rejected[0]["stage"] == "pre_exclude"


def test_extra_excludes_returns_the_same_object_when_nothing_is_added() -> None:
    config = load_config()
    assert _config_with_extra_excludes(config, None) is config
    assert _config_with_extra_excludes(config, []) is config
    assert _config_with_extra_excludes(config, ["  "]) is config


# --------------------------------------------------------------------------- #
# C': the exclude gate is switch-independent (an explicit term is never dropped)
# --------------------------------------------------------------------------- #
def test_prefilter_active_ignores_the_enabled_switch_when_a_term_is_present() -> None:
    def _cfg(prefilter: dict) -> dict:
        return {"jobs": {"material_replication": {"prefilter": prefilter}}}

    assert prefilter_active(_cfg({"enabled": False})) is False
    assert prefilter_active(_cfg({"enabled": True})) is True
    # A term alone activates the gate even with the duration/heat switch off.
    assert prefilter_active(_cfg({"enabled": False, "exclude_terms": ["豆包"]})) is True
    assert prefilter_active({"jobs": {"material_replication": {}}}) is False


def test_exclude_gate_runs_when_prefilter_disabled_with_config_terms(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={"enabled": False, "exclude_terms": ["豆包"]})
    rows = [
        _row("keep", "作者A", title="苹果折叠屏实测", url="https://signed.example/1"),
        _row("drop", "作者B", title="用豆包抢苹果18现货", url="https://signed.example/2"),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert downloaded == ["keep"], downloaded
    out = Path(result["output_dir"])

    # The cut must be auditable: prefilter.json exists even though enabled=false.
    prefilter = json.loads((out / "05-过程数据" / "prefilter.json").read_text(encoding="utf-8"))
    assert prefilter["enabled"] is False
    assert prefilter["exclude_only"] is True
    assert prefilter["config"]["exclude_terms"] == ["豆包"]
    assert prefilter["rejections"][0]["stage"] == "pre_exclude"
    assert prefilter["rejections"][0]["video_id"] == "drop"

    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    assert manifest["prefilter"]["exclude_only"] is True
    assert manifest["search_attribution"]["prefilter_exclude_only"] is True
    assert manifest["search_attribution"]["prefilter_enabled"] is False

    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "## 下载前预筛" in readme
    assert "排除词：豆包" in readme
    assert "仅排除词闸门生效" in readme


def test_exclude_gate_runs_when_prefilter_disabled_via_cli_flag(tmp_path: Path) -> None:
    # prefilter explicitly OFF and no config term: the CLI flag alone must bite.
    config = _config(tmp_path, prefilter={"enabled": False})
    rows = [
        _row("keep", "作者A", title="苹果折叠屏实测", url="https://signed.example/1"),
        _row("drop", "作者B", title="豆包抢购苹果18", url="https://signed.example/2"),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏", business_date="2026-09-12", download_only=True, deps=deps,
        exclude_terms=["豆包"],
    )
    assert downloaded == ["keep"], downloaded
    out = Path(result["output_dir"])
    prefilter = json.loads((out / "05-过程数据" / "prefilter.json").read_text(encoding="utf-8"))
    assert prefilter["exclude_only"] is True
    assert prefilter["config"]["exclude_terms"] == ["豆包"]
    assert "排除词：豆包" in (out / "00-交付说明.md").read_text(encoding="utf-8")


def test_exclude_only_can_clear_the_pool_with_an_auditable_prefiltered_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, prefilter={"enabled": False, "exclude_terms": ["豆包"]})
    rows = [_row("only", "作者A", title="豆包抢苹果18", url="https://signed.example/1")]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏", business_date="2026-09-12", download_only=True, deps=deps,
    )
    assert downloaded == []
    out = Path(result["output_dir"])
    assert (out / "05-过程数据" / "prefilter.json").exists()
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
    # Reuses the existing "prefiltered_empty" conclusion -- not a new branch.
    assert manifest["prefilter"]["conclusion"] == "prefiltered_empty"
    assert manifest["prefilter"]["exclude_only"] is True
    assert any("全部被下载前预筛剔除" in warning for warning in manifest["warnings"])
    run_log = json.loads((out / "05-过程数据" / "run_log.json").read_text(encoding="utf-8"))
    assert run_log["prefilter"]["exclude_only"] is True


def test_disabled_prefilter_without_exclude_terms_is_a_field_for_field_noop(tmp_path: Path) -> None:
    """Contract: with no exclude terms and the switch off, nothing changes.

    The shipped config has ``enabled: false`` is *not* the default, but this pins
    the invariant that the exclude gate introduces no behaviour when empty: an
    absent block and an ``enabled: false`` block produce identical deliveries and
    none of the three layers emit an artifact.
    """
    def _run(base: Path, prefilter: dict | None):
        config = _config(base, prefilter=prefilter)
        rows = [
            _row("kept", "作者A", title="苹果折叠屏实测", url="https://signed.example/1"),
            _row("short", "作者B", title="苹果折叠屏开箱", duration=3, url="https://signed.example/2"),
        ]
        deps, downloaded = _deps(base, rows)
        result = run_material_replication(
            config, "苹果折叠屏", business_date="2026-09-12", download_only=True, deps=deps,
        )
        out = Path(result["output_dir"])
        manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))
        for item in manifest.get("downloads") or []:
            item.pop("media_path", None)
        return out, manifest, downloaded

    (tmp_path / "absent").mkdir()
    (tmp_path / "disabled").mkdir()
    out_absent, man_absent, dl_absent = _run(tmp_path / "absent", None)
    out_disabled, man_disabled, dl_disabled = _run(tmp_path / "disabled", {"enabled": False})

    assert dl_absent == dl_disabled == ["kept", "short"]
    for out, manifest in ((out_absent, man_absent), (out_disabled, man_disabled)):
        assert "prefilter" not in manifest
        assert "download_budget" not in manifest
        assert "validation" not in manifest
        process = out / "05-过程数据"
        assert not (process / "prefilter.json").exists()
        assert not (process / "download_budget.json").exists()
        assert not (process / "validation.json").exists()
        assert "下载前预筛" not in (out / "00-交付说明.md").read_text(encoding="utf-8")

    man_absent.pop("generated_at"), man_disabled.pop("generated_at")
    assert man_absent == man_disabled


def test_cli_exclude_term_is_repeatable() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["material-replication", "run", "--theme", "苹果折叠屏", "--exclude-term", "豆包", "--exclude-term", "抢购"]
    )
    assert args.exclude_term == ["豆包", "抢购"]
    # Absent -> None (the pipeline treats None as "nothing appended").
    args_default = parser.parse_args(["material-replication", "run", "--theme", "苹果折叠屏"])
    assert args_default.exclude_term is None


def test_cli_dispatches_exclude_terms_to_the_runner(monkeypatch) -> None:
    import douyin_intelligence.cli as cli
    import douyin_intelligence.replication_pipeline as pipeline

    captured: dict = {}

    def fake_run(config, theme, **kwargs):
        captured["theme"] = theme
        captured["exclude_terms"] = kwargs.get("exclude_terms")
        return {"status": "success"}

    monkeypatch.setattr(pipeline, "run_material_replication", fake_run)
    exit_code = cli.main(
        ["material-replication", "run", "--theme", "苹果折叠屏", "--exclude-term", "豆包", "--exclude-term", "抢购"]
    )
    assert exit_code == 0
    assert captured["theme"] == "苹果折叠屏"
    assert captured["exclude_terms"] == ["豆包", "抢购"]


def test_run_appends_cli_exclude_terms_to_config_values(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        prefilter={
            "enabled": True, "min_seconds": 10, "max_seconds": 300,
            "allow_unknown_duration": True, "exclude_terms": ["带货"],
        },
    )
    rows = [
        _row("keep", "作者A", title="苹果折叠屏实测", url="https://signed.example/keep"),
        _row("cli-drop", "作者B", title="豆包抢购苹果18", url="https://signed.example/cli"),
        _row("cfg-drop", "作者C", title="带货苹果18现货", url="https://signed.example/cfg"),
    ]
    deps, downloaded = _deps(tmp_path, rows)
    result = run_material_replication(
        config, "苹果折叠屏", business_date="2026-09-12", download_only=True, deps=deps,
        exclude_terms=["豆包"],
    )
    # Only the untouched candidate survives; both the config term and the CLI term bit.
    assert downloaded == ["keep"], downloaded
    output_dir = Path(result["output_dir"])
    prefilter = json.loads((output_dir / "05-过程数据" / "prefilter.json").read_text(encoding="utf-8"))
    # Appended, not overwritten: config value first, CLI value second.
    assert prefilter["config"]["exclude_terms"] == ["带货", "豆包"]
    stages = {entry["stage"] for entry in prefilter["rejections"]}
    assert stages == {"pre_exclude"}
    readme = (output_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "排除词：带货、豆包" in readme


# --------------------------------------------------------------------------- #
# D: ordering is independent of the budget switch
# --------------------------------------------------------------------------- #
def test_download_order_is_independent_of_the_budget_switch(tmp_path: Path) -> None:
    def _run(base: Path, budget: dict | None) -> list[dict]:
        config = _config(base, prefilter={"enabled": False}, budget=budget)
        rows = [
            _row("v-hot", "作者C", title="完全无关的爆款", digg=999, url="https://signed.example/hot"),
            _row("v-relevant", "作者B", title="苹果折叠屏手机 铰链实拍", digg=5, url="https://signed.example/rel"),
            _row("v-low", "作者A", title="无所谓", digg=10, url="https://signed.example/low"),
        ]
        deps, _ = _deps(base, rows)
        result = run_material_replication(
            config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
        )
        return result["downloads"]

    (tmp_path / "no_budget").mkdir()
    (tmp_path / "with_budget").mkdir()
    without = _run(tmp_path / "no_budget", None)
    with_budget = _run(
        tmp_path / "with_budget",
        {"enabled": True, "max_count": 10 ** 9, "max_bytes": 10 ** 15, "max_item_bytes": 10 ** 15},
    )
    # The most relevant candidate leads in *both* runs: the budget only decides how
    # many are fetched, never the order (a pure-heat fallback would pick v-hot first).
    assert without[0]["video_id"] == "v-relevant"
    assert with_budget[0]["video_id"] == "v-relevant"
    assert [d["video_id"] for d in without] == [d["video_id"] for d in with_budget]


# --------------------------------------------------------------------------- #
# E: keyword-coverage floor + truncation visibility
# --------------------------------------------------------------------------- #
def test_min_searched_keywords_raises_the_pool_budget(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["min_pool_size"] = 10
    config["jobs"]["material_replication"]["default_pool_size"] = 40
    config["jobs"]["material_replication"]["max_pool_size"] = 120
    config["jobs"]["material_replication"]["search"] = {"min_searched_keywords": 9}
    seen: dict = {}

    def collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        seen["budget"] = budget
        seen["searched"] = list(keywords)[: budget // 10]
        return {"status": "success", "keywords": list(keywords)[: budget // 10], "budget": budget}

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=40, run_id="r", deps=types.SimpleNamespace(collector=collector),
    )
    # 40 // 10 = 4 keywords requested; the floor forces 9*10 = 90.
    assert seen["budget"] == 90
    assert len(seen["searched"]) == 9
    assert pool["budget"] == 90


def test_min_searched_keywords_is_capped_by_max_pool_and_warns(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["min_pool_size"] = 10
    config["jobs"]["material_replication"]["default_pool_size"] = 40
    config["jobs"]["material_replication"]["max_pool_size"] = 60
    config["jobs"]["material_replication"]["search"] = {"min_searched_keywords": 9}

    def collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        return {"status": "success", "keywords": list(keywords)[: budget // 10], "budget": budget}

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=40, run_id="r", deps=types.SimpleNamespace(collector=collector),
    )
    # 9 keywords need budget 90 > max_pool 60, so it is capped at 60 (6 keywords).
    assert pool["budget"] == 60
    assert any("受 max_pool_size（60）限制" in warning for warning in pool["warnings"])


def test_min_searched_keywords_unset_leaves_the_budget_unchanged(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["min_pool_size"] = 10
    config["jobs"]["material_replication"]["default_pool_size"] = 40
    config["jobs"]["material_replication"]["max_pool_size"] = 120
    config["jobs"]["material_replication"]["search"] = {"_comment": "no min set"}
    seen: dict = {}

    def collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        seen["budget"] = budget
        return {"status": "success", "keywords": keywords, "budget": budget}

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=40, run_id="r", deps=types.SimpleNamespace(collector=collector),
    )
    assert seen["budget"] == 40 and pool["budget"] == 40
    assert not any("max_pool_size" in warning for warning in pool["warnings"])


def test_keyword_truncation_warning_surfaces_for_a_large_pool(tmp_path: Path) -> None:
    """A pool above ``min_pool_size`` must still report a keyword cut."""
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["material_replication"]["min_pool_size"] = 2
    config["jobs"]["material_replication"]["max_pool_size"] = 120
    rows = [_row(f"v{i:02d}", f"作者{i}", url=f"https://signed.example/{i}") for i in range(3)]

    def truncating_collector(cfg, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(cfg.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": list(keywords)[:4], "budget": budget}

    pool = collect_candidate_pool(
        config, "苹果折叠屏", pool_size=80, run_id="r",
        deps=types.SimpleNamespace(collector=truncating_collector),
    )
    requested = pool["keywords_requested"]
    assert len(pool["keywords_used"]) == 4 and len(requested) > 4
    # The pool cleared min_pool, so no shortfall warning -- but the cut is still visible.
    assert not any("低于最小目标" in warning for warning in pool["warnings"])
    assert any(
        f"请求 {len(requested)} 个词、实际搜索 4 个（发生截断）" in warning for warning in pool["warnings"]
    )


# --------------------------------------------------------------------------- #
# Real-pool regression (no network): 61 candidates, denominator 7, 28 zeroed
# --------------------------------------------------------------------------- #
def _load_pool(path: Path) -> tuple[str, list[str], list[Candidate]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = [
        Candidate(video_id=row["video_id"], title=row.get("title") or "", source_keyword=row.get("source_keyword") or "")
        for row in payload["candidates"]
    ]
    return payload["theme"], payload.get("keywords_requested") or [], candidates


def test_fixture_pool_relevance_regression() -> None:
    """Unconditional, no-network regression on the controlled pool fixture.

    ``output/`` is gitignored, so the real delivery artifact is not available in a
    clean checkout; the fixture (committed under ``tests/fixtures/``) pins the same
    numbers so this assertion always runs.
    """
    assert FIXTURE.exists(), "受控回归 fixture 必须随仓库提供"
    theme, keywords_requested, candidates = _load_pool(FIXTURE)
    assert len(candidates) == 61

    report = relevance_report(candidates, theme, keywords_requested)
    # 10 requested terms -> 7 are live in this pool; the 3 dead ones drop out.
    assert len(keywords_requested) == 10
    assert report["live_count"] == 7
    assert set(report["dead_terms"]) == {"苹果折叠屏 实测", "Apple折叠屏", "苹果折叠屏 评测"}
    assert report["degraded"] is False
    # Five distinct tiers, and 28 candidates score zero: the denominator excludes
    # the dead terms so the surviving scores span [0, 1].
    distribution = Counter(report["scores"].values())
    assert distribution[0.0] == 28
    assert sorted(distribution) == [0.0, 0.142857, 0.285714, 0.428571, 0.571429]
    assert sum(distribution.values()) == 61


def test_real_pool_artifact_matches_the_fixture_when_present() -> None:
    """Extra cross-check: the real artifact, when it happens to be on disk."""
    if not REAL_POOL.exists():
        pytest.skip("真实交付产物不在仓库中（output/ 被 .gitignore）；受控 fixture 已覆盖该回归")
    theme, keywords_requested, candidates = _load_pool(REAL_POOL)
    report = relevance_report(candidates, theme, keywords_requested)
    assert report["live_count"] == 7
    distribution = Counter(report["scores"].values())
    assert distribution[0.0] == 28
    assert sorted(distribution) == [0.0, 0.142857, 0.285714, 0.428571, 0.571429]
    assert sum(distribution.values()) == 61
