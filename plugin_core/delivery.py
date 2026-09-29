from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .renderer import ImageRenderError, PlaywrightRenderer
from .store import Store

LOGGER = logging.getLogger(__name__)
MessageSender = Callable[[str, list[dict[str, Any]]], Awaitable[Any]]
ProgressSender = Callable[[str, str], Awaitable[Any]]
RETRY_DELAYS = (60, 300, 1800, 7200)


class OutboxWorker:
    """渲染一次后共享图片，并通过 AstrBot 目标会话发送。"""

    def __init__(
        self,
        store: Store,
        renderer: PlaywrightRenderer,
        sender: MessageSender,
        output_dir: str | Path,
        max_attempts: int = 5,
        progress_sender: ProgressSender | None = None,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self.sender = sender
        self.output_dir = Path(output_dir)
        self.max_attempts = max(1, int(max_attempts))
        self.progress_sender = progress_sender

    async def process_pending(self, limit: int = 20, content_id: int | None = None) -> int:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if content_id is None:
            tasks = self.store.list_pending_tasks(limit)
        else:
            tasks = self.store.list_pending_tasks(limit, content_id=int(content_id))
        grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for task in tasks:
            target_id = int(task["delivery_target_id"])
            message_mode = (
                "text"
                if str(task.get("message_mode") or "image").strip().lower() == "text"
                else "image"
            )
            grouped.setdefault((target_id, message_mode), []).append(task)

        processed = 0
        for (target_id, _message_mode), target_tasks in grouped.items():
            claimed: list[dict[str, Any]] = []
            for task in target_tasks:
                task_id = int(task["id"])
                if not self.store.claim_task(task_id):
                    continue
                attempt = int(task.get("retry_count") or 0) + 1
                history_id = self.store.create_history(task, attempt)
                claimed.append(
                    {"task": task, "attempt": attempt, "history_id": history_id}
                )
            if not claimed:
                continue
            processed += len(claimed)
            target = str(claimed[0]["task"]["unified_msg_origin"])
            fresh = [entry for entry in claimed if int(entry["attempt"]) == 1]
            LOGGER.info(
                "AstrBot Outbox 批次推送开始: target=%s mode=%s items=%s new_items=%s",
                target_id,
                _message_mode,
                len(claimed),
                len(fresh),
            )

            if fresh and self.progress_sender:
                try:
                    notice = _progress_message(
                        [entry["task"] for entry in fresh], _message_mode
                    )
                    result = await self.progress_sender(target, notice)
                    if result is False:
                        raise RuntimeError("AstrBot 未找到匹配的推送目标")
                    LOGGER.info(
                        "AstrBot Outbox 准备提示已发送: target=%s items=%s",
                        target_id,
                        len(fresh),
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "AstrBot Outbox 准备提示发送失败: target=%s error=%s",
                        target_id,
                        type(exc).__name__,
                    )

            prepared: list[dict[str, Any]] = []
            for entry in claimed:
                task = entry["task"]
                pages: list[Path] | None = None
                if str(task.get("message_mode") or "image") == "image":
                    try:
                        pages = await self._get_or_render(task)
                    except Exception as exc:
                        reason = (
                            str(exc).replace("\n", " ")[:160]
                            if isinstance(exc, ImageRenderError)
                            else type(exc).__name__
                        )
                        LOGGER.warning(
                            "截图准备失败，转发原文节点: content=%s error=%s reason=%s",
                            task.get("content_item_id"),
                            type(exc).__name__,
                            reason,
                        )
                prepared.append({"task": task, "pages": pages})

            try:
                try:
                    result = await self.sender(target, prepared)
                except Exception as image_error:
                    if not any(item["pages"] for item in prepared):
                        raise
                    LOGGER.warning(
                        "合并转发图片发送失败，改发原文节点: target=%s error=%s",
                        target_id,
                        type(image_error).__name__,
                    )
                    text_items = [
                        {"task": item["task"], "pages": None} for item in prepared
                    ]
                    result = await self.sender(target, text_items)
                if result is False:
                    raise RuntimeError("AstrBot 未找到匹配的推送目标")
                message_id = _message_id(result)
                self.store.finish_delivery_batch(
                    [
                        {
                            "task_id": int(entry["task"]["id"]),
                            "history_id": int(entry["history_id"]),
                            "status": "sent",
                            "message_id": message_id,
                        }
                        for entry in claimed
                    ]
                )
                LOGGER.info(
                    "AstrBot Outbox 批次推送成功: target=%s mode=%s items=%s message_id=%s",
                    target_id,
                    _message_mode,
                    len(claimed),
                    message_id or "-",
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: 推送失败"
                updates = []
                retry_items = 0
                failed_items = 0
                for entry in claimed:
                    attempt = int(entry["attempt"])
                    status = "retry" if attempt < self.max_attempts else "failed"
                    retry_items += int(status == "retry")
                    failed_items += int(status == "failed")
                    update = {
                        "task_id": int(entry["task"]["id"]),
                        "history_id": int(entry["history_id"]),
                        "status": status,
                        "retry_count": attempt,
                        "error": error,
                    }
                    if status == "retry":
                        update["next_retry_at"] = _next_retry(attempt)
                    updates.append(update)
                self.store.finish_delivery_batch(updates)
                LOGGER.warning(
                    "AstrBot Outbox 批次推送失败: target=%s mode=%s items=%s retry=%s "
                    "failed=%s error=%s",
                    target_id,
                    _message_mode,
                    len(claimed),
                    retry_items,
                    failed_items,
                    type(exc).__name__,
                )
            finally:
                for entry in claimed:
                    self._cleanup_if_finished(int(entry["task"]["content_item_id"]))
        return processed

    async def process_content(self, content_id: int, limit: int = 100) -> int:
        """只处理指定内容的待推送任务，不触碰其他 Outbox 项。"""
        return await self.process_pending(limit=limit, content_id=content_id)

    async def _get_or_render(self, task: dict[str, Any]) -> list[Path]:
        content_id = int(task["content_item_id"])
        output = self.output_dir / f"content-{content_id}.png"
        for _ in range(1200):
            artifact = self.store.claim_artifact(content_id)
            status = str(artifact.get("status") or "")
            if status == "ready":
                pages = [_safe_page(self.output_dir, name) for name in artifact.get("pages", [])]
                if pages and all(path.is_file() and path.stat().st_size > 0 for path in pages):
                    return pages
                self.store.invalidate_artifact(content_id)
                continue
            if status == "failed":
                raise ImageRenderError("该内容图片渲染失败")
            if status == "claimed":
                self._cleanup_render_files(output)
                try:
                    pages = await self.renderer.render(task, output)
                    if not pages:
                        raise ImageRenderError("渲染器未生成图片")
                    for path in pages:
                        if path.parent.resolve() != self.output_dir.resolve():
                            raise ImageRenderError("渲染器返回了非输出目录文件")
                    self.store.mark_artifact_ready(content_id, [page.name for page in pages])
                    return pages
                except Exception as exc:
                    self._cleanup_render_files(output)
                    self.store.mark_artifact_failed(content_id, type(exc).__name__)
                    raise
            await asyncio.sleep(0.5)
        raise ImageRenderError("等待共享截图超时")

    def _cleanup_if_finished(self, content_id: int) -> None:
        try:
            names = self.store.cleanup_artifact_if_done(content_id)
        except Exception as exc:
            LOGGER.warning("共享截图状态检查失败: %s", type(exc).__name__)
            return
        for name in names:
            try:
                _safe_page(self.output_dir, name).unlink(missing_ok=True)
            except (OSError, ValueError):
                LOGGER.warning("共享截图清理失败: file=%s", Path(name).name)

    @staticmethod
    def _cleanup_render_files(output: Path) -> None:
        candidates = [output]
        for suffix in (".png", ".jpg", ".jpeg"):
            base = output.with_suffix(suffix)
            candidates.append(base)
            candidates.extend(base.parent.glob(f"{base.stem}-part-*{suffix}"))
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("临时截图清理失败: file=%s", path.name)


def _progress_message(tasks: list[dict[str, Any]], message_mode: str = "image") -> str:
    authors = {
        str(task.get("author") or "").strip()
        for task in tasks
        if task.get("source_type") == "bilibili" and str(task.get("author") or "").strip()
    }
    if message_mode == "text":
        if len(authors) == 1 and all(task.get("source_type") == "bilibili" for task in tasks):
            return f"{next(iter(authors))}有新动态了，正在整理消息内容…"
        return f"发现 {len(tasks)} 条新资讯，正在整理消息内容…"
    if len(authors) == 1 and all(task.get("source_type") == "bilibili" for task in tasks):
        return f"{next(iter(authors))}有新动态了，正在获取并整理转发内容…"
    return f"发现 {len(tasks)} 条新资讯，正在获取并整理合并转发…"


def _safe_page(output_dir: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise ValueError("无效的渲染文件名")
    path = output_dir / filename
    if path.parent.resolve() != output_dir.resolve():
        raise ValueError("渲染路径超出输出目录")
    return path


def _next_retry(attempt: int) -> str:
    delay = RETRY_DELAYS[min(max(attempt - 1, 0), len(RETRY_DELAYS) - 1)]
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _message_id(result: Any) -> str:
    if isinstance(result, dict):
        value = result.get("message_id") or result.get("id")
    else:
        value = getattr(result, "message_id", "") or getattr(result, "id", "")
    return str(value or "")[:300]
