import logging
from pathlib import Path

from plugin_core.delivery import OutboxWorker
from plugin_core.models import Content
from plugin_core.store import Store


def _content(external_id: str = "123") -> Content:
    return Content(
        source_type="bilibili",
        source_key="42",
        external_id=external_id,
        title=f"测试动态 {external_id}",
        author="测试 UP",
        category="图文",
        source_url="https://www.bilibili.com/opus/123",
    )


async def test_one_render_is_shared_between_targets_and_cleaned(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="plugin_core.delivery")
    store = Store(tmp_path / "outbox.db")
    store.upsert_target("platform:GroupMessage:a")
    store.upsert_target("platform:GroupMessage:b")
    content_id, inserted, task_count = store.insert_content(_content())
    assert inserted and task_count == 2

    class FakeRenderer:
        calls = 0

        async def render(self, task: dict, output: Path) -> list[Path]:
            self.calls += 1
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"image")
            return [output]

    sent: list[tuple[str, list[dict]]] = []

    async def send(umo: str, items: list[dict]):
        sent.append((umo, items))
        return None

    renderer = FakeRenderer()
    worker = OutboxWorker(store, renderer, send, tmp_path / "rendered")
    assert await worker.process_pending(limit=1) == 1
    assert renderer.calls == 1
    assert await worker.process_pending(limit=1) == 1
    assert renderer.calls == 1
    assert [umo for umo, _items in sent] == ["platform:GroupMessage:a", "platform:GroupMessage:b"]
    assert all(item["pages"] for _umo, items in sent for item in items)
    assert not list((tmp_path / "rendered").iterdir())
    assert store.status_counts()["sent"] == 2
    assert "AstrBot Outbox 批次推送开始" in caplog.text
    assert "AstrBot Outbox 批次推送成功" in caplog.text


async def test_image_send_failure_falls_back_to_text(tmp_path) -> None:
    store = Store(tmp_path / "fallback.db")
    store.upsert_target("platform:GroupMessage:a")
    store.insert_content(_content())

    class FakeRenderer:
        async def render(self, task: dict, output: Path) -> list[Path]:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"image")
            return [output]

    sent: list[list[dict]] = []

    async def send(_umo: str, items: list[dict]):
        if any(item["pages"] for item in items):
            raise RuntimeError("image send failed")
        sent.append(items)
        return None

    worker = OutboxWorker(store, FakeRenderer(), send, tmp_path / "rendered")
    assert await worker.process_pending() == 1
    assert len(sent) == 1
    assert sent[0][0]["pages"] is None
    assert store.status_counts()["sent"] == 1


async def test_same_target_items_send_one_forward_after_progress_notice(tmp_path) -> None:
    store = Store(tmp_path / "forward-batch.db")
    store.upsert_target("platform:GroupMessage:onebot-group")
    store.insert_content(_content("dynamic-1"))
    store.insert_content(_content("dynamic-2"))
    events: list[tuple[str, object]] = []

    class FakeRenderer:
        async def render(self, task: dict, output: Path) -> list[Path]:
            events.append(("render", task["external_id"]))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(str(task["external_id"]).encode())
            return [output]

    async def progress(umo: str, message: str):
        events.append(("progress", (umo, message)))
        return True

    async def send(umo: str, items: list[dict]):
        events.append(("forward", (umo, items)))
        return True

    worker = OutboxWorker(
        store,
        FakeRenderer(),
        send,
        tmp_path / "rendered",
        progress_sender=progress,
    )

    assert await worker.process_pending() == 2
    assert [event[0] for event in events] == ["progress", "render", "render", "forward"]
    notice = events[0][1][1]
    assert "测试 UP有新动态了" in notice
    batch_umo, batch_items = events[-1][1]
    assert batch_umo == "platform:GroupMessage:onebot-group"
    assert len(batch_items) == 2
    assert all(item["pages"] for item in batch_items)
    assert store.status_counts()["sent"] == 2


async def test_batch_send_failure_retries_every_item(tmp_path) -> None:
    store = Store(tmp_path / "forward-retry.db")
    store.upsert_target("platform:GroupMessage:onebot-group", message_mode="text")
    store.insert_content(_content("dynamic-1"))
    store.insert_content(_content("dynamic-2"))

    async def send(_umo: str, _items: list[dict]):
        raise RuntimeError("forward send failed")

    worker = OutboxWorker(
        store,
        renderer=None,
        sender=send,
        output_dir=tmp_path / "rendered",
        max_attempts=1,
    )

    assert await worker.process_pending() == 2
    assert store.status_counts()["failed"] == 2
    assert len(store.recent_failures()) == 2


async def test_message_modes_are_processed_as_separate_batches(tmp_path) -> None:
    store = Store(tmp_path / "message-modes.db")
    store.upsert_target("platform:GroupMessage:onebot-group")
    store.insert_content(_content("image-mode"))
    store.insert_content(_content("text-mode"))
    list_pending = store.list_pending_tasks

    def mixed_modes(limit: int = 20) -> list[dict]:
        tasks = list_pending(limit)
        tasks[0]["message_mode"] = "image"
        tasks[1]["message_mode"] = "text"
        return tasks

    store.list_pending_tasks = mixed_modes  # type: ignore[method-assign]

    sent: list[list[dict]] = []

    class FakeRenderer:
        async def render(self, _task: dict, output: Path) -> list[Path]:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"image")
            return [output]

    async def send(_umo: str, items: list[dict]):
        sent.append(items)
        return True

    worker = OutboxWorker(
        store,
        renderer=FakeRenderer(),
        sender=send,
        output_dir=tmp_path / "rendered",
    )

    assert await worker.process_pending() == 2
    assert len(sent) == 2
    assert {item["task"]["message_mode"] for batch in sent for item in batch} == {"image", "text"}
    assert any(sent_item["pages"] for batch in sent for sent_item in batch)
    assert any(
        sent_item["pages"] is None
        for batch in sent
        for sent_item in batch
        if sent_item["task"]["message_mode"] == "text"
    )
    assert store.status_counts()["sent"] == 2


async def test_process_content_does_not_flush_other_pending_tasks(tmp_path) -> None:
    store = Store(tmp_path / "content-scope.db")
    store.upsert_target("platform:GroupMessage:onebot-group")
    store.insert_content(_content("regular-dynamic"))
    manual_content_id, inserted, tasks = store.insert_content(_content("manual-test-dynamic"))
    assert inserted and tasks == 1

    class FakeRenderer:
        async def render(self, _task: dict, output: Path) -> list[Path]:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"image")
            return [output]

    sent_ids: list[str] = []

    async def send(_umo: str, items: list[dict]):
        sent_ids.extend(str(item["task"]["external_id"]) for item in items)
        return True

    worker = OutboxWorker(store, FakeRenderer(), send, tmp_path / "rendered")

    assert await worker.process_content(manual_content_id) == 1
    assert sent_ids == ["manual-test-dynamic"]
    assert store.status_counts() == {"pending": 1, "sent": 1}
