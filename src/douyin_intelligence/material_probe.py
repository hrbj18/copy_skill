from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from .job_runtime import JobLock, JobState, now_iso


SENSITIVE_QUERY_KEYS = re.compile(r"(?:access|auth|authorization|cookie|expires?|key|secret|sig|signature|token)", re.IGNORECASE)
STORY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SUPPORTED_IMAGE_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
RIGHTS_DIRECTORIES = {
    "project_generated": "renderable",
    "renderable_with_attribution": "renderable",
    "review_required": "review-required",
    "reference_only": "reference-only",
}


class MaterialProbeError(RuntimeError):
    """A bounded, user-actionable material probe failure."""


class HTTPStatusProbeError(MaterialProbeError):
    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(f"来源返回 HTTP {self.status_code}")


def _project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root = Path(str(config.get("_project_root") or Path(__file__).resolve().parents[2]))
    return root / path


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
    except ValueError:
        return ""
    safe_query = urlencode([(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if not SENSITIVE_QUERY_KEYS.search(key)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, ""))


def _safe_error(exc: BaseException) -> str:
    text = re.sub(r"(?i)(authorization|cookie|api[_-]?key|secret|token)\s*[:=]\s*\S+", r"\1=[已隐藏]", str(exc))
    text = re.sub(r"https://\S+", lambda match: redact_url(match.group(0).rstrip(".,;:)")), text)
    return text[:300] or type(exc).__name__


def _canonical_host(value: str) -> str:
    return value.casefold().rstrip(".")


def validate_https_url(value: str, allowed_domains: Iterable[str]) -> str:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError as exc:
        raise MaterialProbeError("来源 URL 无效") from exc
    host = _canonical_host(parts.hostname or "")
    allowed = {_canonical_host(item) for item in allowed_domains}
    if parts.scheme != "https" or not host or host not in allowed:
        raise MaterialProbeError("来源 URL 必须使用 HTTPS 且属于素材探针允许域名")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise MaterialProbeError("来源 URL 不得包含认证信息或非标准端口")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise MaterialProbeError("来源 URL 不得指向本机或私网")
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def assert_public_dns(host: str, resolver: Callable[..., Any] = socket.getaddrinfo, *, fake_ip_networks: Iterable[str] = ()) -> None:
    try:
        addresses = {row[4][0] for row in resolver(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise MaterialProbeError("来源域名无法解析") from exc
    if not addresses:
        raise MaterialProbeError("来源域名没有可用地址")
    synthetic = [ipaddress.ip_network(value) for value in fake_ip_networks]
    for value in addresses:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if any(address in network for network in synthetic):
            continue
        if not address.is_global:
            raise MaterialProbeError("来源域名解析到本机、私网或保留地址")


def validate_story(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MaterialProbeError("故事输入必须是 JSON 对象")
    story_id = str(payload.get("story_id") or "").strip()
    if not STORY_ID_RE.fullmatch(story_id):
        raise MaterialProbeError("story_id 必须是 3 到 64 位小写字母、数字或连字符")
    try:
        target_date = date.fromisoformat(str(payload.get("target_date") or ""))
    except ValueError as exc:
        raise MaterialProbeError("target_date 必须是 YYYY-MM-DD") from exc
    title = str(payload.get("title_zh") or "").strip()
    summary = str(payload.get("summary_zh") or "").strip()
    if not 4 <= len(title) <= 160 or not 12 <= len(summary) <= 800:
        raise MaterialProbeError("新闻标题或中性摘要长度不符合要求")
    if payload.get("confirmation_status") != "official_primary_source":
        raise MaterialProbeError("素材探针只接受已由官方一手来源确认的新闻")
    sources = payload.get("official_sources")
    if not isinstance(sources, list) or not sources:
        raise MaterialProbeError("至少需要一个官方一手来源")
    normalized_sources: list[dict[str, str]] = []
    for item in sources[:3]:
        if not isinstance(item, dict):
            raise MaterialProbeError("官方来源格式无效")
        normalized_sources.append({
            "publisher": str(item.get("publisher") or "官方来源").strip()[:120],
            "role": str(item.get("role") or "官方一手来源").strip()[:120],
            "url": redact_url(validate_https_url(str(item.get("url") or ""), settings["allowed_domains"])),
        })
    commons_query = str(payload.get("commons_query") or "").strip()
    if len(commons_query) > 120:
        raise MaterialProbeError("Commons 查询词过长")
    return {
        "schema_version": "1.0",
        "story_id": story_id,
        "target_date": target_date.isoformat(),
        "title_zh": title,
        "summary_zh": summary,
        "confirmation_status": "official_primary_source",
        "official_sources": normalized_sources,
        "commons_query": commons_query,
    }


@dataclass
class RequestBudget:
    max_requests: int
    max_total_bytes: int
    total_timeout_seconds: float
    started_at: float
    request_count: int = 0
    downloaded_bytes: int = 0

    def check_time(self) -> None:
        if time.monotonic() - self.started_at > self.total_timeout_seconds:
            raise MaterialProbeError("素材探针超过 5 分钟总时间预算")

    def start_request(self) -> None:
        self.check_time()
        if self.request_count >= self.max_requests:
            raise MaterialProbeError("素材探针已达到 10 次网络请求上限")
        self.request_count += 1

    def add_bytes(self, size: int) -> None:
        self.check_time()
        if self.downloaded_bytes + size > self.max_total_bytes:
            raise MaterialProbeError("素材探针已达到 50 MB 总下载上限")
        self.downloaded_bytes += size

    def snapshot(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "max_requests": self.max_requests,
            "downloaded_bytes": self.downloaded_bytes,
            "max_total_bytes": self.max_total_bytes,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
            "total_timeout_seconds": self.total_timeout_seconds,
        }


class SafeFetcher:
    def __init__(
        self,
        settings: dict[str, Any],
        budget: RequestBudget,
        *,
        client: httpx.Client | None = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ):
        self.settings = settings
        self.budget = budget
        self.resolver = resolver
        self._owns_client = client is None
        timeout = float(settings["request_timeout_seconds"])
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def get(self, url: str, *, maximum_bytes: int, accepted_types: tuple[str, ...], allow_truncated: bool = False) -> tuple[str, str, bytes]:
        current = validate_https_url(url, self.settings["allowed_domains"])
        for redirect_index in range(int(self.settings["max_redirects"]) + 1):
            parts = urlsplit(current)
            assert_public_dns(parts.hostname or "", self.resolver, fake_ip_networks=self.settings.get("fake_ip_networks") or ())
            self.budget.start_request()
            with self.client.stream(
                "GET",
                current,
                headers={"User-Agent": "copy-skill-material-probe/1.0", "Accept": ", ".join(accepted_types)},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_index >= int(self.settings["max_redirects"]):
                        raise MaterialProbeError("来源重定向次数超出上限")
                    current = validate_https_url(urljoin(current, location), self.settings["allowed_domains"])
                    continue
                if response.status_code != 200:
                    raise HTTPStatusProbeError(response.status_code)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if not any(content_type == expected or expected.endswith("/*") and content_type.startswith(expected[:-1]) for expected in accepted_types):
                    raise MaterialProbeError(f"来源内容类型不受支持：{content_type or '未知'}")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > maximum_bytes and not allow_truncated:
                    raise MaterialProbeError("来源响应超过单次大小上限")
                chunks: list[bytes] = []
                current_size = 0
                for chunk in response.iter_bytes():
                    current_size += len(chunk)
                    if current_size > maximum_bytes:
                        if allow_truncated:
                            remaining = maximum_bytes - (current_size - len(chunk))
                            if remaining > 0:
                                self.budget.add_bytes(remaining)
                                chunks.append(chunk[:remaining])
                            return current, content_type, b"".join(chunks)
                        raise MaterialProbeError("来源响应超过单次大小上限")
                    self.budget.add_bytes(len(chunk))
                    chunks.append(chunk)
                return current, content_type, b"".join(chunks)
        raise MaterialProbeError("来源重定向失败")


class _ImageCandidateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.primary: list[str] = []
        self.secondary: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "").strip() for key, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"} and values.get("content"):
                self.primary.append(values["content"])
        elif tag.casefold() == "img":
            candidate = values.get("src") or values.get("data-src") or values.get("data-original")
            if candidate:
                self.secondary.append(candidate)


def extract_image_candidates(page_url: str, document: bytes, allowed_domains: Iterable[str]) -> list[str]:
    parser = _ImageCandidateParser()
    parser.feed(document.decode("utf-8", errors="replace"))
    result: list[str] = []
    for value in parser.primary + parser.secondary:
        try:
            candidate = validate_https_url(urljoin(page_url, html.unescape(value)), allowed_domains)
        except MaterialProbeError:
            continue
        if candidate not in result:
            result.append(candidate)
        if len(result) >= 8:
            break
    return result


def _decode_image(data: bytes, content_type: str, minimum_dimension: int) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            if image_format not in SUPPORTED_IMAGE_FORMATS:
                raise MaterialProbeError("图片实际格式不受支持")
            if bool(getattr(image, "is_animated", False)):
                raise MaterialProbeError("不接受动画图片")
            expected_mime, extension = SUPPORTED_IMAGE_FORMATS[image_format]
            if content_type != expected_mime:
                raise MaterialProbeError("响应 MIME 与图片实际格式不一致")
            width, height = image.size
            if min(width, height) < minimum_dimension:
                raise MaterialProbeError(f"图片尺寸不足：{width}×{height}")
            grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(grayscale.get_flattened_data())
            bits = [pixels[row * 9 + column] > pixels[row * 9 + column + 1] for row in range(8) for column in range(8)]
            dhash = sum((1 << index) for index, enabled in enumerate(bits) if enabled)
            average = image.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
            return {
                "format": image_format, "extension": extension, "mime_type": expected_mime,
                "width": width, "height": height, "dhash": f"{dhash:016x}", "average_rgb": list(average),
            }
    except (UnidentifiedImageError, OSError) as exc:
        raise MaterialProbeError("响应无法解码为有效图片") from exc


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _asset_identifier(source_kind: str, source_url: str, digest: str) -> str:
    value = hashlib.sha256(f"{source_kind}\n{redact_url(source_url)}\n{digest}".encode("utf-8")).hexdigest()[:16]
    return f"asset-{value}"


def _store_asset(
    stage: Path,
    assets: list[dict[str, Any]],
    data: bytes,
    content_type: str,
    settings: dict[str, Any],
    *,
    source_kind: str,
    source_url: str,
    direct_asset_url: str,
    rights_status: str,
    author: str,
    license_name: str,
    license_url: str,
    attribution_text: str,
    usage_requirements: str,
    relevance: str,
    suggested_role: str,
) -> dict[str, Any] | None:
    if len(assets) >= int(settings["max_assets"]):
        raise MaterialProbeError("素材数量已达到 5 个上限")
    info = _decode_image(data, content_type, int(settings["min_dimension"]))
    digest = hashlib.sha256(data).hexdigest()
    for existing in assets:
        color_delta = max(abs(int(existing["average_rgb"][index]) - int(info["average_rgb"][index])) for index in range(3))
        if existing["sha256"] == digest or _hamming(existing["perceptual_hash"], info["dhash"]) <= 3 and color_delta <= 24:
            return None
    asset_id = _asset_identifier(source_kind, source_url, digest)
    relative = Path(RIGHTS_DIRECTORIES[rights_status]) / f"{asset_id}{info['extension']}"
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    item = {
        "asset_id": asset_id,
        "local_path": relative.as_posix(),
        "source_kind": source_kind,
        "source_url": redact_url(source_url),
        "direct_asset_url": redact_url(direct_asset_url),
        "rights_status": rights_status,
        "renderable": rights_status in {"project_generated", "renderable_with_attribution"},
        "author": author[:300],
        "license_name": license_name[:160],
        "license_url": redact_url(license_url),
        "attribution_text": attribution_text[:500],
        "usage_requirements": usage_requirements[:500],
        "sha256": digest,
        "perceptual_hash": info["dhash"],
        "average_rgb": info["average_rgb"],
        "mime_type": info["mime_type"],
        "width": info["width"],
        "height": info["height"],
        "bytes": len(data),
        "relevance": relevance[:300],
        "suggested_role": suggested_role[:120],
    }
    assets.append(item)
    return item


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key) or {}
    raw = value.get("value") if isinstance(value, dict) else value
    return str(raw or "").strip()


def _plain_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _license_supported(name: str, settings: dict[str, Any]) -> bool:
    normalized = " ".join(name.casefold().replace("creative commons", "").replace("attribution-share alike", "cc by-sa").replace("attribution", "cc by").split())
    supported = {" ".join(str(item).casefold().split()) for item in settings["supported_commons_licenses"]}
    return normalized in supported or name.casefold().strip() in supported


def _commons_candidates(payload: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, str]]:
    pages = ((payload.get("query") or {}).get("pages") or {}) if isinstance(payload, dict) else {}
    rows: list[dict[str, str]] = []
    page_rows = list(pages.values()) if isinstance(pages, dict) else list(pages) if isinstance(pages, list) else []
    for page in sorted(page_rows, key=lambda item: int(item.get("index") or 9999)):
        image_info = (page.get("imageinfo") or [{}])[0]
        metadata = image_info.get("extmetadata") or {}
        license_name = _plain_html(_metadata_value(metadata, "LicenseShortName") or _metadata_value(metadata, "UsageTerms"))
        license_url = _plain_html(_metadata_value(metadata, "LicenseUrl"))
        author = _plain_html(_metadata_value(metadata, "Artist") or _metadata_value(metadata, "Credit"))
        mime = str(image_info.get("mime") or "").casefold()
        width = int(image_info.get("thumbwidth") or image_info.get("width") or 0)
        height = int(image_info.get("thumbheight") or image_info.get("height") or 0)
        size = int(image_info.get("thumbsize") or image_info.get("size") or 0)
        download_url = str(image_info.get("thumburl") or image_info.get("url") or "")
        source_url = str(image_info.get("descriptionurl") or "")
        if (
            mime in {"image/jpeg", "image/png", "image/webp"}
            and min(width, height) >= int(settings["min_dimension"])
            and 0 < size <= int(settings["max_asset_bytes"])
            and author and license_url and _license_supported(license_name, settings)
            and download_url and source_url
        ):
            rows.append({
                "download_url": download_url,
                "source_url": source_url,
                "author": author,
                "license_name": license_name,
                "license_url": license_url,
                "mime_type": mime,
            })
    return rows


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, maximum_width: int) -> str:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > maximum_width and character not in "，。；：、！？,.!?;:":
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def _balanced_two_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, maximum_width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= maximum_width:
        return text
    candidates: list[tuple[float, int, str, str]] = []
    for index in range(4, len(text) - 3):
        if text[index - 1].isascii() and text[index - 1].isalnum() and text[index].isascii() and text[index].isalnum():
            continue
        left, right = text[:index].rstrip(), text[index:].lstrip()
        left_width = draw.textbbox((0, 0), left, font=font)[2]
        right_width = draw.textbbox((0, 0), right, font=font)[2]
        if left_width <= maximum_width and right_width <= maximum_width:
            punctuation_penalty = 200 if right[:1] in "，。；：、！？,.!?;:" else 0
            candidates.append((abs(left_width - right_width) + punctuation_penalty, index, left, right))
    if not candidates:
        return _wrap(draw, text, font, maximum_width)
    _score, _index, left, right = min(candidates, key=lambda item: (item[0], item[1]))
    return f"{left}\n{right}"


def _generated_card(story: dict[str, Any], *, kind: str) -> bytes:
    canvas = Image.new("RGB", (1920, 1080), "#071629")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((80, 80, 1840, 1000), radius=40, fill="#0d2540", outline="#2d81c8", width=3)
    draw.rectangle((80, 80, 116, 1000), fill="#38bdf8")
    label_font = _font(38, bold=True)
    title_font = _font(82, bold=True)
    body_font = _font(44)
    small_font = _font(32)
    if kind == "title_card":
        draw.text((170, 155), "已确认科技新闻 · 素材探针", font=label_font, fill="#7dd3fc")
        wrapped = _balanced_two_lines(draw, story["title_zh"], title_font, 1480)
        draw.multiline_text((170, 285), wrapped, font=title_font, fill="#ffffff", spacing=30)
        draw.text((170, 885), "仅用于后续编辑排版，不代表新闻现场画面", font=small_font, fill="#94a3b8")
    else:
        source = story["official_sources"][0]
        draw.text((170, 155), "官方一手来源", font=label_font, fill="#7dd3fc")
        draw.text((170, 290), source["publisher"], font=title_font, fill="#ffffff")
        summary = _wrap(draw, story["summary_zh"], body_font, 1480)
        draw.multiline_text((170, 445), summary, font=body_font, fill="#dbeafe", spacing=20)
        domain = urlsplit(source["url"]).hostname or ""
        draw.text((170, 885), f"来源：{domain}  ·  请在成片说明中保留来源", font=small_font, fill="#94a3b8")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def _preview_markdown(story: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        f"# {story['title_zh']}：最小素材包",
        "",
        f"状态：`{manifest['status']}`  ·  官方确认：`{manifest['confirmation']['status']}`",
        "",
        f"> {story['summary_zh']}",
        "",
        "## 使用边界",
        "",
        "抖音仅提供选题与热度线索；本素材包的事实入口是下列官方一手来源。权利状态是工程分流标记，不构成法律意见。`review_required` 素材不能自动进入成片。",
        "",
    ]
    for source in story["official_sources"]:
        lines.append(f"- [{source['publisher']}]({source['url']})：{source['role']}")
    lines.extend(["", "## 素材", ""])
    if not manifest["assets"]:
        lines.append("本次没有取得可保存素材。")
    for item in manifest["assets"]:
        usage = "可渲染" if item["renderable"] else "不可自动渲染"
        lines.extend([
            f"### {item['asset_id']} · {item['rights_status']}", "",
            f"- 文件：`{item['local_path']}`（{item['width']}×{item['height']}，{item['bytes']} bytes）",
            f"- 用途：{item['suggested_role']}；{item['relevance']}",
            f"- 结论：{usage}",
            f"- 来源：[{item['source_kind']}]({item['source_url']})",
        ])
        if item["author"]:
            lines.append(f"- 作者/署名：{item['author']}")
        if item["license_name"]:
            license_link = f"[{item['license_name']}]({item['license_url']})" if item["license_url"] else item["license_name"]
            lines.append(f"- 许可：{license_link}")
        if item["attribution_text"]:
            lines.append(f"- 成片署名建议：{item['attribution_text']}")
        if item["usage_requirements"]:
            lines.append(f"- 使用义务：{item['usage_requirements']}")
        lines.append("")
    if manifest["failures"]:
        lines.extend(["## 降级记录", ""])
        for failure in manifest["failures"]:
            lines.append(f"- {failure['source']}：{failure['error_type']}（{failure['message']}）")
        lines.append("")
    counts = manifest["counts"]
    budget = manifest["budget"]
    lines.extend([
        "## 运行预算", "",
        f"- 网络请求：{budget['request_count']} / {budget['max_requests']}",
        f"- 下载体积：{budget['downloaded_bytes']} / {budget['max_total_bytes']} bytes",
        f"- 素材：{counts['assets']} / {counts['max_assets']}（可渲染 {counts['renderable']}，需复核 {counts['review_required']}）",
        f"- 耗时：{budget['elapsed_seconds']} / {budget['total_timeout_seconds']} 秒",
        "- OpenMontage 写入：0",
        "- LLM / OCR / ASR / 浏览器调用：0",
        "",
    ])
    return "\n".join(lines)


def _publish_directory(stage: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(stage, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _failure(source: str, exc: BaseException) -> dict[str, str]:
    return {"source": source[:160], "error_type": type(exc).__name__, "message": _safe_error(exc)}


def latest_material_probe_report(config: dict[str, Any]) -> Path | None:
    root = _project_path(config, config["jobs"]["material_probe"]["output_root"])
    reports = list(root.glob("*/*/preview.md")) if root.exists() else []
    return max(reports, key=lambda path: path.stat().st_mtime) if reports else None


def run_material_probe(
    config: dict[str, Any],
    story_path: str | Path | None = None,
    *,
    client: httpx.Client | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, Any]:
    settings = config["jobs"]["material_probe"]
    source_path = _project_path(config, story_path or settings["story_path"])
    try:
        raw_story = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MaterialProbeError(f"故事输入不存在：{source_path}") from exc
    except json.JSONDecodeError as exc:
        raise MaterialProbeError("故事输入不是有效 JSON") from exc
    story = validate_story(raw_story, settings)
    output_root = _project_path(config, settings["output_root"])
    destination = output_root / story["target_date"] / story["story_id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    state = JobState(config, "material_probe")
    with JobLock(config, "material_probe"):
        state.update(status="running", phase="confirmation", output_path="", errors=[], counts={})
        stage = destination.parent / f".{story['story_id']}.stage-{uuid.uuid4().hex}"
        stage.mkdir(parents=True)
        for folder in ("renderable", "review-required", "reference-only"):
            (stage / folder).mkdir()
        assets: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        budget = RequestBudget(
            max_requests=int(settings["max_requests"]),
            max_total_bytes=int(settings["max_total_bytes"]),
            total_timeout_seconds=float(settings["total_timeout_seconds"]),
            started_at=time.monotonic(),
        )
        fetcher = SafeFetcher(settings, budget, client=client, resolver=resolver)
        official_reachable = False
        official_access_blocked = False
        try:
            official = story["official_sources"][0]
            try:
                final_page_url, _mime, document = fetcher.get(
                    official["url"], maximum_bytes=int(settings["max_html_bytes"]), accepted_types=("text/html",),
                )
                official_reachable = True
                state.update(status="running", phase="official_assets")
                attempts = 0
                for candidate in extract_image_candidates(final_page_url, document, settings["allowed_domains"]):
                    if attempts >= int(settings["max_official_images"]) or len(assets) >= int(settings["max_assets"]) - 2:
                        break
                    attempts += 1
                    try:
                        asset_url, content_type, image_data = fetcher.get(
                            candidate, maximum_bytes=int(settings["max_asset_bytes"]), accepted_types=("image/jpeg", "image/png", "image/webp"),
                        )
                        stored = _store_asset(
                            stage, assets, image_data, content_type, settings,
                            source_kind="official_article_image", source_url=official["url"], direct_asset_url=asset_url,
                            rights_status="review_required", author=official["publisher"], license_name="未取得明确复用许可", license_url="",
                            attribution_text="使用前需人工确认权利与署名要求", usage_requirements="不得在人工权利复核前进入成片",
                            relevance="来自已确认新闻的官方文章页面",
                            suggested_role="官方文章主图参考",
                        )
                        if stored is None:
                            failures.append({"source": "official_image", "error_type": "DuplicateAsset", "message": "与已保存素材重复，已跳过"})
                    except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                        failures.append(_failure("official_image", exc))
            except HTTPStatusProbeError as exc:
                failures.append(_failure("official_source", exc))
                official_access_blocked = exc.status_code in {401, 403, 429}
            except (MaterialProbeError, httpx.HTTPError, ValueError) as exc:
                failures.append(_failure("official_source", exc))

            material_steps_allowed = official_reachable or official_access_blocked
            if material_steps_allowed and story["commons_query"] and int(settings["max_commons_assets"]) > 0 and len(assets) < int(settings["max_assets"]) - 2:
                state.update(status="running", phase="commons_asset")
                api_url = "https://commons.wikimedia.org/w/api.php?" + urlencode({
                    "action": "query", "generator": "search", "gsrsearch": story["commons_query"], "gsrnamespace": "6", "gsrlimit": "5",
                    "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1600", "format": "json", "formatversion": "2",
                })
                try:
                    _final_api, _api_mime, api_data = fetcher.get(api_url, maximum_bytes=int(settings["max_html_bytes"]), accepted_types=("application/json",))
                    candidates = _commons_candidates(json.loads(api_data.decode("utf-8")), settings)
                    if not candidates:
                        raise MaterialProbeError("Commons 未返回许可、作者和尺寸均合格的素材")
                    selected = candidates[0]
                    asset_url, content_type, image_data = fetcher.get(
                        selected["download_url"], maximum_bytes=int(settings["max_asset_bytes"]), accepted_types=("image/jpeg", "image/png", "image/webp"),
                    )
                    stored = _store_asset(
                        stage, assets, image_data, content_type, settings,
                        source_kind="wikimedia_commons", source_url=selected["source_url"], direct_asset_url=asset_url,
                        rights_status="renderable_with_attribution", author=selected["author"], license_name=selected["license_name"], license_url=selected["license_url"],
                        attribution_text=f"{selected['author']} / {selected['license_name']}",
                        usage_requirements=(
                            "必须署名、链接许可并注明修改；改编作品需采用相同或兼容许可"
                            if "by-sa" in selected["license_name"].casefold()
                            else "必须署名、链接许可并注明修改"
                        ),
                        relevance="与新闻人物 Bill Gates 直接相关的许可明确肖像",
                        suggested_role="人物介绍或新闻主体画面",
                    )
                    if stored is None:
                        failures.append({"source": "commons_asset", "error_type": "DuplicateAsset", "message": "与已保存素材重复，已跳过"})
                except (MaterialProbeError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    failures.append(_failure("commons_asset", exc))

            if material_steps_allowed:
                state.update(status="running", phase="generated_cards")
                for kind, role in (("title_card", "新闻标题开场卡"), ("source_card", "官方来源与摘要卡")):
                    data = _generated_card(story, kind=kind)
                    stored = _store_asset(
                        stage, assets, data, "image/png", settings,
                        source_kind=kind, source_url=official["url"], direct_asset_url="", rights_status="project_generated",
                        author="copy_skill 项目", license_name="项目生成素材", license_url="", attribution_text="",
                        usage_requirements="不得作为新闻现场实拍画面使用",
                        relevance="由已确认新闻标题、摘要和官方来源生成，不伪造新闻现场画面", suggested_role=role,
                    )
                    if stored is None:
                        failures.append({"source": kind, "error_type": "DuplicateAsset", "message": "生成卡片与已有素材近似，已跳过"})
        finally:
            fetcher.close()

        external_renderable = any(item["rights_status"] == "renderable_with_attribution" for item in assets)
        network_assets = sum(item["source_kind"] in {"official_article_image", "wikimedia_commons"} for item in assets)
        if official_reachable and len(assets) >= 3 and network_assets >= 1 and external_renderable:
            status = "success"
        elif (official_reachable or official_access_blocked) and assets:
            status = "partial"
        else:
            status = "failed"
        counts = {
            "assets": len(assets),
            "max_assets": int(settings["max_assets"]),
            "network_assets": network_assets,
            "renderable": sum(bool(item["renderable"]) for item in assets),
            "review_required": sum(item["rights_status"] == "review_required" for item in assets),
            "reference_only": sum(item["rights_status"] == "reference_only" for item in assets),
            "failures": len(failures),
            "llm_calls": 0,
            "ocr_calls": 0,
            "asr_calls": 0,
            "browser_calls": 0,
            "openmontage_writes": 0,
        }
        manifest = {
            "schema_version": "1.0",
            "status": status,
            "generated_at": now_iso(str(config["timezone"])),
            "story_id": story["story_id"],
            "confirmation": {
                "status": "official_source_reachable" if official_reachable else "official_source_access_blocked" if official_access_blocked else "official_source_unreachable",
                "input_gate": "official_primary_source",
                "fact_verification_by_model": False,
                "douyin_used_as_fact_source": False,
            },
            "assets": assets,
            "failures": failures,
            "counts": counts,
            "budget": {**budget.snapshot(), "max_asset_bytes": int(settings["max_asset_bytes"]), "request_timeout_seconds": int(settings["request_timeout_seconds"])},
            "boundaries": {"openmontage_modified": False, "arbitrary_network_video_downloaded": False, "rights_status_is_legal_advice": False},
        }
        _atomic_json(stage / "story.json", {**story, "run_budget": manifest["budget"]})
        _atomic_json(stage / "manifest.json", manifest)
        (stage / "preview.md").write_text(_preview_markdown(story, manifest), encoding="utf-8", newline="\n")
        (stage / "run.log").write_text(
            f"status={status}\nrequests={budget.request_count}\nbytes={budget.downloaded_bytes}\nassets={len(assets)}\nfailures={len(failures)}\n",
            encoding="utf-8", newline="\n",
        )
        _publish_directory(stage, destination)
        output_path = destination / "preview.md"
        state.update(status=status, phase="complete", output_path=str(output_path.resolve()), errors=failures, counts=counts)
        return {
            "status": status,
            "story_id": story["story_id"],
            "output_dir": str(destination.resolve()),
            "output_path": str(output_path.resolve()),
            "manifest_path": str((destination / "manifest.json").resolve()),
            "counts": counts,
            "budget": manifest["budget"],
        }
