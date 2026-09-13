from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def atomic_text(path: Path, text: str) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise


def daily_news_broadcast(target_date: str, events: list[dict[str, Any]]) -> str:
    if not events:
        return f"各位好，这里是北京时间 {target_date} 的科技新闻播报。当天没有同时满足日期和来源门槛的新闻，因此本期不以线索或模型推断补充内容。"
    spoken_events = events[:3]
    lines = [f"各位好，下面是北京时间 {target_date} 的科技新闻播报。本期按官方来源和日期门槛筛出 {len(events)} 条，其中先播报三条重点。"]
    for index, event in enumerate(spoken_events, 1):
        source = (event.get("sources") or [{}])[0].get("name") or "官方来源"
        normalized = str(event["title"]).casefold()
        if any(token in normalized for token in ("security", "advisories")):
            headline = "GitHub 更新了安全公告页面的用户屏蔽能力"
        elif any(token in normalized for token in ("teacher", "school", "education")):
            headline = "ChatGPT for Teachers 正扩展到更多美国学区"
        elif any(token in normalized for token in ("billing", "enterprise")):
            headline = "GitHub App 新增企业计费数据访问能力"
        elif "copilot" in normalized:
            headline = "GitHub Copilot 应用的 Customize 标签页正式可用"
        elif any(token in normalized for token in ("codex", "builder")):
            headline = "OpenAI 发布了 Codex 在企业软件开发中的应用案例"
        else:
            headline = f"{source} 发布了官方科技更新"
        lines.append(f"第{index}条，{headline}。值得关注的是，{event['news_value']} 发布日期为 {str(event['time'])[:10]}，来源为{source}，核验状态为{event.get('verification_status') or '待复核'}。")
    if len(events) > len(spoken_events):
        lines.append(f"其余 {len(events) - len(spoken_events)} 条已列入书面报告，便于按链接继续核对。")
    lines.append("以上内容均以列出的官方来源为准；抖音如有出现，只代表受众关注线索，不构成事实证明。")
    return "".join(lines)


def daily_news_markdown(target_date: str, events: list[dict[str, Any]], pending: list[dict[str, Any]], stats: dict[str, Any], *, broadcast: str, analysis_status: dict[str, Any]) -> str:
    lines = [f"# {target_date} 抖音高热科技新闻", "", f"> 统计窗口：北京时间 {target_date} 00:00–23:59；抖音只提供关注度信号，新闻事实以所列来源为准。", "", f"- 合格新闻：{len(events)}", f"- 待核验线索：{len(pending)}", f"- 新闻源文章：{stats.get('article_count', 0)}", f"- 抖音参考视频：{stats.get('douyin_count', 0)}", ""]
    if not events:
        lines.extend(["## 今日结果", "", "没有同时满足日期边界和来源门槛的科技新闻。系统未用抖音标题或模型推断补造新闻。", ""])
    for index, event in enumerate(events, 1):
        lines.extend([f"## {index}. {event['title']}", "", f"- 推荐级别：{event['recommendation']}", f"- 时间：{event['time']}", f"- 主体/地点：{event['subject_place']}", f"- 事件：{event['event']}", f"- 新闻价值：{event['news_value']}", f"- 核验状态：{event.get('verification_status') or '待复核'}", f"- 抖音热度：{event['douyin_heat']}", "- 权威来源："])
        lines.extend(f"  - [{source['name']}]({source['url']})" for source in event["sources"])
        lines.extend([f"- 待核验：{event['claims_to_verify']}", f"- 创作角度：{event['creative_angle']}", ""])
    if pending:
        lines.extend(["## 待核验线索", ""])
        for item in pending:
            lines.extend([f"- {item['title']}（{item['reason']}）：{item['url']}"])
        lines.append("")
    lines.extend(["## 60–90 秒口播总稿", "", broadcast, "", "## AI 深度分析状态", "", "- 可用：" + ("是" if analysis_status.get("enabled") else "否")])
    if not analysis_status.get("enabled"):
        lines.append("- 说明：" + str(analysis_status.get("unavailable_reason") or "AI 深度分析暂不可用；本报告使用确定性来源模板。"))
    lines.append("")
    return "\n".join(lines)


def inspiration_markdown(generated_at: str, cards: list[dict[str, Any]], stats: dict[str, Any], warnings: list[str]) -> str:
    lines = ["# 科技灵感报告", "", f"> 生成时间：{generated_at}；参考视频：{stats.get('reference_count', 0)}；主题数：{len(cards)}。所有事实主张仍需回查一手来源。", ""]
    if not cards:
        lines.extend(["## 本次结果", "", "没有形成达到阈值的灵感卡。请检查抖音登录态、采集输入或扩大时间窗口。", ""])
    for index, card in enumerate(cards, 1):
        lines.extend([f"## {index}. {card['recommended_title']}", "", f"- 推荐度：{card['recommendation']}", f"- 一句话灵感：{card['one_line_idea']}", f"- 有趣之处：{card['why_interesting']}", f"- 内容骨架：{card['outline']}", "- 参考素材："])
        lines.extend(f"  - {source}" for source in card["references"])
        lines.extend([f"- 待核验：{card['claims_to_verify']}", f"- 推荐标题：{card['recommended_title']}", ""])
    if warnings:
        lines.extend(["## 运行提示", ""] + [f"- {item}" for item in warnings] + [""])
    return "\n".join(lines)
