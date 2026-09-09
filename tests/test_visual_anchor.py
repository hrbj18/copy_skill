from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from douyin_intelligence.config import ConfigurationError, load_config
from douyin_intelligence.visual_anchor import (
    VisualAnchorError,
    build_visual_intent,
    check_visual_anchor_rss_health,
    _crop_dhashes,
    _download_douyin_cover,
    _select_diverse_candidates,
    _same_source_near_duplicate,
    extract_rss_image_candidates,
    extract_web_image_candidates,
    latest_visual_anchor_report,
    rank_candidates,
    run_domestic_visual_anchor_smoke,
    run_visual_anchor_batch,
    summarize_domestic_visual_anchor_outputs,
    validate_visual_story,
)
from douyin_intelligence.workbench import visual_anchor_command


def _resolver(_host: str, _port: int, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def _image(color: tuple[int, int, int], size: tuple[int, int] = (1200, 800)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "JPEG", quality=92)
    return stream.getvalue()


def _config(tmp_path: Path) -> tuple[dict, Path]:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state" / "jobs.json")
    config["jobs"]["lock_root"] = str(tmp_path / "state" / "locks")
    settings = config["jobs"]["visual_anchor"]
    settings["output_root"] = str(tmp_path / "output")
    settings["temp_root"] = str(tmp_path / "temp")
    settings["allowed_domains"] = ["official.test", "cdn.official.test", "rss.test"]
    story_path = tmp_path / "stories.json"
    story_path.write_text(json.dumps({"stories": [
        {
            "story_id": "robot-final-2026",
            "target_date": "2026-08-28",
            "title_zh": "Atlas 机器人参加 2026 机器人决赛",
            "summary_zh": "官方赛事页面确认 Atlas 机器人参加决赛并展示比赛现场。",
            "confirmation_status": "official_primary_source",
            "category": "robot_event",
            "visual_subject": "Atlas 机器人决赛",
            "subject_aliases": ["Atlas", "机器人决赛"],
            "official_sources": [{"publisher": "Official", "role": "赛事官方", "url": "https://official.test/robot-final"}],
        }
    ]}, ensure_ascii=False), encoding="utf-8")
    return config, story_path


def test_visual_intent_is_deterministic_bounded_and_does_not_add_facts() -> None:
    config = load_config()
    settings = config["jobs"]["visual_anchor"]
    story = validate_visual_story({
        "story_id": "game-release",
        "target_date": "2026-08-28",
        "title_zh": "星际远征正式发布",
        "summary_zh": "开发商官方页面宣布游戏发布。",
        "confirmation_status": "official_primary_source",
        "category": "game_release",
        "visual_subject": "星际远征",
        "subject_aliases": ["Star Voyage"],
        "official_sources": [{"publisher": "Studio", "role": "开发商公告", "url": "https://openai.com/example"}],
    }, {**settings, "allowed_domains": ["openai.com"]})
    first = build_visual_intent(story, settings)
    assert first == build_visual_intent(story, settings)
    assert first["visual_subject"] == "星际远征"
    assert len(first["query_terms"]) <= 3
    assert first["preferred_scene"] == "官方封面、实机或预告片画面"
    assert "销量" not in json.dumps(first, ensure_ascii=False)


def test_domestic_source_visual_controls_are_validated_and_bounded() -> None:
    settings = {**load_config()["jobs"]["visual_anchor"], "allowed_domains": ["official.test"]}
    story = validate_visual_story({
        "story_id": "domestic-visual-controls",
        "target_date": "2026-08-28",
        "title_zh": "国内新闻视觉控制测试",
        "summary_zh": "固定来源只允许两张经过核对的正文图片，并补充赛事视觉上下文。",
        "confirmation_status": "official_primary_source",
        "category": "robot_event",
        "visual_subject": "世界人形机器人运动会",
        "subject_aliases": ["人形机器人运动会"],
        "official_sources": [{
            "publisher": "Official", "role": "赛事页", "url": "https://official.test/event",
            "page_context_only_allowed": False, "visual_context": "机器人足球比赛", "max_images": 99,
        }],
    }, settings)
    source = story["official_sources"][0]
    assert source["page_context_only_allowed"] is False
    assert source["visual_context"] == "机器人足球比赛"
    assert source["max_images"] == 12


def test_rss_image_priority_namespace_relative_url_and_invalid_scheme() -> None:
    xml = b'''<rss xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>Atlas robot final</title><link>https://rss.test/a/story</link>
      <media:content url="/media/hero.jpg" type="image/jpeg" />
      <media:thumbnail url="https://rss.test/thumb.jpg" />
      <enclosure url="https://rss.test/enclosure.jpg" type="image/jpeg" />
      <description><![CDATA[<img src="https://rss.test/description.jpg">]]></description>
      <content:encoded><![CDATA[<img src="javascript:bad">]]></content:encoded>
    </item></channel></rss>'''
    rows = extract_rss_image_candidates("https://rss.test/feed.xml", xml, ["rss.test"])
    assert [row["extraction_kind"] for row in rows] == ["media_content", "media_thumbnail", "image_enclosure", "embedded_image"]
    assert rows[0]["image_url"] == "https://rss.test/media/hero.jpg"
    assert all(row["image_url"].startswith("https://") for row in rows)


def test_web_image_extraction_includes_jsonld_and_caps_body_images() -> None:
    document = b'''<html><head>
      <meta property="og:image" content="/og.jpg"><meta name="twitter:image" content="/tw.jpg">
      <script type="application/ld+json">{"image":["/schema.jpg"]}</script>
      </head><body><figure><img src="/one.jpg" alt="Atlas robot final"><figcaption>Competition</figcaption></figure>
      <img src="/two.jpg"><img src="/three.jpg"><img src="/four.jpg"></body></html>'''
    rows = extract_web_image_candidates("https://official.test/story", document, ["official.test"])
    assert [row["extraction_kind"] for row in rows[:3]] == ["og_image", "twitter_image", "jsonld_image"]
    assert len([row for row in rows if row["extraction_kind"] == "body_image"]) == 4
    assert len(rows) <= 12
    assert all(row["article_url"] == "https://official.test/story" for row in rows)


def test_domestic_html_extracts_largest_appstore_srcset_lazy_original_and_government_relative_image() -> None:
    document = b'''<html><head><title>Wangzhe Wanxiangqi</title></head><body>
      <picture><source type="image/webp" srcset="https://cdn.official.test/game-480.webp 480w, https://cdn.official.test/game-1286.webp 1286w"><source type="image/jpeg" srcset="https://cdn.official.test/game-1286.jpg 1286w"></picture>
      <img src="/tiny.png" data-original="https://cdn.official.test/person.png?token=secret" title="Tang Daosheng AI marathon">
      <img src="./W020260826.jpg" title="2026 World Robot Conference live" alt="2026 World Robot Conference live">
    </body></html>'''
    rows = extract_web_image_candidates("https://official.test/news/story.html", document, ["official.test", "cdn.official.test"])
    urls = [row["image_url"] for row in rows]
    assert "https://cdn.official.test/game-1286.webp" in urls
    assert "https://cdn.official.test/game-1286.jpg" not in urls
    person = next(row for row in rows if "/person.png" in row["image_url"])
    assert person["title"] == "Tang Daosheng AI marathon"
    assert "token=" not in person["image_url"]
    assert "https://official.test/news/W020260826.jpg" in urls


def test_page_context_can_be_disabled_and_tencent_generic_images_are_rejected() -> None:
    intent = {
        "visual_subject": "Xiaomi HyperOS 4 dual phone", "subject_aliases": ["Xiaomi HyperOS 4"],
        "negative_terms": ["cloud.png", "author avatar", "QR code", "4_hyperos-0807"], "visual_subscenes": [],
    }
    ranked = rank_candidates(intent, [
        {"candidate_id": "exact", "alt": "Xiaomi HyperOS 4", "image_url": "https://cdn.official.test/hyperos4.webp", "page_context": "Xiaomi HyperOS 4", "page_context_only_allowed": False, "source_kind": "official_article", "width": 1900, "height": 2007},
        {"candidate_id": "unrelated", "alt": "phone wallpaper", "image_url": "https://cdn.official.test/wallpaper.webp", "page_context": "Xiaomi HyperOS 4", "page_context_only_allowed": False, "source_kind": "official_article", "width": 1200, "height": 800},
        {"candidate_id": "cloud", "alt": "author avatar", "image_url": "https://cdn.official.test/cloud.png", "page_context": "Xiaomi HyperOS 4", "page_context_only_allowed": True, "source_kind": "official_article", "width": 1200, "height": 800},
        {"candidate_id": "logo-exact", "alt": "Xiaomi HyperOS 4", "image_url": "https://cdn.official.test/4_hyperos-0807.webp", "page_context": "Xiaomi HyperOS 4", "page_context_only_allowed": True, "source_kind": "official_article", "width": 1900, "height": 2073},
    ])
    by_id = {row["candidate_id"]: row for row in ranked}
    assert by_id["exact"]["relevance_status"] == "exact_subject"
    assert by_id["unrelated"]["relevance_status"] == "insufficient_match"
    assert by_id["cloud"]["relevance_status"] == "rejected_generic"
    assert by_id["logo-exact"]["relevance_status"] == "rejected_generic"


def test_final_selection_prefers_a_new_visual_subscene_before_same_scene_backup() -> None:
    qualified = [
        {"candidate_id": "sports", "article_url": "https://official.test/sports", "scene_id": "sports"},
        {"candidate_id": "conference-main", "article_url": "https://official.test/conference", "scene_id": "conference"},
        {"candidate_id": "conference-second", "article_url": "https://official.test/conference-2", "scene_id": "conference"},
    ]
    rejected: list[dict[str, str]] = []
    selected = _select_diverse_candidates(qualified, 2, rejected, preferred_primary_scene="conference")
    assert [row["candidate_id"] for row in selected] == ["conference-main", "sports"]


def test_exact_subject_beats_generic_ai_and_ties_are_stable() -> None:
    intent = {"visual_subject": "Atlas 机器人决赛", "subject_aliases": ["Atlas", "机器人决赛"], "negative_terms": ["AI大脑", "蓝色电路", "通用机器人"]}
    rows = [
        {"candidate_id": "generic", "title": "蓝色电路 AI 大脑", "alt": "通用机器人", "image_url": "https://official.test/generic.jpg", "source_kind": "article_body", "width": 1600, "height": 900},
        {"candidate_id": "exact-b", "title": "Atlas 机器人决赛现场", "alt": "Atlas competition", "image_url": "https://official.test/b.jpg", "source_kind": "official_article", "width": 1200, "height": 800},
        {"candidate_id": "exact-a", "title": "Atlas 机器人决赛现场", "alt": "Atlas competition", "image_url": "https://official.test/a.jpg", "source_kind": "official_article", "width": 1200, "height": 800},
    ]
    ranked = rank_candidates(intent, rows)
    assert [row["candidate_id"] for row in ranked[:2]] == ["exact-a", "exact-b"]
    assert ranked[-1]["relevance_status"] == "rejected_generic"


def test_official_page_context_cannot_turn_generic_logo_or_housing_image_into_exact_subject() -> None:
    intent = build_visual_intent({
        "visual_subject": "2026 FIRST Championship 机器人比赛", "subject_aliases": ["FIRST Championship"],
        "title_zh": "FIRST 公布比赛结果", "category": "robot_event", "query_terms": [], "region": "unknown",
    }, load_config()["jobs"]["visual_anchor"])
    ranked = rank_candidates(intent, [
        {"candidate_id": "logo", "image_url": "https://official.test/site-logo.png", "page_context": "2026 FIRST Championship 机器人比赛", "source_kind": "official_article", "width": 1200, "height": 800},
        {"candidate_id": "housing", "image_url": "https://official.test/housing-process.jpg", "page_context": "2026 FIRST Championship 机器人比赛", "source_kind": "official_article", "width": 1200, "height": 800},
        {"candidate_id": "event", "image_url": "https://official.test/first-championship-event.jpg", "page_context": "2026 FIRST Championship 机器人比赛", "source_kind": "official_article", "width": 1200, "height": 800},
    ])
    by_id = {row["candidate_id"]: row for row in ranked}
    assert by_id["logo"]["relevance_status"] == "rejected_generic"
    assert by_id["housing"]["relevance_status"] == "rejected_generic"
    assert by_id["event"]["relevance_status"] == "exact_subject"
    assert by_id["event"]["score_breakdown"]["page_context"] == 20


def test_product_version_requires_full_phrase_and_rejects_prefix_collisions() -> None:
    intent = {"visual_subject": "GPT-5", "subject_aliases": ["OpenAI GPT-5"], "negative_terms": []}
    ranked = rank_candidates(intent, [
        {"candidate_id": "gpt6", "title": "GPT-6 突发消息", "image_url": "https://cover.test/gpt6.jpg", "source_kind": "douyin_cover", "width": 1080, "height": 1440},
        {"candidate_id": "gpt56", "title": "GPT-5.6-Luna 本地体验", "image_url": "https://cover.test/gpt-5.6.jpg", "source_kind": "douyin_cover", "width": 1080, "height": 1440},
        {"candidate_id": "gpt5", "title": "OpenAI GPT-5 发布内容", "image_url": "https://cover.test/gpt-5.jpg", "source_kind": "douyin_cover", "width": 1080, "height": 1440},
    ])
    by_id = {row["candidate_id"]: row for row in ranked}
    assert by_id["gpt5"]["relevance_status"] == "exact_subject"
    assert by_id["gpt6"]["relevance_status"] == "insufficient_match"
    assert by_id["gpt56"]["relevance_status"] == "insufficient_match"


def test_batch_writes_primary_and_two_backups_with_rights_separated(tmp_path: Path) -> None:
    config, stories = _config(tmp_path)
    hero, second, third, generic = _image((20, 90, 160)), _image((170, 60, 50)), _image((40, 180, 90)), _image((90, 90, 90))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robot-final":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b'''<html><head><meta property="og:image" content="https://cdn.official.test/atlas-final.jpg"></head><body><img src="https://cdn.official.test/atlas-team.jpg" alt="Atlas robot final team"><img src="https://cdn.official.test/atlas-field.jpg" alt="Atlas robot final field"><img src="https://cdn.official.test/generic.jpg" alt="generic AI brain"></body></html>''')
        payload = {"/atlas-final.jpg": hero, "/atlas-team.jpg": second, "/atlas-field.jpg": third, "/generic.jpg": generic}[request.url.path]
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_visual_anchor_batch(config, stories, client=client, resolver=_resolver)
    client.close()
    assert result["status"] in {"success", "partial"}
    manifest = json.loads(Path(result["stories"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 3
    assert [item["role"] for item in manifest["assets"]] == ["primary", "backup_1", "backup_2"]
    assert manifest["optional_secondary_candidate"] == manifest["assets"][1]
    assert manifest["optional_tertiary_candidate"] == manifest["assets"][2]
    assert manifest["primary_candidate"]["rights_status"] == "review_required"
    assert manifest["primary_candidate"]["production_readiness"] == "manual_rights_review"
    assert all(item["rights_status"] != "project_generated" for item in manifest["assets"])
    assert all((Path(result["stories"][0]["output_dir"]) / item["local_path"]).is_file() for item in manifest["assets"])
    assert latest_visual_anchor_report(config).name == "preview.md"
    serialized = json.dumps(manifest, ensure_ascii=False).casefold()
    assert "signature=" not in serialized and "cookie" not in serialized and "bearer" not in serialized


def test_unrelated_image_is_not_used_to_fill_secondary(tmp_path: Path) -> None:
    config, stories = _config(tmp_path)
    exact, unrelated = _image((1, 30, 90)), _image((120, 120, 120))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robot-final":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b'<img src="https://cdn.official.test/exact.jpg" alt="Atlas robot final"><img src="https://cdn.official.test/office.jpg" alt="office portrait">')
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=exact if request.url.path == "/exact.jpg" else unrelated)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_visual_anchor_batch(config, stories, client=client, resolver=_resolver)
    client.close()
    manifest = json.loads(Path(result["stories"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 1
    assert manifest["optional_secondary_candidate"] is None
    assert any(row["reason"] == "relevance_below_threshold" for row in manifest["rejected_candidates"])


def test_same_source_cropped_ui_variant_is_not_used_as_secondary(tmp_path: Path) -> None:
    config, stories = _config(tmp_path)
    base = Image.new("RGB", (1200, 800), (18, 24, 31))
    for index in range(8):
        left = 40 + index * 130
        Image.Image.paste(base, Image.new("RGB", (90, 520), (30 + index * 20, 90, 170)), (left, 180))
    base_stream = io.BytesIO()
    base.save(base_stream, "JPEG", quality=95)
    titled = Image.new("RGB", (1200, 1000), (18, 24, 31))
    titled.paste(base, (0, 200))
    titled_stream = io.BytesIO()
    titled.save(titled_stream, "JPEG", quality=95)
    assert _same_source_near_duplicate(
        {"article_url": "https://official.test/story", "crop_hashes": _crop_dhashes(base_stream.getvalue()), "average_rgb": [70, 75, 80]},
        {"article_url": "https://official.test/story", "crop_hashes": _crop_dhashes(titled_stream.getvalue()), "average_rgb": [78, 82, 89]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robot-final":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b'<meta property="og:image" content="https://cdn.official.test/atlas-ui-titled.jpg"><img src="https://cdn.official.test/atlas-ui.jpg" alt="Atlas robot final interface">')
        data = titled_stream.getvalue() if request.url.path.endswith("titled.jpg") else base_stream.getvalue()
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=data)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_visual_anchor_batch(config, stories, client=client, resolver=_resolver)
    client.close()
    manifest = json.loads(Path(result["stories"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 1
    assert manifest["optional_secondary_candidate"] is None
    assert any(row["reason"] in {"duplicate_visual", "near_duplicate_visual"} for row in manifest["rejected_candidates"])


def test_exact_douyin_cover_is_reference_only_and_wrong_version_is_not_persisted(tmp_path: Path) -> None:
    config, stories = _config(tmp_path)
    settings = config["jobs"]["visual_anchor"]
    settings["rss_sources"] = []
    story = json.loads(stories.read_text(encoding="utf-8"))
    story["stories"][0].update({"visual_subject": "GPT-5", "subject_aliases": ["OpenAI GPT-5"], "title_zh": "OpenAI 发布 GPT-5"})
    stories.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")

    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>No images</html>")))
    exact, wrong = _image((15, 80, 150)), _image((180, 30, 20))

    def provider(_story: dict, _intent: dict, _limits: dict) -> dict:
        return {
            "status": "partial", "query_count": 1, "result_count": 2, "cover_candidates": 2,
            "cover_downloads": 2, "cover_request_count": 2, "cover_downloaded_bytes": len(exact) + len(wrong),
            "candidates": [
                {"_data": exact, "mime_type": "image/jpeg", "title": "OpenAI GPT-5 发布", "alt": "OpenAI GPT-5", "article_url": "https://www.douyin.com/video/123"},
                {"_data": wrong, "mime_type": "image/jpeg", "title": "GPT-6 爆料", "alt": "GPT-6", "article_url": "https://www.douyin.com/video/456"},
            ],
        }

    result = run_visual_anchor_batch(config, stories, client=client, resolver=_resolver, douyin_fallback=True, douyin_provider=provider)
    client.close()
    manifest = json.loads(Path(result["stories"][0]["manifest_path"]).read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 1
    assert manifest["primary_candidate"]["rights_status"] == "reference_only"
    assert manifest["primary_candidate"]["source_article_url"] == "https://www.douyin.com/video/123"
    assert manifest["primary_candidate"]["image_source_url"] == ""
    assert all("456" not in json.dumps(item, ensure_ascii=False) for item in manifest["assets"])


def test_story_and_batch_limits_are_enforced() -> None:
    config = load_config()
    settings = config["jobs"]["visual_anchor"]
    with pytest.raises(VisualAnchorError, match="最多处理 3 条"):
        run_visual_anchor_batch(config, {"stories": [{"story_id": str(index)} for index in range(4)]})
    assert settings["max_stories"] == 3
    assert settings["max_final_assets"] == 3
    assert settings["douyin"]["audio_policy"] == "never"
    assert settings["douyin"]["max_video_downloads"] == 1
    assert settings["douyin"]["max_frames"] == 8


def test_max_final_assets_above_three_is_rejected_by_config(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "config" / "content_intelligence.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["jobs"]["visual_anchor"]["max_final_assets"] = 4
    path = tmp_path / "invalid-config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="max_final_assets"):
        load_config(path)


def test_domestic_smoke_runs_strict_three_plus_one_and_writes_atomic_summary(tmp_path: Path) -> None:
    config = load_config()
    config["_project_root"] = str(tmp_path)
    config["jobs"]["state_path"] = str(tmp_path / "state" / "jobs.json")
    config["jobs"]["lock_root"] = str(tmp_path / "state" / "locks")
    settings = config["jobs"]["visual_anchor"]
    settings["allowed_domains"] = ["official.test", "cdn.official.test"]
    settings["rss_sources"] = []
    settings["domestic_smoke"] = {
        "batch_paths": ["config/batch-1.json", "config/batch-2.json"],
        "output_root": "output/domestic", "target_date": "2026-08-28",
    }
    rows = []
    images: dict[str, bytes] = {}
    for index in range(4):
        subject = f"Atlas Final {index}"
        rows.append({
            "story_id": f"atlas-final-{index}", "target_date": "2026-08-28",
            "title_zh": f"Atlas Final {index} official event", "summary_zh": f"Official page confirms {subject} with two exact visuals.",
            "confirmation_status": "official_primary_source", "category": "robot_event",
            "visual_subject": subject, "subject_aliases": [subject],
            "official_sources": [{"publisher": "Official", "role": "event", "url": f"https://official.test/story-{index}"}],
        })
        images[f"/story-{index}-a.jpg"] = _image((20 + index * 20, 60, 150))
        images[f"/story-{index}-b.jpg"] = _image((180, 120 + index * 20, 30))
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "batch-1.json").write_text(json.dumps({"stories": rows[:3]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "config" / "batch-2.json").write_text(json.dumps({"stories": rows[3:]}, ensure_ascii=False), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/story-") and not request.url.path.endswith(".jpg"):
            index = int(request.url.path.rsplit("-", 1)[-1])
            subject = f"Atlas Final {index}"
            body = f'<img src="https://cdn.official.test/story-{index}-a.jpg" alt="{subject}"><img src="https://cdn.official.test/story-{index}-b.jpg" alt="{subject} second scene">'.encode()
            return httpx.Response(200, headers={"content-type": "text/html"}, content=body)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=images[request.url.path])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_domestic_visual_anchor_smoke(config, client=client, resolver=_resolver)
    client.close()
    assert result["execution"]["batch_story_counts"] == [3, 1]
    assert result["counts"] == {"stories": 4, "success": 4, "partial": 0, "failed": 0, "needs_login": 0, "assets": 8}
    assert Path(result["output_path"]).is_file() and Path(result["json_path"]).is_file()
    assert all(Path(asset["absolute_local_path"]).is_file() for row in result["stories"] for asset in row["assets"])
    first_asset_id = result["stories"][0]["assets"][0]["asset_id"]
    qa_root = tmp_path / "output" / "domestic" / "2026-08-28"
    (qa_root / "visual-qa.json").write_text(json.dumps({"assets": {
        first_asset_id: {
            "content": "robot event", "correspondence": "exact", "duplicate_or_wrong": "no",
            "watermark_text": "none", "clarity": "clear", "orientation_use": "landscape",
        }
    }}), encoding="utf-8")
    rebuilt = summarize_domestic_visual_anchor_outputs(config)
    assert rebuilt["execution"]["summary_rebuilt_without_network"] is True
    assert rebuilt["counts"] == result["counts"]
    assert rebuilt["stories"][0]["assets"][0]["visual_qa"]["correspondence"] == "exact"
    assert "视觉QA" in Path(rebuilt["output_path"]).read_text(encoding="utf-8")


def test_douyin_fallback_is_bounded_needs_login_and_never_attempts_audio(tmp_path: Path) -> None:
    config, stories = _config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><body>No relevant image</body></html>")

    observed: dict = {}

    def provider(_story: dict, _intent: dict, limits: dict) -> dict:
        observed.update(limits)
        return {"status": "needs_login", "query_count": 99, "result_count": 99, "video_count": 99, "frame_count": 99, "audio_attempted": 99}

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_visual_anchor_batch(config, stories, client=client, resolver=_resolver, douyin_fallback=True, douyin_provider=provider)
    client.close()
    row = result["stories"][0]
    assert row["status"] == "needs_login"
    assert row["douyin"] == {
        "enabled": True, "status": "needs_login", "query_count": 3, "result_count": 8, "cover_candidates": 0, "cover_downloads": 0,
        "cover_request_count": 0, "cover_downloaded_bytes": 0, "cover_budget_exhausted": False,
        "video_count": 1, "frame_count": 8, "audio_attempted": 0,
    }
    assert observed["audio_policy"] == "never"


def test_workbench_visual_anchor_command_does_not_enable_douyin_by_default() -> None:
    command = visual_anchor_command("config/content_intelligence.json")
    assert command[-2:] == ["visual-anchor", "run"]
    assert "--douyin-fallback" not in command
    assert "OpenMontage" not in command


def test_default_douyin_clue_adapter_is_metadata_only_and_preserves_login_page(monkeypatch, tmp_path: Path) -> None:
    from douyin_intelligence.visual_anchor import _collect_douyin_clues

    observed: dict = {}

    def fake_collect(_config: dict, total_budget: int, **kwargs) -> dict:
        observed.update({"total_budget": total_budget, **kwargs})
        return {"status": "failed", "keywords": kwargs["keywords"], "sanitization": []}

    monkeypatch.setattr("douyin_intelligence.search_collector.collect_search", fake_collect)
    result = _collect_douyin_clues(
        load_config(), {"story_id": "safe-story"}, {"query_terms": ["a", "b", "c", "d"]},
        {"max_queries": 3, "max_results": 8, "max_cover_downloads": 2, "max_cover_requests": 4, "max_cover_bytes_each": 8_388_608, "max_cover_bytes_total": 12_582_912, "max_video_downloads": 1, "max_frames": 8, "audio_policy": "never"},
    )
    assert result == {"status": "needs_login", "query_count": 3, "result_count": 0, "cover_candidates": 0, "cover_downloads": 0, "cover_request_count": 0, "cover_downloaded_bytes": 0, "cover_budget_exhausted": False, "video_count": 0, "frame_count": 0, "audio_attempted": 0, "candidates": []}
    assert observed["total_budget"] == 8
    assert observed["hard_max"] == 8
    assert observed["keep_browser_on_failure"] is True


def test_rss_health_writes_reachable_feed_and_no_image_degradation(tmp_path: Path) -> None:
    config, _stories = _config(tmp_path)
    config["jobs"]["visual_anchor"]["rss_sources"] = [{"name": "RSS", "url": "https://rss.test/feed.xml", "enabled": True}]
    feed = b"<rss><channel><item><title>No image</title><link>https://rss.test/story</link></item></channel></rss>"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/xml"}, content=feed)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = check_visual_anchor_rss_health(config, client=client, resolver=_resolver)
    client.close()
    assert result["status"] == "success"
    assert result["sources"][0]["status"] == "reachable_no_images"
    assert result["sources"][0]["image_candidates"] == 0
    assert Path(result["output_path"]).is_file()


def test_cover_downloader_checks_redirect_hops_and_stops_at_total_byte_budget() -> None:
    config = load_config()
    image = _image((20, 30, 40))

    def redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private.jpg"})

    redirect_client = httpx.Client(transport=httpx.MockTransport(redirect_handler))
    budget = {"max_requests": 4, "request_count": 0, "max_total_bytes": 10_000_000, "downloaded_bytes": 0, "exhausted": False}
    assert _download_douyin_cover(config, "https://cover.test/a.jpg?signature=secret", maximum_bytes=8_388_608, budget=budget, client=redirect_client, resolver=_resolver) is None
    redirect_client.close()
    assert budget["request_count"] == 1

    image_client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image)))
    tiny = {"max_requests": 1, "request_count": 0, "max_total_bytes": len(image) - 1, "downloaded_bytes": 0, "exhausted": False}
    assert _download_douyin_cover(config, "https://cover.test/a.jpg", maximum_bytes=8_388_608, budget=tiny, client=image_client, resolver=_resolver) is None
    image_client.close()
    assert tiny["exhausted"] is True
    assert tiny["downloaded_bytes"] == tiny["max_total_bytes"]
