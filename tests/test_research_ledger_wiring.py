"""Wiring proof: the agent research ledger is really *injected*, not merely stored.

The feature adds three seams to shipped code -- config validation of
``episode_research_pack.ledger_root``, the material-replication pipeline step that
feeds a discovered ledger into ``publish_from_delivery``, and the
``episode-research-pack`` CLI.  These tests assert on **outbound** values (the
``research_inputs`` argument actually handed to ``publish_from_delivery``) and on
**published pack bytes on disk**, never on a fixture the test just wrote back and
re-read: "write a file, read it back, call it wired" is exactly the tautology this
module exists to rule out.

Nothing here writes into the real repository tree: ``_project_root`` is redirected
to ``tmp_path`` for every pipeline/CLI test, so the published pack and its
``current.json`` land under the temporary directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from douyin_intelligence import cli
from douyin_intelligence import episode_research_pack as pack
from douyin_intelligence import replication_pipeline
from douyin_intelligence.config import ConfigurationError
from douyin_intelligence.episode_research_pack import _build_fixture_delivery, research_pack_settings
from douyin_intelligence.research_ledger import (
    LEDGER_SCHEMA,
    ResearchLedgerError,
    discover_research_ledger,
    load_research_ledger,
)

# ``load_config`` under a non-``load_config`` name on purpose: ``tests/conftest.py``
# monkeypatches the attribute literally named ``load_config`` on any test module
# that has one, in order to strip the production-enabled opt-in switches.  Config
# *validation* of ``ledger_root`` is the subject under test here, so the raw
# validator is what must run -- an alias keeps the seam from blanking the very key
# these cases assert on.
from douyin_intelligence.config import load_config as load_config_raw

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_CONFIG_PATH = _REPO_ROOT / "config" / "content_intelligence.json"
_REAL_LEDGER_PATH = _REPO_ROOT / "input" / "research-ledgers" / "2026-09-17-平陆运河.json"

_THEME = "平陆运河"
_DATE = "2026-09-17"


# --- Fixtures ---------------------------------------------------------------


def _research_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    ledger_root: str = "input/research-ledgers",
) -> dict:
    """A real, valid config whose project root and research-pack block point at tmp."""
    from douyin_intelligence.config import load_config

    config = load_config()
    config["_project_root"] = str(tmp_path)
    block = config["jobs"]["material_replication"].setdefault("episode_research_pack", {})
    block["enabled"] = enabled
    block["ledger_root"] = ledger_root
    block["output_root"] = "output/每期研究包"
    block["annotate_delivery_manifest"] = False
    return config


def _source(source_id: str, *, publisher: str, url: str) -> dict:
    return {
        "source_id": source_id,
        "publisher": publisher,
        "title": f"来源标题-{source_id}",
        "url": url,
        "published_at": "2026-09-16",
        "excerpt": "支持该主张的原文摘录。",
        "authority": "official",
        "verification_state": "verified",
        "heat_only": False,
        "freshness": {
            "observed_at": "2026-09-17T10:00:00+08:00",
            "policy": "event_window",
            "status_at_publish": "fresh",
        },
    }


def _claim(claim_id: str, *, text: str, source_id: str) -> dict:
    return {
        "claim_id": claim_id,
        "topic_id": "topic-01",
        "text": text,
        "evidence_status": "confirmed_official",
        "wording_policy": "assert",
        "freshness_requirement": "fresh",
        "source_ids": [source_id],
        "fact_sources_min": 1,
        "fact_sources_present": 1,
        "claims_to_verify": [],
        "do_not_claim": [],
        "material_refs": [],
    }


def _ledger(
    *,
    text: str,
    source_id: str = "s1",
    claim_id: str = "c1",
    url: str = "https://news.example/ledger",
) -> dict:
    return {
        "schema": LEDGER_SCHEMA,
        "theme": _THEME,
        "business_date": _DATE,
        "episode_id": f"{_DATE}-{_THEME}",
        "sources": [_source(source_id, publisher="新华社", url=url)],
        "claims": [_claim(claim_id, text=text, source_id=source_id)],
    }


def _write_ledger(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _tree_digest(root: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        if item.is_file():
            digest[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return digest


def _spy_publish(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace ``publish_from_delivery`` with a recorder; return the capture dict."""
    captured: dict = {}

    def fake(config, **kwargs):
        captured.update(kwargs)
        captured["_calls"] = captured.get("_calls", 0) + 1
        return {"status": "published", "warnings": [], "pack_path": "<spy>"}

    monkeypatch.setattr(pack, "publish_from_delivery", fake)
    return captured


def _pack_json(pack_path: str | Path, name: str) -> dict:
    return json.loads((Path(pack_path) / name).read_text(encoding="utf-8"))


# --- 1. ledger_root validation ---------------------------------------------


@pytest.mark.parametrize(
    "ledger_root",
    [
        "D:/outside/ledgers",  # absolute drive path
        "C:ledgers",  # drive-relative (Windows: .drive set)
        "//server/share/ledgers",  # UNC share
        "input/../ledgers",  # parent traversal
        "..",  # bare traversal
        ".",  # bare dot: Path(".").parts == () -> no drive/root/".." at all
        "./",  # same value, trailing separator
        "",  # empty string
        "   ",  # whitespace only
        123,  # wrong type
    ],
)
def test_ledger_root_rejects_illegal_values(tmp_path: Path, ledger_root) -> None:
    data = json.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))
    data["jobs"]["material_replication"]["episode_research_pack"]["ledger_root"] = ledger_root
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError) as caught:
        load_config_raw(bad)
    assert "ledger_root" in str(caught.value)


def test_ledger_root_rooted_path_must_not_escape_project(tmp_path: Path) -> None:
    data = json.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))
    data["jobs"]["material_replication"]["episode_research_pack"]["ledger_root"] = "/etc/ledgers"
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    try:
        config = load_config_raw(config_file)
    except ConfigurationError:
        return  # rejected outright -> requirement satisfied

    resolved = pack.project_path(config, research_pack_settings(config)["ledger_root"])
    assert resolved.is_relative_to(tmp_path), f"ledger_root escaped project root: {resolved}"


def test_ledger_root_bare_dot_must_not_collapse_to_project_root(tmp_path: Path) -> None:
    """A bare ``"."`` must be rejected rather than silently becoming the repo root.

    ``Path(".").parts`` is ``()``, and ``Path(".").is_absolute()`` / ``.drive`` /
    ``.root`` are all falsy and there is no ``..`` part -- so the "escape the
    project" checks above all pass.  ``project_path`` then resolves the ledger
    root to the project root itself, which would make every stray ``*.json`` in
    the repository a ledger candidate.
    """
    data = json.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))
    data["jobs"]["material_replication"]["episode_research_pack"]["ledger_root"] = "."
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError) as caught:
        load_config_raw(bad)
    assert "ledger_root" in str(caught.value)


def test_ledger_root_accepts_project_relative_path(tmp_path: Path) -> None:
    data = json.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))
    data["jobs"]["material_replication"]["episode_research_pack"]["ledger_root"] = "custom/ledgers-2026"
    good = tmp_path / "config.json"
    good.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    config = load_config_raw(good)
    assert research_pack_settings(config)["ledger_root"] == "custom/ledgers-2026"


def test_ledger_root_default_when_block_omits_key(tmp_path: Path) -> None:
    data = json.loads(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))
    data["jobs"]["material_replication"]["episode_research_pack"].pop("ledger_root", None)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    config = load_config_raw(config_file)
    assert research_pack_settings(config)["ledger_root"] == "input/research-ledgers"


# --- 2. pipeline injects a discovered ledger --------------------------------


def test_pipeline_passes_loaded_ledger_as_research_inputs(tmp_path: Path, monkeypatch) -> None:
    """The pipeline must hand the *loaded* ledger content to ``publish_from_delivery``."""
    config = _research_config(tmp_path)
    ledger_path = _write_ledger(
        tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json",
        _ledger(text="唯一主张文本-WIRING", url="https://news.example/wiring"),
    )
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "published"
    assert captured["_calls"] == 1
    assert captured["delivery_dir"] == delivery
    assert captured["annotate_delivery"] is False
    research_inputs = captured["research_inputs"]
    assert research_inputs is not None
    # Content, not identity: these are the exact bytes the test authored, carried
    # through discovery + load into the outbound argument.
    assert research_inputs["sources"][0]["url"] == "https://news.example/wiring"
    assert research_inputs["claims"][0]["text"] == "唯一主张文本-WIRING"
    assert any("研究台账已注入" in w for w in warnings)
    assert any(str(ledger_path.resolve()) in w for w in warnings)


def test_pipeline_publishes_real_pack_with_real_ledger_content(tmp_path: Path) -> None:
    """End-to-end through the real publisher: the shipped ledger reaches ``claims.json``."""
    assert _REAL_LEDGER_PATH.is_file(), f"交付件缺失：{_REAL_LEDGER_PATH}"
    ledger_target = _write_ledger(
        tmp_path / "input" / "research-ledgers" / _REAL_LEDGER_PATH.name,
        json.loads(_REAL_LEDGER_PATH.read_text(encoding="utf-8")),
    )
    config = _research_config(tmp_path)
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "published"
    pack_path = Path(result["pack_path"])
    claims = _pack_json(pack_path, "claims.json")["claims"]
    sources = _pack_json(pack_path, "sources.json")["sources"]
    episode = _pack_json(pack_path, "episode.json")
    assert len(claims) >= 15
    assert len(sources) >= 8
    assert episode["disposition"] in {"ready", "partial"}
    readme = (pack_path / "每期研究证据包.md").read_text(encoding="utf-8")
    assert "无第一手事实来源" not in readme
    assert "https://" in readme
    assert ledger_target.is_file()  # the ledger itself is untouched by publishing


# --- 3. canonical-but-illegal ledger is an explicit error -------------------


def test_canonical_invalid_ledger_errors_without_publishing(tmp_path: Path, monkeypatch) -> None:
    config = _research_config(tmp_path)
    payload = _ledger(text="不应发布的主张")
    payload["sources"][0]["authority"] = "forum"  # illegal frozen enum
    canonical = _write_ledger(
        tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json", payload
    )
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    before = _tree_digest(delivery)
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "error"
    assert "authority" in result["error"]
    assert captured.get("_calls", 0) == 0  # never published an empty/derived pack
    assert any("校验失败" in w for w in warnings)
    # old delivery neither deleted nor rewritten
    assert _tree_digest(delivery) == before
    assert (delivery / "清单.json").is_file()
    assert not list((tmp_path / "output" / "每期研究包").rglob("claims.json"))
    assert canonical.is_file()


def test_canonical_broken_json_is_error_not_silent_legacy(tmp_path: Path, monkeypatch) -> None:
    """A canonically named corrupt ledger must not degrade into ``research_required``."""
    config = _research_config(tmp_path)
    canonical = tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("{", encoding="utf-8")
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "error"
    assert captured.get("_calls", 0) == 0


# --- 3b. a canonical name cannot hide a different episode -------------------


def test_pipeline_errors_when_canonical_ledger_theme_mismatches(
    tmp_path: Path, monkeypatch
) -> None:
    """A canonical file name whose *content* is another episode must not be injected."""
    config = _research_config(tmp_path)
    payload = _ledger(text="别的主题的主张")
    payload["theme"] = "智元A3"
    canonical = _write_ledger(
        tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json", payload
    )
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "error"
    assert "theme" in result["error"]
    assert captured.get("_calls", 0) == 0
    assert canonical.is_file()


def test_pipeline_errors_when_canonical_ledger_date_mismatches(
    tmp_path: Path, monkeypatch
) -> None:
    config = _research_config(tmp_path)
    payload = _ledger(text="日期不符的主张")
    payload["business_date"] = "2020-01-01"
    _write_ledger(tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json", payload)
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "error"
    assert "business_date" in result["error"]
    assert captured.get("_calls", 0) == 0


def test_cli_build_explicit_ledger_theme_mismatch_is_rejected(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An explicit ``--ledger`` for another episode is never silently accepted."""
    config = _research_config(tmp_path)
    payload = _ledger(text="串题主张-EXPLICIT")
    payload["theme"] = "智元A3"
    explicit = _write_ledger(tmp_path / "elsewhere" / "explicit.json", payload)
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)

    code = cli.main(
        [
            "episode-research-pack", "build",
            "--delivery-folder", str(delivery),
            "--ledger", str(explicit),
        ]
    )

    assert code == 2
    assert "theme" in capsys.readouterr().err


def test_pipeline_errors_on_unparseable_non_canonical_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """A corrupt non-canonical candidate is an explicit error, never a silent no-op."""
    config = _research_config(tmp_path)
    root = tmp_path / "input" / "research-ledgers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "broken.json").write_text("{", encoding="utf-8")
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "error"
    assert "broken.json" in result["error"]
    assert captured.get("_calls", 0) == 0


# --- 4. no ledger keeps the legacy research_required behaviour --------------


def test_no_ledger_passes_none_and_keeps_research_required(tmp_path: Path, monkeypatch) -> None:
    config = _research_config(tmp_path, ledger_root="input/absent-ledgers")
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    captured = _spy_publish(monkeypatch)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None
    assert captured["_calls"] == 1
    assert captured["research_inputs"] is None
    assert not any("研究台账已注入" in w for w in warnings)


def test_no_ledger_publishes_empty_research_required_pack(tmp_path: Path) -> None:
    config = _research_config(tmp_path, ledger_root="input/absent-ledgers")
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    before = _tree_digest(delivery)
    warnings: list[str] = []

    result = replication_pipeline._maybe_publish_research_pack(
        config, destination=delivery, theme=_THEME, business_date=_DATE, warnings=warnings,
    )

    assert result is not None and result["status"] == "published"
    pack_path = Path(result["pack_path"])
    assert _pack_json(pack_path, "claims.json")["claims"] == []
    assert _pack_json(pack_path, "sources.json")["sources"] == []
    assert _pack_json(pack_path, "episode.json")["disposition"] == "research_required"
    readme = (pack_path / "每期研究证据包.md").read_text(encoding="utf-8")
    assert "无第一手事实来源" in readme
    assert _tree_digest(delivery) == before


# --- 5. CLI ledger-check / render-ledger ------------------------------------


def test_cli_ledger_check_exit_zero_for_real_ledger(capsys) -> None:
    assert _REAL_LEDGER_PATH.is_file(), f"交付件缺失：{_REAL_LEDGER_PATH}"
    code = cli.main(
        ["episode-research-pack", "ledger-check", "--ledger", str(_REAL_LEDGER_PATH)]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_cli_ledger_check_exit_three_for_illegal_ledger(tmp_path: Path, capsys) -> None:
    payload = _ledger(text="坏台账")
    payload["claims"][0]["evidence_status"] = "certain"  # illegal frozen enum
    bad = _write_ledger(tmp_path / "bad.json", payload)

    code = cli.main(["episode-research-pack", "ledger-check", "--ledger", str(bad)])

    assert code == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "fail"
    assert "evidence_status" in printed["errors"][0]


def test_cli_ledger_check_requires_ledger_flag(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["episode-research-pack", "ledger-check"])
    capsys.readouterr()


def test_cli_render_ledger_writes_real_markdown(tmp_path: Path, capsys) -> None:
    assert _REAL_LEDGER_PATH.is_file(), f"交付件缺失：{_REAL_LEDGER_PATH}"
    loaded = load_research_ledger(_REAL_LEDGER_PATH)
    out = tmp_path / "事实台账.md"

    code = cli.main(
        ["episode-research-pack", "render-ledger", "--ledger", str(_REAL_LEDGER_PATH), "--out", str(out)]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rendered"
    assert out.is_file()
    markdown = out.read_text(encoding="utf-8")
    assert len(markdown) > 1500  # not just headers
    assert "## 事实来源" in markdown
    assert "## 主张与证据状态" in markdown
    assert "## 冲突与待核" in markdown
    assert markdown.count("https://") >= 8  # every real source keeps its URL
    assert "无第一手事实来源" not in markdown
    assert "并列来源" in markdown  # conflict section lists the conflicting claims
    assert loaded["claims"][0]["text"] in markdown  # real claim text was rendered


# --- 6. CLI build: explicit --ledger beats auto-discovery -------------------


def _run_build(monkeypatch, config: dict, delivery: Path, *, ledger: Path | None) -> dict:
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    argv = ["episode-research-pack", "build", "--delivery-folder", str(delivery)]
    if ledger is not None:
        argv += ["--ledger", str(ledger)]
    code = cli.main(argv)
    assert code == 0, code
    return code


def test_cli_build_auto_discovers_canonical_ledger(tmp_path: Path, monkeypatch, capsys) -> None:
    """Control for the priority test: without ``--ledger`` the canonical file *is* used."""
    config = _research_config(tmp_path)
    auto = _write_ledger(
        tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json",
        _ledger(text="自动发现主张-AUTO", url="https://auto.example/a"),
    )
    assert discover_research_ledger(config, theme=_THEME, business_date=_DATE) == auto.resolve()
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")

    _run_build(monkeypatch, config, delivery, ledger=None)

    result = json.loads(capsys.readouterr().out)
    claims = _pack_json(result["pack_path"], "claims.json")["claims"]
    assert [c["text"] for c in claims] == ["自动发现主张-AUTO"]


def test_cli_build_explicit_ledger_overrides_auto_discovery(tmp_path: Path, monkeypatch, capsys) -> None:
    config = _research_config(tmp_path)
    _write_ledger(
        tmp_path / "input" / "research-ledgers" / f"{_DATE}-{_THEME}.json",
        _ledger(text="自动发现主张-AUTO", url="https://auto.example/a"),
    )
    explicit = _write_ledger(
        tmp_path / "elsewhere" / "explicit.json",
        _ledger(text="显式指定主张-EXPLICIT", url="https://explicit.example/b", claim_id="c9"),
    )
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")

    _run_build(monkeypatch, config, delivery, ledger=explicit)

    result = json.loads(capsys.readouterr().out)
    claims = _pack_json(result["pack_path"], "claims.json")["claims"]
    texts = [c["text"] for c in claims]
    assert texts == ["显式指定主张-EXPLICIT"]
    assert "自动发现主张-AUTO" not in texts


def test_cli_build_missing_ledger_file_returns_error_code(tmp_path: Path, monkeypatch, capsys) -> None:
    config = _research_config(tmp_path)
    delivery = _build_fixture_delivery(tmp_path, business_date=_DATE, theme=_THEME, video_id="777")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)

    code = cli.main(
        [
            "episode-research-pack", "build",
            "--delivery-folder", str(delivery),
            "--ledger", str(tmp_path / "nope.json"),
        ]
    )

    assert code == 2
    assert "错误" in capsys.readouterr().err


def test_load_research_ledger_error_type_is_value_error() -> None:
    """Boundary contract the CLI relies on to translate a ledger failure to exit 2."""
    assert issubclass(ResearchLedgerError, ValueError)
