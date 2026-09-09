from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when the local project configuration is invalid."""


SEC_UID_RE = re.compile(r"^MS4wLjABAAAA[A-Za-z0-9_-]{20,}$")
TOPIC_RADAR_LANES = {"ai_general", "compute_infrastructure", "chips_hardware"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path, *, root: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root or project_root()) / path


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = resolve_path(path or "config/content_intelligence.json")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置文件不存在：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"配置文件不是有效 JSON：{config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("配置根节点必须是对象")
    if not str(payload.get("timezone") or "").strip():
        raise ConfigurationError("配置缺少 timezone")
    if not isinstance(payload.get("benchmark_accounts"), list):
        raise ConfigurationError("benchmark_accounts 必须是数组")
    if not isinstance(payload.get("categories"), dict):
        raise ConfigurationError("categories 必须是对象")
    for account in payload["benchmark_accounts"]:
        if not isinstance(account, dict):
            raise ConfigurationError("benchmark_accounts 每一项必须是对象")
        if not account.get("enabled", True):
            continue
        account_id = str(account.get("id") or "").strip()
        sec_uid = str(account.get("sec_uid") or "").strip()
        url = str(account.get("url") or "").strip()
        if not account_id:
            raise ConfigurationError("启用的 benchmark account 缺少 id")
        if not SEC_UID_RE.fullmatch(sec_uid):
            raise ConfigurationError(f"账号 {account_id} 缺少有效 sec_uid，不能用公开抖音号代替")
        if not url.endswith(f"/user/{sec_uid}"):
            raise ConfigurationError(f"账号 {account_id} 的 url 与 sec_uid 不一致")
    materials = payload.get("materials")
    if not isinstance(materials, dict):
        raise ConfigurationError("materials 必须是对象")
    if int(materials.get("top_n") or 0) <= 0:
        raise ConfigurationError("materials.top_n 必须大于 0")
    retention = materials.get("retention")
    if not isinstance(retention, dict) or int(retention.get("quota_bytes") or 0) <= 0:
        raise ConfigurationError("materials.retention.quota_bytes 必须大于 0")
    transcription = materials.get("transcription")
    if not isinstance(transcription, dict) or int(transcription.get("chunk_seconds") or 0) <= 0:
        raise ConfigurationError("materials.transcription.chunk_seconds 必须大于 0")
    llm = materials.get("llm")
    if not isinstance(llm, dict):
        raise ConfigurationError("materials.llm 必须是对象")
    base_url = str(llm.get("base_url") or "").strip().casefold()
    allow_insecure_http = bool(llm.get("allow_insecure_http", False))
    if base_url.startswith("http://") and not allow_insecure_http:
        raise ConfigurationError("materials.llm.base_url 使用 HTTP 时必须显式启用 allow_insecure_http")
    if llm.get("enabled", False) and base_url and not (
        base_url.startswith("https://") or base_url.startswith("http://") and allow_insecure_http
    ):
        raise ConfigurationError("启用的 materials.llm.base_url 只支持 HTTPS 或显式授权的 HTTP")
    jobs = payload.get("jobs")
    if not isinstance(jobs, dict):
        raise ConfigurationError("jobs 必须是对象")
    crawler = payload.get("media_crawler")
    if not isinstance(crawler, dict):
        raise ConfigurationError("media_crawler 必须是对象")
    if int(crawler.get("cdp_port") or 0) != 9223:
        raise ConfigurationError("项目专用浏览器必须固定使用 CDP 端口 9223")
    if int(crawler.get("max_project_pages") or 0) != 1:
        raise ConfigurationError("项目专用浏览器最多保留一个可见页面")
    if not all(bool(crawler.get(key)) for key in ("close_owned_browser_on_completion", "keep_open_on_needs_login", "reuse_persistent_profile")):
        raise ConfigurationError("项目浏览器持久登录与完成清理策略必须启用")
    if not 0 < float(crawler.get("cdp_timeout_seconds") or 0) <= 5 or not 0 < float(crawler.get("browser_lock_timeout_seconds") or 0) <= 20:
        raise ConfigurationError("项目浏览器 CDP 或生命周期锁超时超出安全范围")
    for key in ("browser_lease_root", "browser_state_path"):
        value = Path(str(crawler.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"media_crawler.{key} 必须是项目内相对路径")
    daily = jobs.get("daily_news")
    inspiration = jobs.get("inspiration")
    ranking = jobs.get("douyin_tech_ranking")
    trusted_job = jobs.get("trusted_account_news")
    account_pool = jobs.get("account_pool")
    visual_anchor = jobs.get("visual_anchor")
    material_probe = jobs.get("material_probe")
    daily_material_pack = jobs.get("daily_material_pack")
    daily_material_exchange = jobs.get("daily_material_exchange")
    daily_hot_candidate_pool = jobs.get("daily_hot_candidate_pool_v2")
    if not isinstance(daily, dict) or not isinstance(daily.get("sources"), list):
        raise ConfigurationError("jobs.daily_news.sources 必须是数组")
    if not isinstance(inspiration, dict):
        raise ConfigurationError("jobs.inspiration 必须是对象")
    if not isinstance(ranking, dict):
        raise ConfigurationError("jobs.douyin_tech_ranking 必须是对象")
    if not isinstance(trusted_job, dict):
        raise ConfigurationError("jobs.trusted_account_news 必须是对象")
    if not isinstance(account_pool, dict):
        raise ConfigurationError("jobs.account_pool 必须是对象")
    if not isinstance(visual_anchor, dict):
        raise ConfigurationError("jobs.visual_anchor 必须是对象")
    if not isinstance(material_probe, dict):
        raise ConfigurationError("jobs.material_probe 必须是对象")
    if not isinstance(daily_material_pack, dict):
        raise ConfigurationError("jobs.daily_material_pack 必须是对象")
    if not isinstance(daily_material_exchange, dict):
        raise ConfigurationError("jobs.daily_material_exchange 必须是对象")
    if not isinstance(daily_hot_candidate_pool, dict):
        raise ConfigurationError("jobs.daily_hot_candidate_pool_v2 必须是对象")
    story_enrichment = daily_hot_candidate_pool.get("story_enrichment")
    if not isinstance(story_enrichment, dict) or story_enrichment.get("enabled") is not True:
        raise ConfigurationError("V2 事件正文补全必须显式启用")
    frozen_enrichment_limits = {
        "max_detailed_stories": (1, 40),
        "max_selected_videos": (1, 40),
        "max_supporting_videos": (0, 20),
        "max_evidence_videos": (1, 3),
        "max_media_videos": (0, 20),
        "media_total_seconds": (1, 900),
        "per_media_timeout_seconds": (1, 60),
        "max_asr_videos": (0, 0),
        "asr_total_seconds": (1, 180),
        "per_asr_timeout_seconds": (1, 90),
        "llm_max_batches": (0, 5),
        "llm_batch_size": (1, 10),
    }
    for key, (minimum, maximum) in frozen_enrichment_limits.items():
        value = int(story_enrichment.get(key) if story_enrichment.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"V2 事件正文补全预算 {key} 超出任务合同")
    consumer_hot_list = daily_hot_candidate_pool.get("consumer_hot_list")
    if not isinstance(consumer_hot_list, dict) or consumer_hot_list.get("enabled") is not True:
        raise ConfigurationError("V2 前二十消费端热榜必须显式启用")
    if int(consumer_hot_list.get("target_count") or 0) != 20:
        raise ConfigurationError("V2 前二十消费端热榜数量必须固定为 20")
    if not 1 <= int(consumer_hot_list.get("llm_batch_size") or 0) <= 10:
        raise ConfigurationError("V2 前二十消费端热榜模型批大小超出任务合同")
    if not 256 <= int(consumer_hot_list.get("max_output_tokens") or 0) <= 3200:
        raise ConfigurationError("V2 前二十消费端热榜输出 token 超出任务合同")
    public_details = daily_hot_candidate_pool.get("public_detail_discovery")
    if not isinstance(public_details, dict) or public_details.get("enabled") is not True:
        raise ConfigurationError("V2 前二十公开详情补全必须显式启用")
    if public_details.get("provider") != "google_news_rss":
        raise ConfigurationError("V2 公开详情补全来源必须为 google_news_rss")
    public_limits = {"max_queries": (1, 40), "max_results_per_query": (1, 3), "request_timeout_seconds": (1, 15), "max_response_bytes": (1024, 262144), "max_total_bytes": (1024, 2097152), "min_detail_chars": (20, 400), "freshness_days": (1, 7), "freshness_grace_hours": (0, 24)}
    for key, (minimum, maximum) in public_limits.items():
        value = int(public_details.get(key) if public_details.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"V2 前二十公开详情补全预算 {key} 超出任务合同")
    if int(public_details["max_response_bytes"]) > int(public_details["max_total_bytes"]):
        raise ConfigurationError("V2 前二十公开详情单次响应上限不能超过总下载上限")
    official_discovery = daily_hot_candidate_pool.get("official_discovery")
    if not isinstance(official_discovery, dict) or official_discovery.get("enabled") is not True:
        raise ConfigurationError("V2 官方重大事件发现必须显式启用")
    official_limits = {
        "max_sources": (1, 14), "max_requests": (1, 14), "max_total_bytes": (1, 10_485_760),
        "max_page_bytes": (1, 1_048_576), "total_timeout_seconds": (1, 300),
        "request_timeout_seconds": (1, 20), "max_redirects": (0, 2), "max_events": (20, 60),
        "douyin_signal_max_events": (0, 20), "douyin_signal_per_event_limit": (1, 10),
    }
    for key, (minimum, maximum) in official_limits.items():
        value = int(official_discovery.get(key) if official_discovery.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"V2 官方重大事件发现预算 {key} 超出任务合同")
    if int(official_discovery["max_page_bytes"]) > int(official_discovery["max_total_bytes"]):
        raise ConfigurationError("官方重大事件单页上限不能超过总下载上限")
    article_detail_limits = {
        "article_detail_max_events": (1, 24), "article_detail_max_requests": (1, 48),
        "article_detail_max_total_bytes": (1024, 12_582_912), "article_detail_max_page_bytes": (1024, 524_288),
        "article_detail_total_timeout_seconds": (1, 300), "article_detail_timeout_seconds": (1, 20),
    }
    for key, (minimum, maximum) in article_detail_limits.items():
        value = int(official_discovery.get(key) if official_discovery.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"V2 多源新闻详情预算 {key} 超出任务合同")
    if int(official_discovery["article_detail_max_page_bytes"]) > int(official_discovery["article_detail_max_total_bytes"]):
        raise ConfigurationError("多源新闻详情单页上限不能超过总下载上限")
    article_domains = official_discovery.get("article_detail_allowed_domains")
    if not isinstance(article_domains, list) or not 1 <= len(article_domains) <= 32:
        raise ConfigurationError("V2 多源新闻详情允许域名必须是 1 到 32 个配置项")
    for domain in article_domains:
        value = str(domain or "").strip().casefold().rstrip(".")
        if not value or any(token in value for token in (":", "/", "@")) or value == "localhost":
            raise ConfigurationError("V2 多源新闻详情允许域名无效")
    company_priority = official_discovery.get("company_event_priority")
    if not isinstance(company_priority, dict) or company_priority.get("enabled") is not True:
        raise ConfigurationError("V2 大厂重大动作优先级必须显式启用")
    routine_terms = company_priority.get("routine_exclusion_terms")
    if not isinstance(routine_terms, list) or not 4 <= len(routine_terms) <= 24 or any(len(str(item).strip()) < 2 for item in routine_terms):
        raise ConfigurationError("V2 大厂普通消费品排除词无效")
    context_terms = company_priority.get("context_exclusion_terms")
    if not isinstance(context_terms, list) or not 4 <= len(context_terms) <= 24 or any(len(str(item).strip()) < 2 for item in context_terms):
        raise ConfigurationError("V2 大厂第三方评论排除词无效")
    company_tiers = company_priority.get("tiers")
    allowed_company_categories = {"ai_security", "model_release", "generative_media", "ai_infrastructure", "embodied_ai", "autonomous_mobility", "consumer_product"}
    if not isinstance(company_tiers, list) or len(company_tiers) != 2:
        raise ConfigurationError("V2 大厂优先级必须有两层配置")
    tier_ids: set[str] = set()
    for tier in company_tiers:
        if not isinstance(tier, dict):
            raise ConfigurationError("V2 大厂优先级层必须是对象")
        tier_id = str(tier.get("id") or "").strip()
        if not tier_id or tier_id in tier_ids:
            raise ConfigurationError("V2 大厂优先级层名称缺失或重复")
        tier_ids.add(tier_id)
        if not 1 <= int(tier.get("boost") or 0) <= 20:
            raise ConfigurationError("V2 大厂优先级加分超出范围")
        aliases = tier.get("aliases")
        if not isinstance(aliases, list) or not 1 <= len(aliases) <= 40 or any(len(str(item).strip()) < 2 for item in aliases):
            raise ConfigurationError("V2 大厂优先级别名无效")
        categories = tier.get("eligible_categories")
        if not isinstance(categories, list) or not categories or not set(map(str, categories)) <= allowed_company_categories:
            raise ConfigurationError("V2 大厂优先级事件类型无效")
        required_terms = tier.get("required_terms")
        if required_terms is not None and (not isinstance(required_terms, list) or not required_terms or any(len(str(item).strip()) < 2 for item in required_terms)):
            raise ConfigurationError("V2 大厂优先级必要技术锚点无效")
        action_terms = tier.get("action_terms")
        if not isinstance(action_terms, list) or not action_terms or any(len(str(item).strip()) < 2 for item in action_terms):
            raise ConfigurationError("V2 大厂优先级重大动作词无效")
    brief_localization = official_discovery.get("brief_localization")
    if not isinstance(brief_localization, dict) or brief_localization.get("enabled") is not True:
        raise ConfigurationError("V2 官方重大事件中文卡片整理必须显式启用")
    if not 20 <= int(brief_localization.get("max_events") or 0) <= 30:
        raise ConfigurationError("V2 多源科技新闻中文卡片数量必须在 20 到 30")
    if not 256 <= int(brief_localization.get("max_output_tokens") or 0) <= 4800:
        raise ConfigurationError("V2 官方重大事件中文卡片输出 token 超出任务合同")
    reader_editorial = official_discovery.get("reader_editorial")
    if not isinstance(reader_editorial, dict) or reader_editorial.get("enabled") is not True:
        raise ConfigurationError("V2 公众主榜事件与白话写作规则必须显式启用")
    reader_lists = {
        "preview_terms": (4, 16),
        "research_terms": (4, 16),
        "platform_terms": (4, 16),
        "consumer_impact_terms": (4, 16),
        "industry_only_terms": (4, 16),
        "mainstream_categories": (4, 12),
    }
    allowed_reader_categories = {"ai_security", "model_release", "generative_media", "policy_governance", "ai_infrastructure", "embodied_ai", "autonomous_mobility", "consumer_product", "technology_update"}
    for key, (minimum, maximum) in reader_lists.items():
        values = reader_editorial.get(key)
        if not isinstance(values, list) or not minimum <= len(values) <= maximum or any(not str(item).strip() for item in values):
            raise ConfigurationError(f"V2 公众主榜规则 {key} 无效")
    if not set(map(str, reader_editorial["mainstream_categories"])) <= allowed_reader_categories:
        raise ConfigurationError("V2 公众主榜事件类型无效")
    if set(official_discovery.get("fake_ip_networks") or []) != {"198.18.0.0/15", "fd7a:746f:6c69:6e65:66::/96"}:
        raise ConfigurationError("官方重大事件发现 Fake-IP DNS 兼容网段与受控代理合同不一致")
    official_sources = official_discovery.get("sources")
    if not isinstance(official_sources, list) or not 1 <= len(official_sources) <= 14:
        raise ConfigurationError("V2 多源科技新闻来源必须是 1 到 14 个配置项")
    source_names: set[str] = set()
    for source in official_sources:
        if not isinstance(source, dict):
            raise ConfigurationError("V2 官方重大事件来源必须是对象")
        name = str(source.get("name") or "").strip()
        if not name or name in source_names:
            raise ConfigurationError("V2 官方重大事件来源名称缺失或重复")
        source_names.add(name)
        url = str(source.get("url") or "").strip()
        if not url.startswith("https://") or "@" in url:
            raise ConfigurationError("V2 官方重大事件来源入口必须是无认证 HTTPS URL")
        if str(source.get("kind") or "") not in {"official", "primary", "authority", "government", "news_index", "media"}:
            raise ConfigurationError("V2 多源科技新闻来源可信层级无效")
        if str(source.get("format") or "") not in {"feed", "html_listing", "google_news_rss"}:
            raise ConfigurationError("V2 多源科技新闻来源格式无效")
        domains = source.get("allowed_domains")
        if not isinstance(domains, list) or not domains:
            raise ConfigurationError("V2 官方重大事件来源缺少允许域名")
        for domain in domains:
            value = str(domain or "").strip().casefold().rstrip(".")
            if not value or any(token in value for token in (":", "/", "@")) or value == "localhost":
                raise ConfigurationError("V2 官方重大事件来源允许域名无效")
        prefixes = source.get("article_path_prefixes")
        if prefixes is not None and (not isinstance(prefixes, list) or any(not str(item).startswith("/") for item in prefixes)):
            raise ConfigurationError("V2 官方重大事件文章路径前缀无效")
    public_web_discovery = daily_hot_candidate_pool.get("public_web_discovery")
    if not isinstance(public_web_discovery, dict) or public_web_discovery.get("enabled") is not True:
        raise ConfigurationError("V2 公开网络探索池必须显式启用")
    if str(public_web_discovery.get("provider") or "") != "google_news_rss":
        raise ConfigurationError("V2 公开网络探索池目前只支持 google_news_rss")
    if str(public_web_discovery.get("endpoint") or "") != "https://news.google.com/rss/search":
        raise ConfigurationError("V2 公开网络探索池入口必须是受控 Google News RSS")
    web_limits = {
        "max_queries": (8, 16), "max_results_per_query": (1, 8), "request_timeout_seconds": (1, 20),
        "max_response_bytes": (1024, 262144), "max_total_bytes": (1024, 4_194_304),
        "total_timeout_seconds": (1, 300), "target_count": (20, 20),
    }
    for key, (minimum, maximum) in web_limits.items():
        value = int(public_web_discovery.get(key) if public_web_discovery.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"V2 公开网络探索池预算 {key} 超出任务合同")
    if int(public_web_discovery["max_response_bytes"]) > int(public_web_discovery["max_total_bytes"]):
        raise ConfigurationError("V2 公开网络探索池单次响应上限不能超过总下载上限")
    if set(public_web_discovery.get("fake_ip_networks") or []) != {"198.18.0.0/15", "fd7a:746f:6c69:6e65:66::/96"}:
        raise ConfigurationError("V2 公开网络探索池 Fake-IP DNS 兼容网段与受控代理合同不一致")
    public_queries = public_web_discovery.get("queries")
    if not isinstance(public_queries, list) or not 8 <= len(public_queries) <= 16:
        raise ConfigurationError("V2 公开网络探索池查询矩阵必须有 8 到 16 项")
    query_ids: set[str] = set()
    for query in public_queries:
        if not isinstance(query, dict):
            raise ConfigurationError("V2 公开网络探索池查询必须是对象")
        query_id = str(query.get("id") or "").strip()
        text = str(query.get("text") or "").strip()
        if not query_id or query_id in query_ids or not re.fullmatch(r"[a-z0-9_]{3,80}", query_id):
            raise ConfigurationError("V2 公开网络探索池查询 ID 无效或重复")
        if not 6 <= len(text) <= 180 or any(token in text.casefold() for token in ("游戏", "steam", "switch", "xbox", "ps5")):
            raise ConfigurationError("V2 公开网络探索池查询文本无效或包含专用游戏词")
        query_ids.add(query_id)
    if int(daily_hot_candidate_pool.get("max_wall_seconds") or 0) != 3600:
        raise ConfigurationError("V2 每日候选池总墙钟必须固定为一小时")
    priority = daily_hot_candidate_pool.get("editorial_priority")
    if not isinstance(priority, dict) or priority.get("enabled") is not True:
        raise ConfigurationError("V2 科技价值与亲民度推荐排序必须显式启用")
    priority_weights = priority.get("weights")
    required_priority_weights = {"heat", "strategic_significance", "public_relevance", "discussion_value"}
    if not isinstance(priority_weights, dict) or set(priority_weights) != required_priority_weights:
        raise ConfigurationError("V2 推荐排序权重字段不完整")
    if any(not 0 <= float(priority_weights[key]) <= 1 for key in required_priority_weights) or abs(sum(float(priority_weights[key]) for key in required_priority_weights) - 1.0) > 1e-9:
        raise ConfigurationError("V2 推荐排序权重必须位于 0 到 1 且总和为 1")
    search_words = [str(word).strip().casefold() for key in ("core_keywords", "supplemental_keywords") for word in daily_hot_candidate_pool.get(key) or []]
    if any("游戏" in word or word in {"steam", "switch", "xbox", "ps5"} for word in search_words):
        raise ConfigurationError("V2 每日科技候选池不得配置专用游戏搜索词")
    if not 1 <= int(ranking.get("max_metadata_records") or 0) <= 10:
        raise ConfigurationError("抖音科技热榜元数据上限必须在 1 到 10 之间")
    if not isinstance(ranking.get("keywords"), list) or not ranking["keywords"]:
        raise ConfigurationError("jobs.douyin_tech_ranking.keywords 必须是非空数组")
    if not isinstance(ranking.get("weights"), dict):
        raise ConfigurationError("jobs.douyin_tech_ranking.weights 必须是对象")
    if not 1 <= int(trusted_job.get("max_items") or 0) <= int(trusted_job.get("hard_max_items") or 0) <= 10:
        raise ConfigurationError("可信账号快讯作品上限必须在 1 到 10 之间")
    if int(trusted_job.get("window_hours") or 0) != 48:
        raise ConfigurationError("可信账号快讯窗口必须固定为 48 小时")
    if int(trusted_job.get("min_asr_chars") or 0) < 20:
        raise ConfigurationError("可信账号快讯本地转写最低有效字数不能小于 20")
    visual = trusted_job.get("visual_ocr")
    if not isinstance(visual, dict) or visual.get("frame_strategy") != "scene_plus_interval_guard":
        raise ConfigurationError("可信账号视觉 OCR 必须使用 scene_plus_interval_guard")
    if not 0.05 <= float(visual.get("scene_threshold") or 0) <= 0.5:
        raise ConfigurationError("视觉 OCR 场景阈值必须在 0.05 到 0.5 之间")
    if not 0.5 <= float(visual.get("interval_seconds") or 0) <= 5:
        raise ConfigurationError("视觉 OCR 间隔必须在 0.5 到 5 秒之间")
    if int(visual.get("first_pass_width") or 0) != 960 or int(visual.get("retry_width") or 0) != 1280:
        raise ConfigurationError("视觉 OCR 首轮/重试宽度必须为 960/1280")
    if not 1 <= int(visual.get("retry_max_frames") or 0) <= 10:
        raise ConfigurationError("视觉 OCR 单视频重试帧数必须在 1 到 10 之间")
    if int(visual.get("per_video_timeout_seconds") or 0) > 60 or int(visual.get("batch_timeout_seconds") or 0) > 480:
        raise ConfigurationError("视觉 OCR 时间预算超过安全上限")
    if int(visual.get("batch_soft_frame_limit") or 0) > 300 or int(visual.get("batch_hard_frame_limit") or 0) > 400:
        raise ConfigurationError("视觉 OCR 批次帧预算超过安全上限")
    expected_budgets = [(15, 20, 30), (30, 30, 45), (60, 40, 60), (120, 60, 80), (None, 80, 100)]
    actual_budgets = [
        (row.get("max_duration_seconds"), int(row.get("first_pass") or 0), int(row.get("hard") or 0))
        for row in visual.get("frame_budgets") or [] if isinstance(row, dict)
    ]
    if actual_budgets != expected_budgets:
        raise ConfigurationError("视觉 OCR 分段帧预算与任务合同不一致")
    for key in ("output_root", "temp_root"):
        value = Path(str(trusted_job.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.trusted_account_news.{key} 必须是项目内相对路径")
    if int(account_pool.get("window_days") or 0) != 14:
        raise ConfigurationError("候选账号体检窗口必须固定为14个自然日")
    if not 1 <= int(account_pool.get("max_posts_per_account") or 0) <= 30:
        raise ConfigurationError("候选账号体检每账号作品上限必须在1到30之间")
    if int(account_pool.get("concurrency") or 0) != 1:
        raise ConfigurationError("候选账号体检并发必须为1")
    visual_sampling = account_pool.get("visual_sampling")
    if not isinstance(visual_sampling, dict) or int(visual_sampling.get("global_max_samples") or 0) > 3:
        raise ConfigurationError("候选账号视觉抽样全局上限不能超过3")
    if visual_sampling.get("audio_policy") != "never":
        raise ConfigurationError("候选账号视觉抽样必须禁用音频")
    thresholds = account_pool.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ConfigurationError("jobs.account_pool.thresholds 必须是对象")
    expected_thresholds = {
        "core_min_active_days": (1, 14),
        "supplemental_min_active_days": (1, 14),
        "min_technology_news_ratio": (0, 1),
        "min_short_video_ratio": (0, 1),
        "max_promotion_ratio": (0, 1),
        "min_duration_coverage": (0, 1),
    }
    for key, (minimum, maximum) in expected_thresholds.items():
        value = float(thresholds.get(key, -1))
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"候选账号阈值 {key} 超出允许范围")
    for key in ("state_path", "output_root", "temp_root"):
        value = Path(str(account_pool.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.account_pool.{key} 必须是项目内相对路径")
    initial_candidates = account_pool.get("initial_candidates")
    if not isinstance(initial_candidates, list):
        raise ConfigurationError("jobs.account_pool.initial_candidates 必须是数组")
    seen_candidate_ids: set[str] = set()
    for candidate in initial_candidates:
        if not isinstance(candidate, dict) or not str(candidate.get("display_name") or "").strip():
            raise ConfigurationError("候选账号缺少显示名称")
        profile = str(candidate.get("profile_url") or "").strip().rstrip("/")
        prefix = "https://www.douyin.com/user/"
        stable_id = profile[len(prefix):] if profile.startswith(prefix) else ""
        if not SEC_UID_RE.fullmatch(stable_id):
            raise ConfigurationError("候选账号必须使用规范的HTTPS抖音用户主页")
        if stable_id in seen_candidate_ids:
            raise ConfigurationError(f"候选账号重复：{stable_id}")
        seen_candidate_ids.add(stable_id)
        role = str(candidate.get("production_role") or "")
        if role:
            if role != "topic_radar" or candidate.get("lifecycle_status") != "candidate" or candidate.get("discovery_enabled") is not True or candidate.get("enabled") is not True:
                raise ConfigurationError("topic_radar 候选必须保持 enabled candidate 并显式启用发现")
            if not re.fullmatch(r"\d{5,32}", str(candidate.get("id") or "")):
                raise ConfigurationError("topic_radar 候选缺少稳定抖音号")
            if not str(candidate.get("source_group_id") or "").strip() or not str(candidate.get("source_group_name") or "").strip():
                raise ConfigurationError("topic_radar 候选缺少 source_group")
            if str(candidate.get("editorial_lane") or "") not in TOPIC_RADAR_LANES:
                raise ConfigurationError("topic_radar 候选赛道无效")
        elif candidate.get("discovery_enabled"):
            raise ConfigurationError("普通 candidate 不得启用生产发现")
    expected_anchor_limits = {
        "max_stories": (1, 3),
        "max_candidates": (1, 12),
        "max_final_assets": (1, 3),
        "minimum_relevance_score": (1, 100),
        "total_timeout_seconds_per_story": (1, 300),
        "request_timeout_seconds": (1, 15),
        "max_requests_per_story": (1, 10),
        "max_total_bytes_per_story": (1, 52_428_800),
        "max_asset_bytes": (1, 15_728_640),
        "max_html_bytes": (1, 2_097_152),
        "min_dimension": (1, 480),
        "max_redirects": (0, 3),
    }
    for key, (minimum, maximum) in expected_anchor_limits.items():
        value = int(visual_anchor.get(key) if visual_anchor.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"新闻视觉锚点预算 {key} 超出任务合同")
    if int(visual_anchor["max_asset_bytes"]) > int(visual_anchor["max_total_bytes_per_story"]):
        raise ConfigurationError("新闻视觉锚点单素材上限不能超过单新闻总下载上限")
    for key in ("stories_path", "output_root", "temp_root"):
        value = Path(str(visual_anchor.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.visual_anchor.{key} 必须是项目内相对路径")
    allowed_anchor_domains = visual_anchor.get("allowed_domains")
    if not isinstance(allowed_anchor_domains, list) or not allowed_anchor_domains:
        raise ConfigurationError("jobs.visual_anchor.allowed_domains 必须是非空数组")
    douyin_anchor = visual_anchor.get("douyin")
    if not isinstance(douyin_anchor, dict) or douyin_anchor.get("audio_policy") != "never" or int(douyin_anchor.get("concurrency") or 0) != 1:
        raise ConfigurationError("新闻视觉锚点抖音降级必须单并发且禁用音频")
    if int(douyin_anchor.get("max_queries") or 0) > 3 or int(douyin_anchor.get("max_results") or 0) > 8 or int(douyin_anchor.get("max_video_downloads") or 0) > 1 or int(douyin_anchor.get("max_frames") or 0) > 8:
        raise ConfigurationError("新闻视觉锚点抖音预算超过任务合同")
    if not 1 <= int(douyin_anchor.get("max_cover_downloads") or 0) <= 2 or not 1 <= int(douyin_anchor.get("max_cover_requests") or 0) <= 4:
        raise ConfigurationError("新闻视觉锚点封面下载数量或请求预算超限")
    if not 1 <= int(douyin_anchor.get("max_cover_bytes_each") or 0) <= 8_388_608 or not 1 <= int(douyin_anchor.get("max_cover_bytes_total") or 0) <= 12_582_912:
        raise ConfigurationError("新闻视觉锚点封面字节预算超限")
    if int(douyin_anchor["max_cover_bytes_each"]) > int(douyin_anchor["max_cover_bytes_total"]):
        raise ConfigurationError("单封面字节上限不能超过封面总字节上限")
    domestic_smoke = visual_anchor.get("domestic_smoke")
    if not isinstance(domestic_smoke, dict) or not isinstance(domestic_smoke.get("batch_paths"), list) or len(domestic_smoke["batch_paths"]) != 2:
        raise ConfigurationError("国内视觉验收必须配置两个3+1批次")
    for value in [*domestic_smoke["batch_paths"], domestic_smoke.get("output_root")]:
        path = Path(str(value or ""))
        if not str(path) or path.is_absolute() or ".." in path.parts:
            raise ConfigurationError("国内视觉验收路径必须是项目内相对路径")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(domestic_smoke.get("target_date") or "")):
        raise ConfigurationError("国内视觉验收 target_date 必须是 YYYY-MM-DD")
    expected_pack_limits = {
        "max_stories": (1, 4),
        "max_assets_per_story": (1, 2),
        "max_wall_seconds": (1, 300),
        "max_network_requests": (0, 40),
        "max_download_bytes": (1, 209_715_200),
    }
    for key, (minimum, maximum) in expected_pack_limits.items():
        value = int(daily_material_pack.get(key) if daily_material_pack.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"每日科技素材供应包预算 {key} 超出任务合同")
    if daily_material_pack.get("quick_by_default") is not True:
        raise ConfigurationError("每日科技素材供应包默认必须启用快速模式")
    for key in ("selection_input", "output_root", "temp_root"):
        value = Path(str(daily_material_pack.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.daily_material_pack.{key} 必须是项目内相对路径")
    expected_exchange_limits = {
        "max_wall_seconds": (1, 3600),
        "max_network_requests": (1, 24),
        "max_download_bytes": (1, 52_428_800),
        "max_html_bytes": (1, 2_097_152),
        "max_asset_bytes": (1, 15_728_640),
        "min_dimension": (1, 480),
        "request_timeout_seconds": (1, 15),
        "max_redirects": (0, 3),
    }
    for key, (minimum, maximum) in expected_exchange_limits.items():
        value = int(daily_material_exchange.get(key) if daily_material_exchange.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"每日02:00素材交换区预算 {key} 超出任务合同")
    if int(daily_material_exchange["max_asset_bytes"]) > int(daily_material_exchange["max_download_bytes"]):
        raise ConfigurationError("每日02:00素材交换区单图片上限不能超过总下载上限")
    for key in ("selection_input", "output_root"):
        value = Path(str(daily_material_exchange.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.daily_material_exchange.{key} 必须是项目内相对路径")
    exchange_domains = daily_material_exchange.get("allowed_domains")
    if not isinstance(exchange_domains, list) or not exchange_domains:
        raise ConfigurationError("jobs.daily_material_exchange.allowed_domains 必须是非空数组")
    for domain in exchange_domains:
        value = str(domain or "").strip().casefold().rstrip(".")
        if not value or ":" in value or "/" in value or value == "localhost":
            raise ConfigurationError("每日02:00素材交换区允许域名无效")
    if set(daily_material_exchange.get("fake_ip_networks") or []) != {"198.18.0.0/15", "fd7a:746f:6c69:6e65:66::/96"}:
        raise ConfigurationError("每日02:00素材交换区 Fake-IP DNS 兼容网段与受控代理合同不一致")
    expected_probe_limits = {
        "total_timeout_seconds": (1, 300),
        "request_timeout_seconds": (1, 15),
        "max_requests": (1, 10),
        "max_assets": (1, 5),
        "max_total_bytes": (1, 52_428_800),
        "max_asset_bytes": (1, 15_728_640),
        "max_html_bytes": (1, 2_097_152),
        "min_dimension": (1, 480),
        "max_official_images": (0, 2),
        "max_commons_assets": (0, 1),
        "max_redirects": (0, 3),
    }
    for key, (minimum, maximum) in expected_probe_limits.items():
        value = int(material_probe.get(key) if material_probe.get(key) is not None else -1)
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"素材探针预算 {key} 超出任务合同")
    if int(material_probe["max_asset_bytes"]) > int(material_probe["max_total_bytes"]):
        raise ConfigurationError("素材探针单素材上限不能超过总下载上限")
    fake_ip_networks = material_probe.get("fake_ip_networks")
    if set(fake_ip_networks or []) != {"198.18.0.0/15", "fd7a:746f:6c69:6e65:66::/96"}:
        raise ConfigurationError("素材探针 Fake-IP DNS 兼容网段与本机受控代理合同不一致")
    for key in ("story_path", "output_root", "temp_root"):
        value = Path(str(material_probe.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"jobs.material_probe.{key} 必须是项目内相对路径")
    allowed_domains = material_probe.get("allowed_domains")
    if not isinstance(allowed_domains, list) or not allowed_domains:
        raise ConfigurationError("jobs.material_probe.allowed_domains 必须是非空数组")
    for domain in allowed_domains:
        value = str(domain or "").strip().casefold().rstrip(".")
        if not value or ":" in value or "/" in value or value == "localhost":
            raise ConfigurationError("素材探针允许域名无效")
    licenses = material_probe.get("supported_commons_licenses")
    if not isinstance(licenses, list) or not licenses or not all(str(item).strip() for item in licenses):
        raise ConfigurationError("素材探针 Commons 许可白名单必须是非空数组")
    trusted_accounts = payload.get("trusted_news_accounts")
    if not isinstance(trusted_accounts, list) or not trusted_accounts:
        raise ConfigurationError("trusted_news_accounts 必须是非空数组")
    seen_trusted: set[str] = set()
    for account in trusted_accounts:
        if not isinstance(account, dict) or not account.get("id") or not account.get("name"):
            raise ConfigurationError("可信新闻账号缺少 id 或 name")
        account_id = str(account["id"])
        if account_id in seen_trusted:
            raise ConfigurationError(f"可信新闻账号重复：{account_id}")
        seen_trusted.add(account_id)
        stable_id = str(account.get("stable_id") or "")
        if not SEC_UID_RE.fullmatch(stable_id):
            raise ConfigurationError(f"可信新闻账号 {account_id} 缺少有效稳定 ID")
        expected_hash = str(account.get("expected_creator_hash") or "")
        if expected_hash and not re.fullmatch(r"[a-f0-9]{16,64}", expected_hash):
            raise ConfigurationError(f"可信新闻账号 {account_id} 的匿名作者哈希无效")
        profile = str(account.get("profile_url") or "")
        share = str(account.get("share_entry_url") or "")
        if profile != f"https://www.douyin.com/user/{stable_id}" or not share.startswith("https://v.douyin.com/"):
            raise ConfigurationError(f"可信新闻账号 {account_id} 的 HTTPS 入口或主页无效")
        if account.get("source_tier") != "trusted_creator" or account.get("approval_basis") != "user_curated_account":
            raise ConfigurationError(f"可信新闻账号 {account_id} 必须由用户配置批准")
        profile = str(account.get("content_extraction_profile") or "speech_first")
        audio_policy = str(account.get("audio_policy") or "allowed")
        if profile == "visual_text_only" and (audio_policy != "never" or account.get("frame_strategy") != "scene_plus_interval_guard"):
            raise ConfigurationError(f"视觉文字账号 {account_id} 必须禁用音频并使用视觉兜底选帧")
        if not 1 <= int(account.get("max_items") or 0) <= 10 or int(account.get("window_hours") or 0) != 48:
            raise ConfigurationError(f"可信新闻账号 {account_id} 的窗口或上限无效")
    reference_max = int(inspiration.get("hard_max_reference_videos") or 0)
    detail_max = int(inspiration.get("max_detail_videos") or 0)
    media_max = int(inspiration.get("hard_max_media_videos") or 0)
    if not 1 <= reference_max <= 100 or not 1 <= detail_max <= 20 or not 1 <= media_max <= 5:
        raise ConfigurationError("灵感任务上限必须满足参考<=100、深读<=20、媒体<=5")
    if int(inspiration.get("default_reference_videos") or 0) > reference_max:
        raise ConfigurationError("默认参考数不能超过硬上限")
    workbench = payload.get("workbench")
    if not isinstance(workbench, dict):
        raise ConfigurationError("workbench 必须是对象")
    for key in ("editorial_override_path", "editorial_output_root"):
        value = Path(str(workbench.get(key) or ""))
        if not str(value) or value.is_absolute() or ".." in value.parts:
            raise ConfigurationError(f"workbench.{key} 必须是项目内相对路径")
    return payload
