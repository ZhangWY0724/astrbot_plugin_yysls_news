from __future__ import annotations

from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import Content, NewsItem

DEFAULT_NEWS_URL = "https://www.yysls.cn/news/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def parse_listing(html: str, list_url: str, fallback_category: str = "最新") -> list[NewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[NewsItem] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url, _fragment = urldefrag(urljoin(list_url, str(anchor.get("href") or "").strip()))
        parsed_url = urlparse(url)
        hostname = (parsed_url.hostname or "").lower()
        if hostname != "yysls.cn" and not hostname.endswith(".yysls.cn"):
            continue
        if not _is_article_url(parsed_url.path):
            continue
        if url == urldefrag(list_url)[0] or url in seen:
            continue

        clone_soup = BeautifulSoup(str(anchor), "html.parser")
        clone = clone_soup.find("a")
        if clone is None:
            continue
        category = fallback_category
        category_node = clone.find(["span", "em", "i"])
        if category_node:
            category_text = category_node.get_text(" ", strip=True)
            if category_text and len(category_text) <= 12:
                category = category_text
            category_node.decompose()
        title = " ".join(clone.stripped_strings)
        if not title or title.isdecimal() or len(title) > 300:
            continue

        published_at = None
        time_node = anchor.find("time")
        date_text = str(time_node.get("datetime") or "") if time_node else ""
        if date_text:
            try:
                published_at = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            except ValueError:
                published_at = None
        result.append(NewsItem(title=title, url=url, category=category, published_at=published_at))
        seen.add(url)
    return result


def _is_article_url(path: str) -> bool:
    lowered = path.casefold()
    if not lowered.startswith("/news/") or not lowered.endswith(".html"):
        return False
    segments = lowered.strip("/").split("/")
    if any(segment in {"index", "page", "pages", "list"} for segment in segments[1:-1]):
        return False
    leaf = lowered.rsplit("/", 1)[-1]
    if leaf in {"index.html", "list.html", "news.html"}:
        return False
    if leaf.startswith("index_") or leaf.startswith("page_"):
        return False
    return True


class YyslsClient:
    def __init__(self, timeout_seconds: float = 20) -> None:
        self.timeout_seconds = max(5, timeout_seconds)

    async def discover(self, list_url: str, category: str = "最新") -> list[NewsItem]:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.yysls.cn/"},
        ) as client:
            response = await client.get(list_url)
            response.raise_for_status()
        return parse_listing(response.text, str(response.url), category)


def to_content(item: NewsItem) -> Content:
    return Content(
        source_type="yysls",
        source_key="official",
        external_id=item.url,
        title=item.title,
        author="燕云十六声官网",
        category=item.category,
        source_url=item.url,
        published_at=item.published_at,
    )
