import sys
from types import ModuleType, SimpleNamespace

import pytest

from plugin_core.bilibili import (
    extract_new_items,
    fetch_dynamic,
    matches_filters,
    normalize_dynamic_id,
    parse_dynamic,
    to_content,
)
from plugin_core.bilibili_account import fetch_self_username
from plugin_core.bilibili_urls import opus_detail_url, source_url_for_task


def _dynamic(dynamic_id: str, dynamic_type: str, text: str = "") -> dict:
    return {
        "id_str": dynamic_id,
        "type": dynamic_type,
        "modules": {
            "module_author": {"name": "测试UP", "pub_ts": 1720000000},
            "module_dynamic": {"desc": {"text": text}, "major": {}},
        },
    }


def test_extract_new_items_stops_at_cursor_and_skips_pins() -> None:
    items = [
        _dynamic("new-2", "DYNAMIC_TYPE_WORD", "第二条"),
        {
            **_dynamic("pinned", "DYNAMIC_TYPE_WORD", "置顶"),
            "modules": {
                **_dynamic("pinned", "DYNAMIC_TYPE_WORD")["modules"],
                "module_tag": {"text": "置顶"},
            },
        },
        _dynamic("new-1", "DYNAMIC_TYPE_WORD", "第一条"),
        _dynamic("old", "DYNAMIC_TYPE_WORD", "历史"),
    ]

    assert [item["id_str"] for item in extract_new_items({"items": items}, "old")] == [
        "new-2",
        "new-1",
    ]


def test_video_dynamic_uses_structured_archive_fields() -> None:
    item = _dynamic("123", "DYNAMIC_TYPE_AV", "动态文案")
    item["modules"]["module_dynamic"]["major"] = {
        "archive": {
            "title": "新视频",
            "desc": "投稿简介",
            "bvid": "BV1xx",
            "cover": "//i0.hdslb.com/bfs/cover.jpg",
            "jump_url": "https://www.bilibili.com/video/BV1xx",
        }
    }

    model = parse_dynamic(item, 42)
    assert model is not None
    assert model.title == "新视频"
    assert model.video_cover_url == "https://i0.hdslb.com/bfs/cover.jpg"
    assert model.video_url == "https://www.bilibili.com/video/BV1xx"
    assert model.source_url == "https://t.bilibili.com/123"
    assert to_content(model).category == "视频"


def test_video_without_bvid_retains_dynamic_route_as_fallback() -> None:
    item = _dynamic("123", "DYNAMIC_TYPE_AV", "动态文案")
    item["modules"]["module_dynamic"]["major"] = {
        "archive": {"title": "新视频", "jump_url": "https://www.bilibili.com/video/BV1xx"}
    }

    model = parse_dynamic(item, 42)

    assert model is not None
    assert model.video_url == ""
    assert model.source_url == "https://t.bilibili.com/123"


def test_opus_url_and_type_keyword_filters() -> None:
    item = _dynamic("42", "DYNAMIC_TYPE_DRAW", "")
    item["modules"]["module_dynamic"]["major"] = {
        "opus": {
            "jump_url": "//www.bilibili.com/opus/42",
            "title": "版本公告",
            "summary": {"text": "更新内容"},
        }
    }
    model = parse_dynamic(item, 42)
    assert model is not None
    assert model.source_url == "https://www.bilibili.com/opus/42"
    assert not matches_filters(model, [], ["公告"])
    assert not matches_filters(model, ["图文"], [])
    assert matches_filters(model, [], ["未出现"])
    assert parse_dynamic(_dynamic("live", "DYNAMIC_TYPE_LIVE_RCMD"), 42) is None


def test_word_and_forward_keep_their_outer_dynamic_ids_and_routes() -> None:
    word = parse_dynamic(_dynamic("43", "DYNAMIC_TYPE_WORD", "纯文字"), 42)
    forward_item = _dynamic("44", "DYNAMIC_TYPE_FORWARD", "转发评论")
    forward_item["orig"] = _dynamic("45", "DYNAMIC_TYPE_WORD", "被转发正文")
    forward = parse_dynamic(forward_item, 42)

    assert word is not None
    assert word.dynamic_id == "43"
    assert word.source_url == "https://t.bilibili.com/43"
    assert forward is not None
    assert forward.dynamic_id == "44"
    assert forward.source_url == "https://t.bilibili.com/44"
    assert "被转发正文" in forward.content


def test_article_opus_uses_structured_jump_url() -> None:
    item = _dynamic("1252945138288689153", "DYNAMIC_TYPE_ARTICLE", "")
    item["modules"]["module_dynamic"]["major"] = {
        "opus": {
            "jump_url": "//www.bilibili.com/opus/1252945138288689153",
            "title": "图文动态标题",
            "summary": {"text": "图文动态正文"},
        },
        "article": {"title": "旧结构标题", "desc": "旧结构正文"},
    }

    model = parse_dynamic(item, 42)

    assert model is not None
    assert model.title == "图文动态标题"
    assert model.content == "图文动态正文"
    assert model.source_url == "https://www.bilibili.com/opus/1252945138288689153"


def test_opus_without_jump_url_uses_canonical_route() -> None:
    item = _dynamic("1252945138288689153", "DYNAMIC_TYPE_DRAW", "")
    item["modules"]["module_dynamic"]["major"] = {
        "opus": {"title": "图文动态标题", "summary": {"text": "正文"}}
    }

    model = parse_dynamic(item, 42)

    assert model is not None
    assert model.source_url == "https://www.bilibili.com/opus/1252945138288689153"


def test_queued_opus_task_normalizes_old_t_route() -> None:
    task = {
        "source_type": "bilibili",
        "category": "图文",
        "external_id": "1252945138288689153",
        "source_url": "https://t.bilibili.com/1252945138288689153",
    }

    assert opus_detail_url(task["external_id"], task["source_url"]) == (
        "https://www.bilibili.com/opus/1252945138288689153"
    )
    assert source_url_for_task(task) == "https://www.bilibili.com/opus/1252945138288689153"


def test_legacy_text_and_article_routes_are_not_forced_to_opus() -> None:
    dynamic_id = "1252945138288689153"
    for category in ("文字", "专栏"):
        task = {
            "source_type": "bilibili",
            "category": category,
            "external_id": dynamic_id,
            "source_url": f"https://t.bilibili.com/{dynamic_id}",
        }
        assert source_url_for_task(task) == task["source_url"]


def test_normalize_dynamic_id_accepts_ids_and_bilibili_routes() -> None:
    dynamic_id = "1252945138288689153"

    assert normalize_dynamic_id(dynamic_id) == dynamic_id
    assert normalize_dynamic_id(f"https://t.bilibili.com/{dynamic_id}?from=share") == dynamic_id
    assert normalize_dynamic_id(f"www.bilibili.com/opus/{dynamic_id}") == dynamic_id
    with pytest.raises(ValueError):
        normalize_dynamic_id("https://example.com/opus/123")


async def test_fetch_dynamic_uses_authenticated_structured_detail(monkeypatch) -> None:
    captured = {}
    dynamic_id = "1252945138288689153"

    class FakeCredential:
        def __init__(self, **values):
            self.values = values

    class FakeDynamic:
        def __init__(self, requested_id, credential=None):
            captured["requested_id"] = requested_id
            captured["credential"] = credential

        async def get_info(self):
            return {
                "item": {
                    "id_str": dynamic_id,
                    "basic": {"comment_type": 2},
                    "modules": {
                        "module_author": {
                            "mid": 42,
                            "name": "测试 UP",
                            "pub_ts": 1720000000,
                        },
                        "module_dynamic": {
                            "desc": {"text": "正文"},
                            "major": {
                                "type": "MAJOR_TYPE_OPUS",
                                "opus": {
                                    "title": "手动测试动态",
                                    "summary": {"text": "详细正文"},
                                    "jump_url": f"//www.bilibili.com/opus/{dynamic_id}",
                                    "pics": [{"url": "https://i0.hdslb.com/cover.jpg"}],
                                },
                            },
                        },
                    },
                }
            }

    bilibili_api = ModuleType("bilibili_api")
    bilibili_api.Credential = FakeCredential
    bilibili_api.dynamic = SimpleNamespace(Dynamic=FakeDynamic)
    monkeypatch.setitem(sys.modules, "bilibili_api", bilibili_api)

    model = await fetch_dynamic(
        dynamic_id,
        {"sessdata": "session-secret", "bili_jct": "csrf-secret", "dedeuserid": "42"},
    )

    assert captured["requested_id"] == int(dynamic_id)
    assert captured["credential"].values == {
        "sessdata": "session-secret",
        "bili_jct": "csrf-secret",
        "dedeuserid": "42",
    }
    assert model.dynamic_id == dynamic_id
    assert model.uid == 42
    assert model.author_name == "测试 UP"
    assert model.title == "手动测试动态"
    assert model.source_url == f"https://www.bilibili.com/opus/{dynamic_id}"


async def test_fetch_self_username_uses_authenticated_profile(monkeypatch) -> None:
    captured = {}

    class FakeCredential:
        def __init__(self, **values):
            self.values = values

    async def fake_get_self_info(credential):
        captured["credential"] = credential
        return {"data": {"profile": {"uname": "当前登录用户"}}}

    bilibili_api = ModuleType("bilibili_api")
    bilibili_api.Credential = FakeCredential
    bilibili_api.user = SimpleNamespace(get_self_info=fake_get_self_info)
    monkeypatch.setitem(sys.modules, "bilibili_api", bilibili_api)

    username = await fetch_self_username(
        {
            "sessdata": "session-secret",
            "bili_jct": "csrf-secret",
            "dedeuserid": "42",
            "username": "旧名称字段不作为 Cookie",
        }
    )

    assert username == "当前登录用户"
    assert captured["credential"].values == {
        "sessdata": "session-secret",
        "bili_jct": "csrf-secret",
        "dedeuserid": "42",
    }
