from plugin_core.models import Content
from plugin_core.store import Store


def test_content_and_delivery_tasks_are_inserted_atomically(tmp_path) -> None:
    store = Store(tmp_path / "plugin.db")
    store.upsert_target("qq_official:GroupMessage:group-1", "测试群")
    store.upsert_target("qq_official:GroupMessage:group-2", "另一个群")
    content = Content(
        source_type="yysls",
        source_key="official",
        external_id="https://www.yysls.cn/news/2026/09/24/123.html",
        title="更新公告",
        author="燕云十六声官网",
        category="公告",
        source_url="https://www.yysls.cn/news/2026/09/24/123.html",
    )

    content_id, inserted, task_count = store.insert_content(content)
    duplicate_id, duplicate_inserted, duplicate_tasks = store.insert_content(content)

    assert content_id == duplicate_id
    assert inserted and not duplicate_inserted
    assert task_count == 2 and duplicate_tasks == 0
    tasks = store.list_pending_tasks()
    assert len(tasks) == 2
    assert {task["content_item_id"] for task in tasks} == {content_id}


def test_pending_task_query_can_be_scoped_to_one_content(tmp_path) -> None:
    store = Store(tmp_path / "scoped-tasks.db")
    store.upsert_target("platform:GroupMessage:group-1", "测试群")

    def content(external_id: str) -> Content:
        return Content(
            source_type="bilibili",
            source_key="42",
            external_id=external_id,
            title=f"动态 {external_id}",
            author="测试 UP",
            category="文字",
            source_url=f"https://t.bilibili.com/{external_id}",
        )

    store.insert_content(content("regular"))
    test_content_id, _inserted, _tasks = store.insert_content(content("manual-test"))

    tasks = store.list_pending_tasks(content_id=test_content_id)

    assert len(tasks) == 1
    assert tasks[0]["content_item_id"] == test_content_id
    assert store.content_delivery_counts(test_content_id) == {"pending": 1}
