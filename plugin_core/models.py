from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BiliDynamic:
    dynamic_id: str
    dynamic_type: str
    uid: int
    author_name: str
    publish_time: datetime | None
    title: str
    content: str
    source_url: str
    video_cover_url: str = ""
    video_url: str = ""


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    category: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class Content:
    source_type: str
    source_key: str
    external_id: str
    title: str
    author: str
    category: str
    source_url: str
    published_at: datetime | None = None
    body_text: str = ""
    video_cover_url: str = ""
    video_url: str = ""


@dataclass(frozen=True)
class PollResult:
    source_key: str
    discovered: int = 0
    inserted: int = 0
    delivery_tasks: int = 0
    error: str = ""
