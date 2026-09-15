from __future__ import annotations

from pathlib import Path
import shutil

from douyin_intelligence.config import load_config
from douyin_intelligence.normalize import load_raw_records, normalize_files, parse_count, parse_datetime


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_count_handles_chinese_and_compact_units() -> None:
    assert parse_count("1.2万") == 12_000
    assert parse_count("3.5k") == 3_500
    assert parse_count("1亿") == 100_000_000
    assert parse_count(None) is None
    assert parse_count("unknown") is None


def test_parse_datetime_handles_milliseconds_and_iso() -> None:
    iso = parse_datetime(1787623200000, "Asia/Shanghai")
    assert iso == "2026-08-25T10:00:00+08:00"
    assert parse_datetime("2026-08-25T12:00:00+08:00", "Asia/Shanghai") == "2026-08-25T12:00:00+08:00"


def test_normalize_flattened_mediacrawler_jsonl() -> None:
    records = normalize_files([FIXTURES / "creator_contents_2026-08-25.jsonl"], load_config())
    assert len(records) == 4
    assert records[0].source == "douyin_creator"
    assert records[0].digg_count == 12_000
    assert records[0].collect_count == 600
    assert records[0].category == "hardware_products"
    assert records[0].play_count is None
    assert "play_count" not in records[0].missing_fields


def test_normalize_nested_api_json_and_infers_search_source() -> None:
    records = normalize_files([FIXTURES / "search_contents_2026-08-25.json"], load_config())
    assert len(records) == 2
    assert records[1].source == "douyin_search"
    assert records[1].account_name == "开源观察"
    assert records[1].digg_count == 35_000
    assert records[1].category == "open_source"


def test_creator_directory_restores_configured_account_identity(tmp_path: Path) -> None:
    account_dir = tmp_path / "creator" / "48304051157" / "douyin" / "jsonl"
    account_dir.mkdir(parents=True)
    destination = account_dir / "creator_contents_2026-08-25.jsonl"
    shutil.copyfile(FIXTURES / "creator_contents_2026-08-25.jsonl", destination)
    records = normalize_files([destination], load_config())
    assert records[0].account_id == "48304051157"
    assert records[0].account_name == "benchmark-48304051157"


def test_load_raw_records_rejoins_a_record_split_by_a_raw_newline(tmp_path: Path) -> None:
    """A title containing a raw newline must not void the whole file.

    The 2026-09-16 ``理想i9`` run lost its **entire** candidate pool to one such
    record: the crawler wrote the title unescaped, so a single JSON object
    arrived as three physical lines and the old strict rule rejected the file
    (127 lines, one split).  The joined text must come back verbatim.
    """
    path = tmp_path / "search_contents_2026-09-16.jsonl"
    path.write_text(
        '{"aweme_id": "1", "title": "第一行\n第二行", "desc": "d"}\n'
        '{"aweme_id": "2", "title": "ok", "desc": "d"}\n',
        encoding="utf-8",
    )
    rows = load_raw_records(path)
    assert [row["aweme_id"] for row in rows] == ["1", "2"]
    assert rows[0]["title"] == "第一行\n第二行"


def test_load_raw_records_skips_only_the_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"aweme_id": "1"}\nnot json at all\n{"aweme_id": "2"}\n', encoding="utf-8")
    rows = load_raw_records(path)
    assert [row["aweme_id"] for row in rows] == ["1", "2"]


def test_load_raw_records_all_bad_still_yields_empty(tmp_path: Path) -> None:
    """Dropping bad lines must not turn a broken file into a silent success."""
    path = tmp_path / "x.jsonl"
    path.write_text("nonsense\nmore nonsense\n", encoding="utf-8")
    assert load_raw_records(path) == []
