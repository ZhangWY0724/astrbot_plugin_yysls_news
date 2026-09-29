from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

OPUS_HOSTS = {"bilibili.com", "www.bilibili.com"}


def opus_detail_url(dynamic_id: str, jump_url: Any = "") -> str:
    """解析 Opus 详情地址；接口地址不可用时构造规范路由。"""
    normalized_id = str(dynamic_id or "").strip()
    if not normalized_id.isdigit():
        return ""
    fallback = f"https://www.bilibili.com/opus/{normalized_id}"
    if not isinstance(jump_url, str) or not jump_url.strip():
        return fallback

    candidate = jump_url.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    if (
        parsed.scheme == "https"
        and parsed.hostname in OPUS_HOSTS
        and parsed.path.startswith("/opus/")
    ):
        return candidate
    return fallback


def source_url_for_task(task: dict[str, Any]) -> str:
    """为渲染和发送统一解析已入库任务的 B 站原文地址。"""
    source_url = str(task.get("source_url") or "")
    if str(task.get("source_type") or "") != "bilibili":
        return source_url

    category = str(task.get("category") or "")
    if category == "视频":
        return str(task.get("video_url") or source_url)
    dynamic_id = str(task.get("external_id") or "")
    if category == "图文" and dynamic_id.isascii() and dynamic_id.isdigit():
        try:
            hostname = urlsplit(source_url).hostname
        except ValueError:
            hostname = ""
        if hostname == "t.bilibili.com":
            return opus_detail_url(dynamic_id) or source_url
    return source_url
