from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Content

RENDER_VERSION = 1
PROCESSING_LEASE_SECONDS = 600

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS bilibili_subscriptions (
    uid INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    filter_types_json TEXT NOT NULL DEFAULT '[]',
    filter_keywords_json TEXT NOT NULL DEFAULT '[]',
    last_dynamic_id TEXT NOT NULL DEFAULT '', recent_dynamic_ids_json TEXT NOT NULL DEFAULT '[]',
    next_poll_at TEXT, last_success_at TEXT, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS delivery_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, unified_msg_origin TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
    message_mode TEXT NOT NULL DEFAULT 'image', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
    source_key TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '', published_at TEXT,
    body_text TEXT NOT NULL DEFAULT '', video_cover_url TEXT NOT NULL DEFAULT '',
    video_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE(source_type, source_key, external_id)
);
CREATE TABLE IF NOT EXISTS delivery_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL REFERENCES content_items(id),
    delivery_target_id INTEGER NOT NULL REFERENCES delivery_targets(id),
    status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT, processing_started_at TEXT,
    message_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT,
    UNIQUE(content_item_id, delivery_target_id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_tasks_due
    ON delivery_tasks(status, next_retry_at, created_at);
CREATE TABLE IF NOT EXISTS render_artifacts (
    content_item_id INTEGER PRIMARY KEY REFERENCES content_items(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, status TEXT NOT NULL,
    pages_json TEXT NOT NULL DEFAULT '[]', last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER REFERENCES content_items(id),
    delivery_target_id INTEGER REFERENCES delivery_targets(id),
    title TEXT NOT NULL DEFAULT '', unified_msg_origin TEXT NOT NULL DEFAULT '',
    attempt_number INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'processing',
    message_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_history_created_at
    ON push_history(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS source_state (
    source_key TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT, next_poll_at TEXT, last_error TEXT NOT NULL DEFAULT ''
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """保存来源游标、资讯、AstrBot 目标、Outbox 和共享截图状态。"""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO source_state(source_key) VALUES ('yysls:official')"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            return default

    def sync_subscriptions(
        self,
        uids: list[int],
        interval_seconds: int,
        filter_types: list[str],
        filter_keywords: list[str],
    ) -> None:
        active = set(uids)
        interval = max(int(interval_seconds), 300)
        with self.connect() as connection:
            for row in connection.execute("SELECT uid FROM bilibili_subscriptions").fetchall():
                uid = int(row["uid"])
                if uid not in active:
                    connection.execute(
                        "UPDATE bilibili_subscriptions SET enabled=0 WHERE uid=?", (uid,)
                    )
            for uid in sorted(active):
                connection.execute(
                    """INSERT INTO bilibili_subscriptions(
                        uid, enabled, poll_interval_seconds, filter_types_json, filter_keywords_json
                    ) VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(uid) DO UPDATE SET enabled=1,
                        poll_interval_seconds=excluded.poll_interval_seconds,
                        filter_types_json=excluded.filter_types_json,
                        filter_keywords_json=excluded.filter_keywords_json""",
                    (uid, interval, self._dump(filter_types), self._dump(filter_keywords)),
                )

    def list_subscriptions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bilibili_subscriptions WHERE enabled=1 ORDER BY uid"
            ).fetchall()
        return [
            {
                **dict(row),
                "filter_types": self._load(row["filter_types_json"], []),
                "filter_keywords": self._load(row["filter_keywords_json"], []),
                "recent_dynamic_ids": self._load(row["recent_dynamic_ids_json"], []),
            }
            for row in rows
        ]

    def update_bili_cursor(
        self,
        uid: int,
        last_dynamic_id: str,
        recent_ids: list[str],
        next_poll_at: str,
        error: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE bilibili_subscriptions SET last_dynamic_id=?,
                    recent_dynamic_ids_json=?, next_poll_at=?,
                    last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                    last_error=? WHERE uid=?""",
                (
                    last_dynamic_id,
                    self._dump(recent_ids[:64]),
                    next_poll_at,
                    error,
                    utc_now(),
                    error[:500],
                    uid,
                ),
            )

    def update_bili_error(self, uid: int, error: str, next_poll_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE bilibili_subscriptions SET last_error=?, next_poll_at=? WHERE uid=?",
                (error[:500], next_poll_at, uid),
            )

    def upsert_target(
        self, unified_msg_origin: str, display_name: str = "", message_mode: str = "image"
    ) -> int:
        umo = unified_msg_origin.strip()
        if not umo or len(umo) > 1000:
            raise ValueError("无效的 AstrBot 会话标识")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO delivery_targets(
                    unified_msg_origin, display_name, message_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(unified_msg_origin) DO UPDATE SET
                    display_name=CASE WHEN excluded.display_name<>''
                        THEN excluded.display_name ELSE delivery_targets.display_name END,
                    message_mode=excluded.message_mode, enabled=1, deleted_at=NULL,
                    updated_at=excluded.updated_at""",
                (umo, display_name.strip(), message_mode, now, now),
            )
            row = connection.execute(
                "SELECT id FROM delivery_targets WHERE unified_msg_origin=?", (umo,)
            ).fetchone()
        return int(row["id"])

    def list_targets(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM delivery_targets WHERE deleted_at IS NULL"
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY id"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def delete_target(self, target_id: int) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id FROM delivery_targets WHERE id=? AND deleted_at IS NULL",
                    (target_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    """UPDATE delivery_tasks SET status='failed', last_error='目标已删除',
                       processing_started_at=NULL WHERE delivery_target_id=?
                       AND status IN ('pending','retry','processing')""",
                    (target_id,),
                )
                connection.execute(
                    "UPDATE delivery_targets SET enabled=0, deleted_at=?, updated_at=? WHERE id=?",
                    (now, now, target_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def content_exists(self, source_type: str, source_key: str, external_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM content_items WHERE source_type=? AND source_key=?
                   AND external_id=?""",
                (source_type, source_key, external_id),
            ).fetchone()
        return row is not None

    def insert_content(self, content: Content, create_tasks: bool = True) -> tuple[int, bool, int]:
        now = utc_now()
        published_at = content.published_at.isoformat() if content.published_at else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO content_items(
                        source_type,source_key,external_id,title,author,category,source_url,
                        published_at,body_text,video_cover_url,video_url,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        content.source_type,
                        content.source_key,
                        content.external_id,
                        content.title,
                        content.author,
                        content.category,
                        content.source_url,
                        published_at,
                        content.body_text,
                        content.video_cover_url,
                        content.video_url,
                        now,
                    ),
                )
                inserted = cursor.rowcount == 1
                row = connection.execute(
                    """SELECT id FROM content_items WHERE source_type=? AND source_key=?
                       AND external_id=?""",
                    (content.source_type, content.source_key, content.external_id),
                ).fetchone()
                content_id = int(row["id"])
                task_count = 0
                if inserted and create_tasks:
                    targets = connection.execute(
                        "SELECT id FROM delivery_targets WHERE enabled=1 AND deleted_at IS NULL"
                    ).fetchall()
                    for target in targets:
                        task_count += connection.execute(
                            """INSERT OR IGNORE INTO delivery_tasks(
                                content_item_id, delivery_target_id, created_at
                            ) VALUES (?, ?, ?)""",
                            (content_id, int(target["id"]), now),
                        ).rowcount
                connection.execute("COMMIT")
                return content_id, inserted, task_count
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def yysls_state(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_state WHERE source_key='yysls:official'"
            ).fetchone()
        return dict(row) if row else {"initialized": 0, "next_poll_at": None}

    def update_yysls_state(self, next_poll_at: str, error: str = "") -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE source_state SET
                    initialized=CASE WHEN ?='' THEN 1 ELSE initialized END,
                    last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                    next_poll_at=?, last_error=? WHERE source_key='yysls:official'""",
                (error, error, now, next_poll_at, error[:500]),
            )

    def list_pending_tasks(
        self, limit: int = 20, content_id: int | None = None
    ) -> list[dict[str, Any]]:
        now = utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=PROCESSING_LEASE_SECONDS)
        ).isoformat()
        content_filter = ""
        params: list[Any] = []
        if content_id is not None:
            content_filter = "content.id=? AND "
            params.append(int(content_id))
        params.extend((now, stale_before, max(1, min(int(limit), 100))))
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT task.*, content.source_type,content.source_key,content.external_id,
                    content.title,content.author,content.category,content.source_url,
                    content.body_text,content.video_cover_url,content.video_url,
                    target.unified_msg_origin,target.display_name AS target_display_name,
                    target.message_mode
                FROM delivery_tasks AS task
                JOIN content_items AS content ON content.id=task.content_item_id
                JOIN delivery_targets AS target ON target.id=task.delivery_target_id
                WHERE {content_filter}target.enabled=1 AND target.deleted_at IS NULL AND (
                    (task.status IN ('pending','retry') AND
                     (task.next_retry_at IS NULL OR task.next_retry_at<=?)) OR
                    (task.status='processing' AND
                     (task.processing_started_at IS NULL OR task.processing_started_at<=?))
                ) ORDER BY task.created_at LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def content_delivery_counts(self, content_id: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS total FROM delivery_tasks "
                "WHERE content_item_id=? GROUP BY status",
                (int(content_id),),
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def claim_task(self, task_id: int) -> bool:
        now = utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=PROCESSING_LEASE_SECONDS)
        ).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE delivery_tasks SET status='processing', processing_started_at=?
                   WHERE id=? AND (status IN ('pending','retry') OR
                   (status='processing' AND
                    (processing_started_at IS NULL OR processing_started_at<=?)))""",
                (now, task_id, stale_before),
            )
        return cursor.rowcount == 1

    def mark_task_sent(self, task_id: int, message_id: str = "", trace_id: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE delivery_tasks SET status='sent',message_id=?,trace_id=?,last_error='',
                   sent_at=?,processing_started_at=NULL WHERE id=?""",
                (message_id, trace_id, utc_now(), task_id),
            )

    def mark_task_retry(self, task_id: int, retry_count: int, error: str, next_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE delivery_tasks SET status='retry',retry_count=?,last_error=?,
                   next_retry_at=?,processing_started_at=NULL WHERE id=?""",
                (retry_count, error[:500], next_at, task_id),
            )

    def mark_task_failed(self, task_id: int, retry_count: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE delivery_tasks SET status='failed',retry_count=?,last_error=?,
                   processing_started_at=NULL WHERE id=?""",
                (retry_count, error[:500], task_id),
            )

    def create_history(self, task: dict[str, Any], attempt_number: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO push_history(content_item_id,delivery_target_id,title,
                   unified_msg_origin,attempt_number,created_at) VALUES (?,?,?,?,?,?)""",
                (
                    int(task["content_item_id"]),
                    int(task["delivery_target_id"]),
                    str(task.get("title") or ""),
                    str(task["unified_msg_origin"]),
                    max(1, attempt_number),
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def finish_history(
        self, history_id: int, status: str, message_id: str = "", error: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE push_history SET status=?,message_id=?,error=?,finished_at=?
                   WHERE id=?""",
                (status, message_id, error[:500], utc_now(), history_id),
            )

    def finish_delivery_batch(self, updates: list[dict[str, Any]]) -> None:
        """在同一事务中更新一条合并转发涉及的所有任务和历史记录。"""
        now = utc_now()
        allowed_statuses = {"sent", "retry", "failed"}
        if any(str(update.get("status")) not in allowed_statuses for update in updates):
            raise ValueError("无效的批量推送状态")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for update in updates:
                    task_id = int(update["task_id"])
                    history_id = int(update["history_id"])
                    status = str(update["status"])
                    message_id = str(update.get("message_id") or "")[:300]
                    error = str(update.get("error") or "")[:500]
                    retry_count = max(0, int(update.get("retry_count") or 0))
                    if status == "sent":
                        connection.execute(
                            """UPDATE delivery_tasks SET status='sent',message_id=?,trace_id='',
                               last_error='',sent_at=?,processing_started_at=NULL WHERE id=?""",
                            (message_id, now, task_id),
                        )
                    elif status == "retry":
                        connection.execute(
                            """UPDATE delivery_tasks SET status='retry',retry_count=?,last_error=?,
                               next_retry_at=?,processing_started_at=NULL WHERE id=?""",
                            (
                                retry_count,
                                error,
                                str(update["next_retry_at"]),
                                task_id,
                            ),
                        )
                    else:
                        connection.execute(
                            """UPDATE delivery_tasks SET status='failed',retry_count=?,last_error=?,
                               processing_started_at=NULL WHERE id=?""",
                            (retry_count, error, task_id),
                        )
                    connection.execute(
                        """UPDATE push_history SET status=?,message_id=?,error=?,finished_at=?
                           WHERE id=?""",
                        (
                            "sent" if status == "sent" else "failed",
                            message_id if status == "sent" else "",
                            "" if status == "sent" else error,
                            now,
                            history_id,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def recent_failures(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id,title,unified_msg_origin,attempt_number,error,created_at
                   FROM push_history WHERE status='failed'
                   ORDER BY created_at DESC,id DESC LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS total FROM delivery_tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def claim_artifact(self, content_id: int, lease_seconds: int = 600) -> dict[str, Any]:
        now = utc_now()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM render_artifacts WHERE content_item_id=?", (content_id,)
                ).fetchone()
                if row and int(row["version"]) == RENDER_VERSION:
                    state = str(row["status"])
                    if state == "ready":
                        result = {**dict(row), "pages": self._load(row["pages_json"], [])}
                        connection.execute("COMMIT")
                        return result
                    if state == "failed":
                        connection.execute("COMMIT")
                        return {**dict(row), "pages": []}
                    if state == "claimed" and str(row["updated_at"]) > stale_before:
                        connection.execute("COMMIT")
                        return {"status": "waiting", "pages": []}
                connection.execute(
                    """INSERT INTO render_artifacts(content_item_id,version,status,updated_at)
                       VALUES(?,?,'claimed',?) ON CONFLICT(content_item_id) DO UPDATE SET
                       version=excluded.version,status='claimed',pages_json='[]',last_error='',
                       updated_at=excluded.updated_at""",
                    (content_id, RENDER_VERSION, now),
                )
                connection.execute("COMMIT")
                return {"status": "claimed", "pages": []}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def mark_artifact_ready(self, content_id: int, pages: list[str]) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE render_artifacts SET status='ready',pages_json=?,last_error='',
                   updated_at=? WHERE content_item_id=? AND version=?""",
                (self._dump(pages), utc_now(), content_id, RENDER_VERSION),
            )

    def mark_artifact_failed(self, content_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE render_artifacts SET status='failed',pages_json='[]',last_error=?,
                   updated_at=? WHERE content_item_id=? AND version=?""",
                (error[:500], utc_now(), content_id, RENDER_VERSION),
            )

    def invalidate_artifact(self, content_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM render_artifacts WHERE content_item_id=?", (content_id,)
            )

    def cleanup_artifact_if_done(self, content_id: int) -> list[str]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    """SELECT 1 FROM delivery_tasks WHERE content_item_id=?
                       AND status IN ('pending','retry','processing') LIMIT 1""",
                    (content_id,),
                ).fetchone()
                if active:
                    connection.execute("COMMIT")
                    return []
                row = connection.execute(
                    "SELECT pages_json FROM render_artifacts WHERE content_item_id=?",
                    (content_id,),
                ).fetchone()
                names = self._load(row["pages_json"], []) if row else []
                connection.execute(
                    "DELETE FROM render_artifacts WHERE content_item_id=?", (content_id,)
                )
                connection.execute("COMMIT")
                return [str(name) for name in names]
            except Exception:
                connection.execute("ROLLBACK")
                raise
