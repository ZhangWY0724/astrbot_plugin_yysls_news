from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .bilibili import (
    extract_new_items,
    fetch_latest,
    matches_filters,
    parse_dynamic,
)
from .bilibili import to_content as bili_to_content
from .credentials import BilibiliCredentialManager
from .models import PollResult
from .store import Store
from .yysls import YyslsClient
from .yysls import to_content as yysls_to_content

LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        store: Store,
        credentials: BilibiliCredentialManager,
        yysls_client: YyslsClient,
        get_config: callable,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.yysls_client = yysls_client
        self.get_config = get_config

    async def poll_once(self, force: bool = False) -> list[PollResult]:
        config = self.get_config()
        results: list[PollResult] = []
        subscriptions = self.store.list_subscriptions()
        if subscriptions:
            credential = await self.credentials.ensure_valid()
            if credential:
                credential_data = self.credentials.load()
                for subscription in subscriptions:
                    if force or _is_due(subscription.get("next_poll_at")):
                        results.append(await self._poll_bilibili(subscription, credential_data))
        if config.get("yysls_enabled", True):
            state = self.store.yysls_state()
            if force or _is_due(state.get("next_poll_at")):
                results.append(await self._poll_yysls(config, state))
        return results

    async def _poll_bilibili(
        self, subscription: dict[str, Any], credential_data: dict[str, Any] | None
    ) -> PollResult:
        uid = int(subscription["uid"])
        interval = max(int(subscription.get("poll_interval_seconds") or 300), 300)
        started = time.monotonic()
        try:
            payload = await fetch_latest(uid, credential_data)
            known_cursor = str(subscription.get("last_dynamic_id") or "")
            recent = list(subscription.get("recent_dynamic_ids") or [])
            items = extract_new_items(payload, known_cursor, recent)
            ids = [str(item.get("id_str") or item.get("id") or "") for item in items]
            is_bootstrap = not known_cursor and not recent
            inserted = 0
            task_count = 0
            if not is_bootstrap:
                excluded_types = subscription.get("filter_types") or []
                excluded_keywords = subscription.get("filter_keywords") or []
                for item in reversed(items):
                    model = parse_dynamic(item, uid)
                    if model is None or not matches_filters(
                        model, excluded_types, excluded_keywords
                    ):
                        continue
                    _content_id, was_inserted, created_tasks = self.store.insert_content(
                        bili_to_content(model)
                    )
                    inserted += int(was_inserted)
                    task_count += created_tasks
            previous = [str(value) for value in recent]
            merged = list(dict.fromkeys([*ids, *previous]))[:64]
            last_id = ids[0] if ids else known_cursor
            self.store.update_bili_cursor(uid, last_id, merged, _next_poll(interval))
            LOGGER.info(
                "B站轮询完成 uid=%s new=%s inserted=%s tasks=%s duration_ms=%s",
                uid,
                len(items),
                inserted,
                task_count,
                int((time.monotonic() - started) * 1000),
            )
            return PollResult(str(uid), len(items), inserted, task_count)
        except Exception as exc:
            error = f"{type(exc).__name__}: 采集失败"
            self.store.update_bili_error(uid, error, _next_poll(interval))
            LOGGER.warning("B站轮询失败 uid=%s error=%s", uid, type(exc).__name__)
            return PollResult(str(uid), error=error)

    async def _poll_yysls(self, config: dict[str, Any], state: dict[str, Any]) -> PollResult:
        interval = max(int(config.get("yysls_poll_interval_seconds", 600)), 600)
        list_url = str(config.get("yysls_url") or "https://www.yysls.cn/news/").strip()
        category = str(config.get("yysls_category") or "最新").strip() or "最新"
        started = time.monotonic()
        try:
            items = await self.yysls_client.discover(list_url, category)
            bootstrap = not bool(state.get("initialized"))
            discovered = 0
            inserted = 0
            task_count = 0
            for item in items[:50]:
                if self.store.content_exists("yysls", "official", item.url):
                    continue
                discovered += 1
                _content_id, was_inserted, created_tasks = self.store.insert_content(
                    yysls_to_content(item), create_tasks=not bootstrap
                )
                inserted += int(was_inserted)
                task_count += created_tasks
            self.store.update_yysls_state(_next_poll(interval))
            LOGGER.info(
                "官网轮询完成 discovered=%s inserted=%s tasks=%s duration_ms=%s",
                discovered,
                inserted,
                task_count,
                int((time.monotonic() - started) * 1000),
            )
            return PollResult("official", discovered, inserted, task_count)
        except Exception as exc:
            error = f"{type(exc).__name__}: 采集失败"
            self.store.update_yysls_state(_next_poll(interval), error)
            LOGGER.warning("官网轮询失败 error=%s", type(exc).__name__)
            return PollResult("official", error=error)


def config_values(config: Any) -> dict[str, Any]:
    uids: set[int] = set()
    for value in _parse_values(config.get("bilibili_uids")):
        if value.isdecimal() and int(value) > 0:
            uids.add(int(value))
    return {
        "enabled": _as_bool(config.get("enabled"), True),
        "admin_ids": _parse_values(config.get("admin_ids")),
        "bilibili_uids": sorted(uids),
        "bilibili_poll_interval_seconds": max(
            300, _as_int(config.get("bilibili_poll_interval_seconds"), 300)
        ),
        "bilibili_filter_types": _parse_values(config.get("bilibili_filter_types")),
        "bilibili_filter_keywords": _parse_values(config.get("bilibili_filter_keywords")),
        "yysls_enabled": _as_bool(config.get("yysls_enabled"), True),
        "yysls_url": _valid_http_url(str(config.get("yysls_url") or "https://www.yysls.cn/news/")),
        "yysls_category": str(config.get("yysls_category") or "最新"),
        "yysls_poll_interval_seconds": max(
            600, _as_int(config.get("yysls_poll_interval_seconds"), 600)
        ),
        "http_timeout_seconds": max(5, _as_int(config.get("http_timeout_seconds"), 20)),
    }


def _parse_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        source = [str(item) for item in value]
    else:
        source = str(value or "").replace(",", "\n").splitlines()
    result: list[str] = []
    for item in source:
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _valid_http_url(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and (
        hostname == "yysls.cn" or hostname.endswith(".yysls.cn")
    ):
        return value.strip()
    return "https://www.yysls.cn/news/"


def _is_due(value: Any) -> bool:
    if not value:
        return True
    try:
        due = datetime.fromisoformat(str(value))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _next_poll(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(60, int(seconds)))).isoformat()
