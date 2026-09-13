from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class VideoRecord:
    video_id: str
    title: str
    account_id: str
    account_name: str
    share_url: str
    published_at: str | None
    play_count: int | None = None
    digg_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    collect_count: int | None = None
    category: str = "unclassified"
    source: str = "douyin_creator"
    source_keyword: str = ""
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    id_derived: bool = False
    raw_file: str = ""
    production_role: str = ""
    source_group_id: str = ""
    source_group_name: str = ""
    editorial_lane: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
