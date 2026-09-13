from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from douyin_intelligence.config import load_config
from douyin_intelligence.material_probe import (
    MaterialProbeError,
    RequestBudget,
    _commons_candidates,
    _balanced_two_lines,
    _decode_image,
    assert_public_dns,
    redact_url,
    run_material_probe,
    validate_https_url,
    validate_story,
)
from douyin_intelligence.workbench import material_probe_command


def _image_bytes(color: tuple[int, int, int], size: tuple[int, int] = (1200, 800)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG", quality=92)
    return output.getvalue()


def _resolver(_host: str, _port: int, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def _config(tmp_path: Path) -> tuple[dict, Path]:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state" / "jobs.json")
    config["jobs"]["lock_root"] = str(tmp_path / "state" / "locks")
    settings = config["jobs"]["material_probe"]
    settings["output_root"] = str(tmp_path / "output")
    settings["temp_root"] = str(tmp_path / "temp")
    story_path = tmp_path / "story.json"
    story_path.write_text(json.dumps({
        "story_id": "bill-gates-turbulent-ai-era",
        "target_date": "2026-08-28",
        "title_zh": "比尔·盖茨：现在对 AI 做出的选择至关重要",
        "summary_zh": "比尔·盖茨在个人官方网站讨论 AI 转型、社会准备与税收治理选项。",
        "confirmation_status": "official_primary_source",
        "official_sources": [{
            "publisher": "Gates Notes",
            "role": "作者官方文章",
            "url": "https://www.gatesnotes.com/story",
        }],
        "commons_query": "Bill Gates in 2024",
    }, ensure_ascii=False), encoding="utf-8")
    return config, story_path


def _transport(*, commons_license: bool = True, official_status: int = 200) -> httpx.MockTransport:
    official_image = _image_bytes((19, 91, 161))
    commons_image = _image_bytes((178, 76, 61), (900, 1200))

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "www.gatesnotes.com":
            return httpx.Response(
                official_status,
                headers={"content-type": "text/html"},
                content=b'<html><head><meta property="og:image" content="https://assets.gatesnotes.com/hero.jpg?signature=private&amp;w=1200"></head></html>',
            )
        if host == "assets.gatesnotes.com":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=official_image)
        if host == "commons.wikimedia.org" and path == "/w/api.php":
            metadata = {
                "Artist": {"value": "<a href='https://example.test'>European Commission</a>"},
                "LicenseShortName": {"value": "CC BY 4.0" if commons_license else "All rights reserved"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/" if commons_license else ""},
            }
            payload = {"query": {"pages": [{"index": 1, "imageinfo": [{
                "mime": "image/jpeg", "width": 900, "height": 1200, "size": len(commons_image),
                "thumbwidth": 900, "thumbheight": 1200, "thumbsize": len(commons_image),
                "thumburl": "https://upload.wikimedia.org/gates.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Bill_Gates_in_2024.jpg",
                "extmetadata": metadata,
            }]}]}}
            return httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"}, json=payload)
        if host == "upload.wikimedia.org":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=commons_image)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def test_story_gate_rejects_unconfirmed_http_and_private_sources() -> None:
    config = load_config()
    settings = config["jobs"]["material_probe"]
    base = {
        "story_id": "valid-story",
        "target_date": "2026-08-28",
        "title_zh": "一条已经确认的科技新闻",
        "summary_zh": "这是一条由官方一手来源支持的中性摘要内容。",
        "confirmation_status": "unverified",
        "official_sources": [{"url": "https://www.gatesnotes.com/story"}],
    }
    with pytest.raises(MaterialProbeError, match="官方一手来源"):
        validate_story(base, settings)
    base["confirmation_status"] = "official_primary_source"
    base["official_sources"] = [{"url": "http://www.gatesnotes.com/story"}]
    with pytest.raises(MaterialProbeError, match="HTTPS"):
        validate_story(base, settings)
    with pytest.raises(MaterialProbeError, match="允许域名"):
        validate_https_url("https://127.0.0.1/private", settings["allowed_domains"])


def test_dns_guard_only_allows_fake_ip_range_when_explicitly_enabled() -> None:
    def fake_ip(_host: str, _port: int, **_kwargs):
        return [
            (2, 1, 6, "", ("198.18.1.25", 443)),
            (23, 1, 6, "", ("fd7a:746f:6c69:6e65:66::189", 443, 0, 0)),
        ]
    with pytest.raises(MaterialProbeError, match="私网或保留地址"):
        assert_public_dns("www.gatesnotes.com", fake_ip)
    assert_public_dns(
        "www.gatesnotes.com", fake_ip,
        fake_ip_networks=("198.18.0.0/15", "fd7a:746f:6c69:6e65:66::/96"),
    )


def test_url_redaction_removes_signed_material_parameters() -> None:
    value = redact_url("https://assets.gatesnotes.com/a.jpg?w=1200&signature=secret&token=hidden#fragment")
    assert value == "https://assets.gatesnotes.com/a.jpg?w=1200"
    assert "secret" not in value and "token" not in value and "fragment" not in value


def test_image_validation_rejects_html_disguised_as_image_and_mime_mismatch() -> None:
    with pytest.raises(MaterialProbeError, match="有效图片"):
        _decode_image(b"<html>not an image</html>", "image/jpeg", 480)
    with pytest.raises(MaterialProbeError, match="MIME"):
        _decode_image(_image_bytes((1, 2, 3)), "image/png", 480)


def test_generated_card_wrapping_balances_title_and_keeps_punctuation_off_line_start() -> None:
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(Image.new("RGB", (1000, 500)))
    font = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 48)
    wrapped = _balanced_two_lines(draw, "比尔·盖茨：现在对 AI 做出的选择至关重要", font, 720)
    lines = wrapped.splitlines()
    assert len(lines) == 2 and min(len(line) for line in lines) >= 8
    assert all(line[:1] not in "，。；：、！？,.!?;:" for line in lines)
    assert "A\nI" not in wrapped


def test_commons_requires_supported_license_author_and_license_url() -> None:
    config = load_config()
    settings = config["jobs"]["material_probe"]
    base = {"query": {"pages": [{"index": 1, "imageinfo": [{
        "mime": "image/jpeg", "width": 900, "height": 1200, "size": 1000,
        "url": "https://upload.wikimedia.org/a.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:A.jpg",
        "extmetadata": {
            "Artist": {"value": "Author"},
            "LicenseShortName": {"value": "CC BY 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
        },
    }]}]}}
    assert len(_commons_candidates(base, settings)) == 1
    del base["query"]["pages"][0]["imageinfo"][0]["extmetadata"]["Artist"]
    assert _commons_candidates(base, settings) == []


def test_request_budget_hard_limits_requests_and_bytes(monkeypatch) -> None:
    budget = RequestBudget(max_requests=1, max_total_bytes=5, total_timeout_seconds=300, started_at=1.0)
    monkeypatch.setattr("douyin_intelligence.material_probe.time.monotonic", lambda: 2.0)
    budget.start_request()
    with pytest.raises(MaterialProbeError, match="请求上限"):
        budget.start_request()
    budget.add_bytes(5)
    with pytest.raises(MaterialProbeError, match="总下载上限"):
        budget.add_bytes(1)


def test_full_probe_builds_auditable_atomic_package_with_stable_ids(tmp_path: Path) -> None:
    config, story_path = _config(tmp_path)
    first_client = httpx.Client(transport=_transport())
    first = run_material_probe(config, story_path, client=first_client, resolver=_resolver)
    first_client.close()
    assert first["status"] == "success"
    assert first["budget"]["request_count"] == 4
    output = Path(first["output_dir"])
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"] == {
        "assets": 4, "max_assets": 5, "network_assets": 2, "renderable": 3,
        "review_required": 1, "reference_only": 0, "failures": 0,
        "llm_calls": 0, "ocr_calls": 0, "asr_calls": 0, "browser_calls": 0, "openmontage_writes": 0,
    }
    assert {item["rights_status"] for item in manifest["assets"]} == {"review_required", "renderable_with_attribution", "project_generated"}
    assert all(item["source_url"].startswith("https://") for item in manifest["assets"])
    commons = next(item for item in manifest["assets"] if item["source_kind"] == "wikimedia_commons")
    assert "署名" in commons["usage_requirements"]
    assert all((output / item["local_path"]).is_file() for item in manifest["assets"])
    assert "signature" not in json.dumps(manifest).casefold()
    assert not list(output.parent.glob(".*.stage-*"))
    first_ids = [item["asset_id"] for item in manifest["assets"]]

    second_client = httpx.Client(transport=_transport())
    second = run_material_probe(config, story_path, client=second_client, resolver=_resolver)
    second_client.close()
    second_manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
    assert [item["asset_id"] for item in second_manifest["assets"]] == first_ids
    assert "OpenMontage 写入：0" in (output / "preview.md").read_text(encoding="utf-8")


def test_unknown_commons_rights_degrades_without_putting_asset_in_renderable(tmp_path: Path) -> None:
    config, story_path = _config(tmp_path)
    client = httpx.Client(transport=_transport(commons_license=False))
    result = run_material_probe(config, story_path, client=client, resolver=_resolver)
    client.close()
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "partial"
    assert all(item["source_kind"] != "wikimedia_commons" for item in manifest["assets"])
    assert manifest["counts"]["assets"] == 3
    assert any(item["source"] == "commons_asset" for item in manifest["failures"])


def test_unreachable_official_source_stops_external_assets_and_writes_failed_report(tmp_path: Path) -> None:
    config, story_path = _config(tmp_path)
    client = httpx.Client(transport=_transport(official_status=503))
    result = run_material_probe(config, story_path, client=client, resolver=_resolver)
    client.close()
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert manifest["assets"] == []
    assert result["budget"]["request_count"] == 1
    assert manifest["confirmation"]["status"] == "official_source_unreachable"


def test_official_anti_automation_block_degrades_without_bypass(tmp_path: Path) -> None:
    config, story_path = _config(tmp_path)
    client = httpx.Client(transport=_transport(official_status=403))
    result = run_material_probe(config, story_path, client=client, resolver=_resolver)
    client.close()
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "partial"
    assert manifest["confirmation"]["status"] == "official_source_access_blocked"
    assert manifest["counts"]["assets"] == 3
    assert manifest["counts"]["network_assets"] == 1
    assert all(item["source_kind"] != "official_article_image" for item in manifest["assets"])
    assert result["budget"]["request_count"] == 3


def test_workbench_material_probe_command_has_no_browser_model_or_openmontage_path() -> None:
    command = material_probe_command("config/content_intelligence.json")
    assert command[-2:] == ["material-probe", "run"]
    for forbidden in ("--live", "browser-start", "ocr", "asr", "trusted-ai-brief", "OpenMontage"):
        assert forbidden not in command
