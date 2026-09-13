"""Independent QA verification of the "delivered bytes -> real transferred bytes" change.

Written by the QA engineer (task #13) to *independently* re-derive the claims of
the engineer's fix, not to re-run the engineer's own tests.  Everything here is a
fresh harness: a fake downloader that records the exact bytes it writes, a
discriminating validator, and hand-built candidate batches.

Covered (V-numbers map to the review brief):

* V1  a rejected-after-download file is charged to ``transferred_bytes``;
* V2  ``max(delivered, transferred)`` is load-bearing -- the delivered and wire
      ceilings are both hard bounds, under all-cache and all-real extremes;
* V3  the stop semantics + the "starve the item count" exchange ratio;
* V4  zero-byte failures charge nothing and free their slot; a cache hit grows
      ``delivered_bytes`` only;
* V7  ``bytes`` stays an exact alias of ``delivered_bytes``; the two
      ``face_truncated_samples`` aggregation branches carry identical fields.

No source file is modified by this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_intelligence.config import load_config
from douyin_intelligence.replication_pipeline import ReplicationDeps, run_material_replication
from douyin_intelligence.replication_selection import DownloadBudget


# --------------------------------------------------------------------------- #
# Shared harness
# --------------------------------------------------------------------------- #
def _row(video_id: str, author: str, *, duration: float = 60.0, digg: int = 100) -> dict:
    return {
        "aweme_id": video_id,
        "desc": f"标题-{video_id}",
        "author": {"uid": f"uid-{author}", "nickname": author},
        "create_time": "2026-09-11T08:00:00+08:00",
        "statistics": {"digg_count": digg, "comment_count": 10, "share_count": 5, "collect_count": 20},
        "duration": duration,
        "video_download_url": f"https://signed.example/{video_id}",
        "share_url": f"https://www.douyin.com/video/{video_id}",
    }


def _collector(rows: list[dict]):
    def collect(config, budget, *, run_id=None, keywords=None, hard_max=None, before_sanitize=None):
        source = Path(str(config.get("_project_root"))) / "raw" / "search_contents_1.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before_sanitize([source])
        return {"status": "success", "keywords": keywords, "budget": budget}
    return collect


def _config(tmp_path: Path, *, budget=None, prefilter=None, validation=None) -> dict:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    mr = config["jobs"]["material_replication"]
    mr["prefilter"] = prefilter or {"enabled": False}
    if budget is None:
        mr.pop("download_budget", None)
    else:
        mr["download_budget"] = budget
    if validation is None:
        mr.pop("validation", None)
    else:
        mr["validation"] = validation
    return config


_BUDGET_OPEN = {"enabled": True, "max_count": 100, "max_bytes": 10 ** 9, "max_item_bytes": 10 ** 9}
_PREFILTER_OPEN = {
    "enabled": True, "min_seconds": 10, "max_seconds": 300,
    "heat_gate_percentile": 0.0, "allow_unknown_duration": True,
}


def _budget_block(result: dict) -> dict:
    out = Path(result["output_dir"])
    return json.loads((out / "05-过程数据" / "download_budget.json").read_text(encoding="utf-8"))


def _run(tmp_path, rows, *, config, downloader, prober, validator=None):
    deps = ReplicationDeps(collector=_collector(rows), downloader=downloader, prober=prober,
                           validator=validator)
    return run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )


# --------------------------------------------------------------------------- #
# V1 -- the whole point: a downloaded-then-rejected file still cost bandwidth
# --------------------------------------------------------------------------- #
def test_v1_every_written_byte_is_charged_even_when_the_file_is_rejected(tmp_path: Path) -> None:
    """Three candidates: one delivered, one `undecodable`, one outside the window.

    A fake downloader records the exact payload size it wrote for each id; the
    assertion is that ``transferred_bytes`` equals the sum over *all three*, not
    just the delivered one.  If the rejected pair were uncharged the fix is fake.
    """
    sizes = {"ok": 1000, "bad": 2000, "short": 3000}
    written: dict[str, int] = {}

    def downloader(url, destination, config, *, max_bytes=None):
        vid = Path(destination).stem
        payload = b"x" * sizes[vid]
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(payload)
        written[vid] = len(payload)

    def prober(path, config):
        vid = Path(path).stem
        duration = 5.0 if vid == "short" else 60.0  # "short" -> below the 10s floor
        return {"duration_seconds": duration, "width": 1080, "height": 1920, "codec": "h264"}

    def validator(video_path, config, **kwargs):
        vid = Path(video_path).stem
        if vid == "bad":
            return {"video_id": vid, "conclusion": "undecodable", "passed": False, "error": "simulated"}
        return {"video_id": vid, "conclusion": "ok", "passed": True}

    rows = [
        _row("ok", "作者A"),
        _row("bad", "作者B"),
        _row("short", "作者C", duration=0.0),  # no metadata duration -> duration_post window applies
    ]
    config = _config(
        tmp_path, budget=dict(_BUDGET_OPEN), prefilter=dict(_PREFILTER_OPEN),
        validation={"enabled": True, "full_decode": True, "decode_time_budget_seconds": 20,
                    "duration_tolerance": 0.05, "require_metadata_duration": False,
                    "cache_attestation": True},
    )
    result = _run(tmp_path, rows, config=config, downloader=downloader, prober=prober, validator=validator)
    used = _budget_block(result)["used"]

    # All three candidates actually went over the wire ...
    assert written == sizes
    # ... and the wire ledger equals their sum -- rejected included.
    assert used["transferred_bytes"] == sum(sizes.values())
    # Only "ok" was delivered, so the *delivered* ledger is much smaller.
    assert used["count"] == 1
    assert used["delivered_bytes"] == sizes["ok"]
    # Attribution: the two rejections are visible on the failure list.
    stages = {item["stage"] for item in result["failures"]}
    assert "validation" in stages
    assert "duration_post" in stages


def test_v1_skip_alone_charges_nothing_so_mark_transferred_is_load_bearing(tmp_path: Path) -> None:
    """The counter-proof: ``skip()`` on its own moves neither ledger.

    This shows that without the explicit ``mark_transferred()`` call in the
    download loops a rejected file would be charged nothing -- i.e. that call is
    what makes V1 hold, not an accident of ``skip``.
    """
    from types import SimpleNamespace

    budget = DownloadBudget(max_count=10, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    candidate = SimpleNamespace(video_id="v", title="t", author="a", heat_score=0.5)
    budget.skip(candidate, "validation", "undecodable")
    assert budget.bytes == 0
    assert budget.transferred_bytes == 0
    budget.mark_transferred(1234)
    assert budget.transferred_bytes == 1234
    assert budget.bytes == 0  # a rejected file never grows the delivered ledger


# --------------------------------------------------------------------------- #
# V2 -- max(delivered, transferred) is load-bearing, and both ceilings hold
# --------------------------------------------------------------------------- #
def test_v2_delivered_and_transferred_never_cross_the_ceiling(tmp_path: Path) -> None:
    """All-real-download extreme: a rejecting batch cannot push either ledger over."""
    item = 4096
    config = _config(
        tmp_path, budget={"enabled": True, "max_count": 0, "max_bytes": 5 * item, "max_item_bytes": 10 ** 9},
        prefilter={**dict(_PREFILTER_OPEN), "enabled": False},
        validation={"enabled": True, "full_decode": True, "decode_time_budget_seconds": 20,
                    "duration_tolerance": 0.05, "require_metadata_duration": False,
                    "cache_attestation": True},
    )

    written: dict[str, int] = {}

    def downloader(url, destination, config, *, max_bytes=None):
        vid = Path(destination).stem
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * item)
        written[vid] = item

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    def validator(video_path, config, **kwargs):
        return {"video_id": Path(video_path).stem, "conclusion": "undecodable", "passed": False}

    rows = [_row(f"v{i:02d}", f"作者{i}") for i in range(20)]
    result = _run(tmp_path, rows, config=config, downloader=downloader, prober=prober, validator=validator)
    used = _budget_block(result)["used"]

    assert used["delivered_bytes"] <= 5 * item
    assert used["transferred_bytes"] <= 5 * item
    assert used["transferred_bytes"] == sum(written.values()) == 5 * item
    assert used["delivered_bytes"] == 0


def test_v2_item_cap_shrinks_to_the_remaining_wire_budget() -> None:
    """The per-download cap must track *real* remaining budget, not the delivered one.

    With 900 wire bytes already spent on rejected files and an empty delivered
    ledger, the next download may only ask for 100 more -- proving ``item_cap``
    is measured on ``max(delivered, transferred)``.
    """
    budget = DownloadBudget(max_count=100, max_bytes=1000, max_item_bytes=10 ** 9)
    budget.transferred_bytes = 900
    budget.bytes = 0
    assert budget.remaining_bytes() == 100
    assert budget.item_cap() == 100
    # A cap-respecting downloader that writes <= cap keeps the wire bound intact.
    budget.mark_transferred(budget.item_cap())
    assert budget.transferred_bytes == 1000
    assert budget.transferred_bytes <= 1000
    assert budget.allow()[0] is False
    assert budget.stopped_by == "transferred_bytes"


def test_v2_cache_hit_would_break_pure_transferred_accounting(tmp_path: Path) -> None:
    """The engineer's rationale, verified independently.

    With only ``transferred_bytes`` counted, an all-cache-hit run would never
    approach the ceiling (a cache hit writes zero wire bytes) and the *delivered*
    payload could exceed ``max_bytes``.  Taking the ``max`` of both ledgers keeps
    the delivered ceiling honest.  Uses the *real* ``materials.download_video``
    so its cache short-circuit is genuinely exercised.
    """
    per = 5000
    max_bytes = 2 * per  # room for exactly two cached files
    config = _config(
        tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": max_bytes, "max_item_bytes": 10 ** 9},
        prefilter={"enabled": False}, validation=None,
    )
    for index in range(5):
        cache_target = (
            __import__("douyin_intelligence.replication_theme", fromlist=["project_path"]).project_path(
                config, config["jobs"]["material_replication"]["media_root"]
            ) / "material" / f"v{index:04d}.mp4"
        )
        cache_target.parent.mkdir(parents=True, exist_ok=True)
        cache_target.write_bytes(b"c" * per)

    rows = [_row(f"v{i:04d}", f"作者{i}") for i in range(5)]
    deps = ReplicationDeps(
        collector=_collector(rows),
        prober=lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"},
    )
    result = run_material_replication(
        config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
    )
    block = _budget_block(result)
    used = block["used"]

    # No new traffic at all (every file came from cache) ...
    assert used["transferred_bytes"] == 0
    # ... yet the delivered ceiling is still honoured exactly.
    assert used["delivered_bytes"] == max_bytes
    assert used["delivered_bytes"] <= max_bytes
    assert block["stopped_by"] == "bytes"
    # A pure-transferred cap would have let all 5 cache hits through (5*per > cap).
    assert used["count"] == 2


# --------------------------------------------------------------------------- #
# V3 -- stop semantics + the item-count exchange ratio
# --------------------------------------------------------------------------- #
def test_v3_stop_reasons_are_mutually_exclusive() -> None:
    """``allow`` must attribute the *binding* limit, never conflate the two byte ledgers."""
    count_capped = DownloadBudget(max_count=1, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    count_capped.count = 1
    assert count_capped.allow() == (False, "已达下载条数上限 1 条")
    assert count_capped.stopped_by == "count"

    delivered_capped = DownloadBudget(max_count=100, max_bytes=1000, max_item_bytes=10 ** 9)
    delivered_capped.bytes = 1000
    delivered_capped.transferred_bytes = 200  # wire smaller than delivered
    assert delivered_capped.allow()[0] is False
    assert delivered_capped.stopped_by == "bytes"

    wire_capped = DownloadBudget(max_count=100, max_bytes=1000, max_item_bytes=10 ** 9)
    wire_capped.transferred_bytes = 1000
    wire_capped.bytes = 0
    assert wire_capped.allow()[0] is False
    assert wire_capped.stopped_by == "transferred_bytes"


def test_v3_starvation_exchange_ratio(tmp_path: Path) -> None:
    """A batch whose first candidates are all rejected can deliver *nothing*.

    Concretely: 20 candidates ordered v00..v19, the *first ten* rejected after a
    successful download, ``max_bytes = 5 * item``.  The wire ledger fills on the
    fifth file and the fifth-to-last never starts, so **0** items are delivered
    even though 15 healthy candidates remain.  Numbers are printed for the record.
    """
    item = 1000
    config = _config(
        tmp_path, budget={"enabled": True, "max_count": 12, "max_bytes": 5 * item, "max_item_bytes": 10 ** 9},
        prefilter={**dict(_PREFILTER_OPEN), "enabled": False},
        validation={"enabled": True, "full_decode": True, "decode_time_budget_seconds": 20,
                    "duration_tolerance": 0.05, "require_metadata_duration": False,
                    "cache_attestation": True},
    )
    written: dict[str, int] = {}
    rejected_ids = {f"v{i:02d}" for i in range(10)}  # the top ten (worst case)

    def downloader(url, destination, config, *, max_bytes=None):
        vid = Path(destination).stem
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * item)
        written[vid] = item

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    def validator(video_path, config, **kwargs):
        vid = Path(video_path).stem
        if vid in rejected_ids:
            return {"video_id": vid, "conclusion": "undecodable", "passed": False}
        return {"video_id": vid, "conclusion": "ok", "passed": True}

    rows = [_row(f"v{i:02d}", f"作者{i}") for i in range(20)]
    result = _run(tmp_path, rows, config=config, downloader=downloader, prober=prober, validator=validator)
    block = _budget_block(result)
    used = block["used"]

    delivered = used["count"]
    transferred = used["transferred_bytes"]
    downloaded = len(written)
    print(f"\n[V3 starvation] delivered={delivered} downloaded={downloaded} "
          f"transferred={transferred} max_bytes={5 * item} stopped_by={block['stopped_by']}")

    assert downloaded == 5
    assert delivered == 0
    assert transferred == 5 * item
    assert block["stopped_by"] == "transferred_bytes"
    # ... while 15 non-rejected candidates were never even attempted (the budget
    # stop is recorded as a budget *skip*, not as a download failure).
    assert any(item["stage"] == "budget" for item in block["skipped"])


# --------------------------------------------------------------------------- #
# V4 -- zero-byte failures and cache hits
# --------------------------------------------------------------------------- #
def test_v4_zero_byte_failures_charge_nothing_and_free_their_slot(tmp_path: Path) -> None:
    """A network error and a pre-body oversize cost zero, and do not hold a slot."""
    item = 1000
    config = _config(
        tmp_path, budget={"enabled": True, "max_count": 1, "max_bytes": 10 ** 9, "max_item_bytes": 2000},
        prefilter={"enabled": False}, validation=None,
    )
    written: dict[str, int] = {}

    def downloader(url, destination, config, *, max_bytes=None):
        vid = Path(destination).stem
        if vid == "v00":
            raise OSError("simulated network failure")  # zero bytes
        if vid == "v01":
            from douyin_intelligence.materials import MediaTooLargeError
            raise MediaTooLargeError("declared oversize", declared_bytes=10 ** 9, limit=max_bytes or 0)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * item)
        written[vid] = item

    def prober(path, config):
        return {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"}

    rows = [_row("v00", "作者0"), _row("v01", "作者1"), _row("v02", "作者2")]
    result = _run(tmp_path, rows, config=config, downloader=downloader, prober=prober)
    used = _budget_block(result)["used"]

    assert sorted(written) == ["v02"]  # only the third candidate wrote bytes
    assert used["transferred_bytes"] == item
    assert used["delivered_bytes"] == item
    assert used["count"] == 1  # the two failures did *not* consume the single slot


# --------------------------------------------------------------------------- #
# V7 -- backwards-compatible alias + face_truncated_samples field parity
# --------------------------------------------------------------------------- #
def test_v7_bytes_is_an_exact_alias_of_delivered_bytes() -> None:
    budget = DownloadBudget(max_count=10, max_bytes=10 ** 9, max_item_bytes=10 ** 9)
    budget.bytes = 4321
    budget.transferred_bytes = 9999
    used = budget.snapshot()["used"]
    assert used["bytes"] == used["delivered_bytes"] == 4321
    assert used["transferred_bytes"] == 9999


def _run_full_chain_with_face(tmp_path, rows, face):
    import douyin_intelligence.replication_pipeline as pipe
    import douyin_intelligence.replication_selection as sel

    config = _config(tmp_path, budget=None, prefilter={"enabled": False}, validation=None)

    def downloader(url, destination, config, *, max_bytes=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 2048)

    class _Ocr:
        def run(self, video, duration, cache_dir, temp_dir):
            return {"status": "no_text", "items": [], "sampled_frames": 10}

    class _Transcriber:
        def run(self, video, cache_dir, temp_dir, **kwargs):
            return {"status": "no_speech", "text": "", "segments": []}

    deps = ReplicationDeps(
        collector=_collector(rows), downloader=downloader,
        prober=lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"},
        validator=lambda path, config, **kwargs: {"video_id": Path(path).stem, "conclusion": "ok", "passed": True},
        transcriber=_Transcriber(), ocr=_Ocr(), face_detector=face,
    )
    original_tool = pipe.media_tool_available
    original_visual = sel.compute_visual_metrics
    pipe.media_tool_available = lambda config, name: True
    sel.compute_visual_metrics = lambda *a, **k: sel.VisualMetrics(
        sampled_frames=10, motion_frame_ratio=0.9, ocr_text_frame_ratio=0.0, visual_ok=True)
    try:
        result = run_material_replication(config, "苹果折叠屏手机", business_date="2026-09-12", deps=deps)
    finally:
        pipe.media_tool_available = original_tool
        sel.compute_visual_metrics = original_visual
    return Path(result["output_dir"])


class _MixedTruncatingFace:
    """Severe truncation for even ids (rejected), mild for odd ids (delivered)."""

    backend = "opencv_yunet"

    def status(self):
        return {"backend": "opencv_yunet", "status": "ok", "model_present": True}

    def run(self, video, duration, cache_dir, temp_dir):
        index = int(Path(video).stem.lstrip("v"))
        severe = index % 2 == 0
        emitted = 13 if severe else 50
        return {
            "backend": "opencv_yunet", "status": "ok", "face_frame_ratio": 0.0,
            "max_face_area_ratio": 0.0, "face_class": "face_free", "face_class_reason": "",
            "sampled_frames": emitted, "expected_frames": 60, "emitted_frames": emitted,
            "sample_coverage": emitted / 60, "truncated": True,
            "low_confidence": True, "face_per_frame": [False] * emitted,
            "sample_interval_seconds": 1,
        }


def test_v7_budget_layer_is_additive_only(tmp_path: Path) -> None:
    """Enabling the budget may only *add* keys/fields -- never remove or change one.

    Runs the same candidate batch with the budget absent and with a non-binding
    budget (huge ceilings), so behaviour is identical and any difference is pure
    addition.  Every key of the no-budget manifest must survive in the budget
    manifest with the same value (downloads may only gain the new
    ``relevance_score``).
    """
    def _run_variant(base: Path, budget):
        config = _config(base, budget=budget, prefilter={"enabled": False}, validation=None)
        rows = [_row(f"v{i:02d}", f"作者{i}") for i in range(3)]

        def downloader(url, destination, config, *, max_bytes=None):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"x" * 2048)

        deps = ReplicationDeps(
            collector=_collector(rows), downloader=downloader,
            prober=lambda path, config: {"duration_seconds": 60.0, "width": 1080, "height": 1920, "codec": "h264"},
        )
        result = run_material_replication(
            config, "苹果折叠屏手机", business_date="2026-09-12", download_only=True, deps=deps,
        )
        manifest = json.loads((Path(result["output_dir"]) / "清单.json").read_text(encoding="utf-8"))
        for item in manifest.get("downloads") or []:
            item.pop("media_path", None)
        return manifest

    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    without = _run_variant(tmp_path / "a", None)
    with_budget = _run_variant(tmp_path / "b", {"enabled": True, "max_count": 10 ** 9,
                                                "max_bytes": 10 ** 15, "max_item_bytes": 10 ** 15})
    without.pop("generated_at"); with_budget.pop("generated_at")

    # No key of the pre-budget manifest was dropped ...
    assert set(without.keys()) - set(with_budget.keys()) == set()
    # ... and every non-download key kept its value exactly.
    for key, value in without.items():
        if key == "downloads":
            continue
        assert with_budget[key] == value, f"budget changed existing key {key!r}"
    # A download record may only gain the additive ``relevance_score`` field.
    assert len(without["downloads"]) == len(with_budget["downloads"])
    for old, new in zip(without["downloads"], with_budget["downloads"]):
        assert set(old.items()) <= set(new.items()), "an existing download field changed value"


def test_v7_face_truncated_samples_field_parity_across_branches(tmp_path: Path) -> None:
    """Both aggregation branches (delivered & rejected) must carry identical fields."""
    rows = [_row(f"v{i:02d}", f"作者{i}") for i in range(4)]
    out = _run_full_chain_with_face(tmp_path, rows, _MixedTruncatingFace())
    manifest = json.loads((out / "清单.json").read_text(encoding="utf-8"))

    samples = manifest.get("face_truncated_samples") or []
    assert samples, "the mixed batch must surface truncated samples"
    flags = {item.get("truncated") for item in samples}
    assert flags == {True}
    delivered_flags = {item.get("delivered") for item in samples}
    assert delivered_flags == {True, False}, "both branches must be exercised for a parity check"
    key_sets = {frozenset(item.keys()) for item in samples}
    assert len(key_sets) == 1, f"field sets differ between branches: {key_sets}"
