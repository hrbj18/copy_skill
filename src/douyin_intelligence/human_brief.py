from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit


_SPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNSAFE_MARKDOWN = str.maketrans(
    {
        "\\": "＼",
        "#": "＃",
        "*": "＊",
        "_": "＿",
        "[": "［",
        "]": "］",
        "<": "＜",
        ">": "＞",
        "|": "｜",
        "!": "！",
        "`": "｀",
    }
)


def _plain(value: Any, *, limit: int = 220, fallback: str = "未提供") -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    text = _URL.sub("［外部链接已移除］", text).translate(_UNSAFE_MARKDOWN)
    if not text:
        return fallback
    if limit > 0 and len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _safe_https(value: Any) -> str:
    url = str(value or "").strip()
    if any(character.isspace() or character in "<>" for character in url):
        return ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return url.replace("(", "%28").replace(")", "%29")


def _safe_video_url(value: Any) -> str:
    url = _safe_https(value)
    parsed = urlsplit(url) if url else None
    if not parsed or parsed.netloc not in {"douyin.com", "www.douyin.com"}:
        return ""
    return url


def _safe_image_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or path.parts[0] != "images" or ".." in path.parts:
        return ""
    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return ""
    return path.as_posix().replace("(", "%28").replace(")", "%29")


def _safe_artifact_prefix(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw or raw == ".":
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact_prefix 必须是无上跳的安全相对路径")
    return path.as_posix()


def _artifact_path(prefix: str, relative: str) -> str:
    return f"{prefix}/{relative}" if prefix else relative


def _image_state(event: dict[str, Any]) -> str:
    images = event.get("images") if isinstance(event.get("images"), list) else []
    if images:
        return f"已配图 {len(images)} 张"
    status = event.get("image_status") if isinstance(event.get("image_status"), dict) else {}
    if status.get("state") == "not_ranked_top3":
        return "未在 Top 3 配图范围"
    return "暂无合格配图"


def _image_block(event: dict[str, Any], title: str, artifact_prefix: str) -> list[str]:
    images = event.get("images") if isinstance(event.get("images"), list) else []
    lines = ["### 已获取配图", ""]
    rendered = 0
    for index, asset in enumerate(images, start=1):
        if not isinstance(asset, dict):
            continue
        package_relative = _safe_image_path(asset.get("relative_path"))
        if not package_relative:
            continue
        relative = _artifact_path(artifact_prefix, package_relative)
        rendered += 1
        alt = _plain(f"{title} 配图 {index}", limit=100)
        width = int(asset.get("width") or 0)
        height = int(asset.get("height") or 0)
        source_name = _plain(asset.get("source_name"), limit=80, fallback="受控图片来源")
        source_url = _safe_https(asset.get("source_article_url"))
        source = f"[{source_name}]({source_url})" if source_url else source_name
        rights_value = str(asset.get("rights_status") or "review_required").strip()
        rights = rights_value if _SAFE_TOKEN.fullmatch(rights_value) else "review_required"
        lines.extend(
            [
                f"![{alt}]({relative})",
                "",
                f"图源：{source}　尺寸：{width}×{height}　权利状态：`{rights}`",
                "",
            ]
        )
    if rendered:
        lines.append("> 图片仅作素材候选，使用前必须完成人工内容与权利复核。")
        lines.append("")
        return lines

    status = event.get("image_status") if isinstance(event.get("image_status"), dict) else {}
    if status.get("state") == "not_ranked_top3":
        message = "本流程只为推荐 Top 3 尝试配图，本条未进入图片获取范围。"
    elif images:
        message = "包内存在图片记录，但没有可安全嵌入的有效相对路径。"
    else:
        reason = _plain(status.get("reason"), limit=140, fallback="未取得通过质量门槛的图片")
        message = f"暂无合格配图：{reason}。"
    lines.extend([f"> {message}没有使用视频截图、泛化图片或错误事件图片补位。", ""])
    return lines


def render_human_brief(pack: dict[str, Any], *, artifact_prefix: str = "") -> str:
    """Render a concise, image-forward Markdown view without mutating the pack."""
    artifact_prefix = _safe_artifact_prefix(artifact_prefix)
    candidates = pack.get("candidates") if isinstance(pack.get("candidates"), list) else []
    counts = pack.get("counts") if isinstance(pack.get("counts"), dict) else {}
    business_date = _plain(pack.get("business_date"), limit=20)
    status = _plain(pack.get("status"), limit=30)
    image_count = int(counts.get("images") or sum(len(item.get("images") or []) for item in candidates if isinstance(item, dict)))
    disclaimer = _plain(pack.get("disclaimer"), limit=240, fallback="copy_skill 未核验真实性，候选仅供选题与后续核验。")
    ready_news = [item for item in candidates if isinstance(item, dict) and item.get("news_readiness") == "ready"]
    other_content = [item for item in candidates if isinstance(item, dict) and item.get("news_readiness") != "ready"]

    lines = [
        f"# {business_date} 昨日科技热点图文简报",
        "",
        f"> {disclaimer}",
        "",
        f"**运行状态** `{status}`　 **新闻候选** {len(ready_news)} 条　 **科技内容/待补充** {len(other_content)} 条　 **已获取配图** {image_count} 张",
        "",
        "## 昨日科技新闻候选",
        "",
        "> 仅收录已经提取出主体、具体动作和明确对象的事件。这里的“可读”不等于“真实”，播报前仍须核验。",
        "",
        "| 推荐 | 热点新闻 | 推荐分 | 原始热度（热度名次） | 来源视频 | 配图 |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for event in ready_news:
        title = _plain(event.get("news_headline") or event.get("canonical_title") or event.get("title"), limit=90)
        lines.append(
            f"| {int(event.get('delivery_rank') or event.get('rank') or 0)} | {title} | {float(event.get('delivery_priority_score') or event.get('heat_score') or 0):.3f} | "
            f"{float(event.get('heat_score') or 0):.3f}（第 {int(event.get('heat_rank') or event.get('rank') or 0)}） | "
            f"{int(event.get('video_count') or len(event.get('contributing_videos') or []))} | {_image_state(event)} |"
        )
    if not ready_news:
        lines.append("| — | 当前没有达到事件完整度要求的新闻 | — | — | — | — |")

    lines.extend([
        "",
        "## 高热度科技内容与待补充线索",
        "",
        "> 这些内容仍保留原始热度和来源，可供评测、教程或选题灵感使用，但不能直接当作完整新闻播报。",
        "",
        "| 推荐 | 内容线索 | 类型 | 新闻准备度 | 原始热度（热度名次） | 未进入新闻区原因 |",
        "|---:|---|---|---|---:|---|",
    ])
    for event in other_content:
        title = _plain(event.get("canonical_title") or event.get("title"), limit=90)
        completeness = event.get("event_completeness") if isinstance(event.get("event_completeness"), dict) else {}
        missing = completeness.get("missing") if isinstance(completeness.get("missing"), list) else []
        reason = "缺少 " + "、".join(_plain(value, limit=30) for value in missing) if missing else "内容类型不属于具体新闻"
        lines.append(
            f"| {int(event.get('delivery_rank') or event.get('rank') or 0)} | {title} | {_plain(event.get('content_type'), limit=40, fallback='legacy_unclassified')} | "
            f"{_plain(event.get('news_readiness'), limit=40, fallback='legacy_missing')} | {float(event.get('heat_score') or 0):.3f}"
            f"（第 {int(event.get('heat_rank') or event.get('rank') or 0)}） | {reason} |"
        )

    for lane_title, lane_items in (("新闻候选详情", ready_news), ("科技内容与待补充详情", other_content)):
        if not lane_items:
            continue
        lines.extend(["", "---", "", f"## {lane_title}", ""])
        for event in lane_items:
            rank = int(event.get("delivery_rank") or event.get("rank") or 0)
            title = _plain(event.get("news_headline") or event.get("canonical_title") or event.get("title"), limit=120)
            summary = _plain(event.get("event_summary") or title, limit=320)
            extraction = _plain(event.get("extraction_status"), limit=40, fallback="not_run")
            story_id = _plain(event.get("story_id") or event.get("event_id"), limit=80)
            lines.extend(
                [
                    "",
                    f"### {rank}. {title}",
                    "",
                    f"**推荐分** `{float(event.get('delivery_priority_score') or event.get('heat_score') or 0):.3f}`　 **原始热度** `{float(event.get('heat_score') or 0):.3f}`（第 {int(event.get('heat_rank') or event.get('rank') or 0)}）　 **来源视频** {int(event.get('video_count') or len(event.get('contributing_videos') or []))} 条　 **内容整理** `{extraction}`　 **类型/准备度** `{_plain(event.get('content_type'), limit=40, fallback='legacy_unclassified')}` / `{_plain(event.get('news_readiness'), limit=40, fallback='legacy_missing')}`",
                    "",
                    f"> {summary}",
                    "",
                ]
            )
            lines.extend(_image_block(event, title, artifact_prefix))

            points = event.get("key_points") if isinstance(event.get("key_points"), list) else []
            angles = event.get("content_angles") if isinstance(event.get("content_angles"), list) else []
            claims = event.get("claims_to_verify") if isinstance(event.get("claims_to_verify"), list) else []
            lines.extend(["#### 主要信息", ""])
            if points:
                lines.extend(f"- {_plain(point, limit=150)}" for point in points[:4])
            else:
                lines.append("- 当前只有标题级线索，建议核对来源视频后再决定是否采用。")
            if angles:
                lines.append(f"- 可选角度：{'、'.join(_plain(angle, limit=50) for angle in angles[:4])}")

            lines.extend(["", "#### 采用前提醒", ""])
            if claims:
                lines.extend(f"- 待核验：{_plain(claim, limit=160)}" for claim in claims[:5])
            else:
                lines.append("- 整条抖音线索尚未由 copy_skill 核验。")

            videos = event.get("contributing_videos") if isinstance(event.get("contributing_videos"), list) else []
            lines.extend(["", "#### 来源视频", ""])
            for video in videos[:6]:
                if not isinstance(video, dict):
                    continue
                video_title = _plain(video.get("title"), limit=110, fallback="查看来源视频")
                url = _safe_video_url(video.get("share_url"))
                label = f"[{video_title}]({url})" if url else video_title
                author = _plain(video.get("author"), limit=40, fallback="未知账号")
                published = _plain(video.get("published_at"), limit=40, fallback="时间未知")
                lines.append(f"- {label}　{author}　{published}")
            if len(videos) > 6:
                lines.append(f"- 另有 {len(videos) - 6} 条贡献视频，请查看技术审计稿或机器合同。")
            lines.extend(["", f"事件 ID：`{story_id}`"])

    lines.extend(
        [
            "",
            "---",
            "",
            "## 使用说明",
            "",
            "- 热度只代表公开互动、时间与来源覆盖，不代表新闻真实或适合播报。",
            "- 图片只在实际获取并通过项目质量门槛时展示，所有图片仍需人工复核内容与权利。",
            f"- 完整热度分量、矩阵去重、聚类理由和全部视频请查看[技术审计稿]({_artifact_path(artifact_prefix, '昨日抖音科技热点候选.md')})。",
            f"- 下游程序请读取[candidate-pool.json]({_artifact_path(artifact_prefix, 'candidate-pool.json')})，不要解析本图文简报。",
            "",
        ]
    )
    return "\n".join(lines)
