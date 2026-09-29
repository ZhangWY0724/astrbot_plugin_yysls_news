from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from .bilibili_urls import opus_detail_url
from .models import BiliDynamic, Content

TYPE_NAMES = {
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "文字",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_FORWARD": "转发",
}
SUPPORTED_DYNAMIC_TYPES = frozenset(TYPE_NAMES)


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def extract_new_items(
    payload: Any,
    last_dynamic_id: str = "",
    recent_dynamic_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    known = {str(value) for value in recent_dynamic_ids if value}
    if last_dynamic_id:
        known.add(str(last_dynamic_id))
    result: list[dict[str, Any]] = []
    for item in extract_items(payload):
        dynamic_id = str(item.get("id_str") or item.get("id") or "")
        if not dynamic_id:
            continue
        if dynamic_id in known:
            break
        modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
        tag = modules.get("module_tag") if isinstance(modules, dict) else None
        if isinstance(tag, dict) and str(tag.get("text") or "") == "置顶":
            continue
        result.append(item)
    return result


def parse_dynamic(item: dict[str, Any], uid: int) -> BiliDynamic | None:
    dynamic_id = str(item.get("id_str") or item.get("id") or "")
    dynamic_type = str(item.get("type") or "")
    if not dynamic_id or dynamic_type not in TYPE_NAMES:
        return None
    modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
    author_module = modules.get("module_author") if isinstance(modules, dict) else {}
    dynamic_module = modules.get("module_dynamic") if isinstance(modules, dict) else {}
    author_module = author_module if isinstance(author_module, dict) else {}
    dynamic_module = dynamic_module if isinstance(dynamic_module, dict) else {}
    author_name = str(author_module.get("name") or "")
    timestamp = author_module.get("pub_ts")
    try:
        published_at = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else None
        )
    except (TypeError, ValueError, OverflowError, OSError):
        published_at = None

    desc = dynamic_module.get("desc")
    body = content_to_text(desc)
    major = dynamic_module.get("major")
    major = major if isinstance(major, dict) else {}
    title = ""
    cover_url = ""
    video_url = ""
    source_url = f"https://t.bilibili.com/{dynamic_id}"

    if dynamic_type == "DYNAMIC_TYPE_AV":
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        title = str(archive.get("title") or "")
        body = "\n".join(part for part in (body, str(archive.get("desc") or "")) if part)
        cover_url = _absolute_url(str(archive.get("cover") or ""))
        video_bvid = str(archive.get("bvid") or "").strip()
        if video_bvid:
            video_url = f"https://www.bilibili.com/video/{video_bvid}"
    elif dynamic_type in {
        "DYNAMIC_TYPE_DRAW",
        "DYNAMIC_TYPE_WORD",
        "DYNAMIC_TYPE_ARTICLE",
    }:
        opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
        article = major.get("article") if isinstance(major.get("article"), dict) else {}
        title = str(opus.get("title") or article.get("title") or "")
        summary = content_to_text(opus.get("summary"))
        if dynamic_type == "DYNAMIC_TYPE_ARTICLE":
            article_body = content_to_text(article.get("desc"))
            body = next((part for part in (summary, article_body, body) if part), "")
            source_url = opus_detail_url(dynamic_id, opus.get("jump_url")) or source_url
        elif not body:
            body = summary
        if dynamic_type in {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"} and isinstance(
            major.get("opus"), dict
        ):
            source_url = opus_detail_url(dynamic_id, opus.get("jump_url")) or source_url
    elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
        original = item.get("orig")
        if isinstance(original, dict):
            original_model = parse_dynamic(original, uid)
            if original_model:
                body = "\n".join(
                    part for part in (body, original_model.title, original_model.content) if part
                )

    if not title:
        title = body.splitlines()[0][:120] if body else f"{TYPE_NAMES[dynamic_type]}动态"
    return BiliDynamic(
        dynamic_id=dynamic_id,
        dynamic_type=dynamic_type,
        uid=uid,
        author_name=author_name,
        publish_time=published_at,
        title=title,
        content=body,
        source_url=source_url,
        video_cover_url=cover_url,
        video_url=video_url,
    )


def content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    if isinstance(text, dict):
        text = text.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    paragraphs = value.get("paragraphs")
    if isinstance(paragraphs, list):
        lines = [content_to_text(paragraph) for paragraph in paragraphs]
        result = "\n".join(line for line in lines if line)
        if result:
            return result
    nodes = value.get("rich_text_nodes")
    if isinstance(nodes, list):
        return "".join(
            str(node.get("text") or "") for node in nodes if isinstance(node, dict)
        ).strip()
    return ""


def matches_filters(
    model: BiliDynamic, excluded_types: Iterable[str], excluded_keywords: Iterable[str]
) -> bool:
    type_name = TYPE_NAMES.get(model.dynamic_type, model.dynamic_type)
    if any(value in {model.dynamic_type, type_name} for value in excluded_types):
        return False
    haystack = f"{model.title}\n{model.content}".casefold()
    return not any(keyword and keyword.casefold() in haystack for keyword in excluded_keywords)


def to_content(model: BiliDynamic) -> Content:
    return Content(
        source_type="bilibili",
        source_key=str(model.uid),
        external_id=model.dynamic_id,
        title=model.title,
        author=model.author_name,
        category=TYPE_NAMES.get(model.dynamic_type, "动态"),
        source_url=model.source_url,
        published_at=model.publish_time,
        body_text=model.content if model.dynamic_type == "DYNAMIC_TYPE_AV" else "",
        video_cover_url=model.video_cover_url,
        video_url=model.video_url,
    )


async def fetch_latest(uid: int, credential_data: dict[str, Any] | None) -> dict[str, Any]:
    try:
        from bilibili_api import Credential, user
    except ImportError as exc:
        raise RuntimeError("未安装 bilibili-api-python") from exc
    allowed = {
        key: value
        for key, value in (credential_data or {}).items()
        if key in {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
        and value not in (None, "")
    }
    credential = Credential(**allowed) if allowed else None
    instance = user.User(uid=int(uid), credential=credential)
    return await instance.get_dynamics_new()


def normalize_dynamic_id(value: Any) -> str:
    """验证动态 ID；也接受常见的 B 站动态详情链接。"""
    raw = str(value or "").strip()
    if raw.isdecimal() and len(raw) <= 24:
        return str(int(raw))

    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("请输入有效的 B 站动态 ID 或链接") from exc
    hostname = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    allowed_hosts = {"bilibili.com", "www.bilibili.com", "t.bilibili.com"}
    if (
        parsed.scheme in {"http", "https"}
        and hostname in allowed_hosts
        and path_parts
        and path_parts[-1].isdecimal()
        and len(path_parts[-1]) <= 24
    ):
        if hostname == "t.bilibili.com" and len(path_parts) == 1:
            return str(int(path_parts[0]))
        if hostname != "t.bilibili.com" and len(path_parts) == 2 and path_parts[0] in {
            "opus",
            "dynamic",
        }:
            return str(int(path_parts[-1]))
    raise ValueError("请输入有效的 B 站动态 ID 或链接")


async def fetch_dynamic(
    dynamic_id: str | int, credential_data: dict[str, Any] | None
) -> BiliDynamic:
    """按 ID 读取结构化动态详情，并转换为插件统一模型。"""
    normalized_id = normalize_dynamic_id(dynamic_id)
    try:
        from bilibili_api import Credential, dynamic
    except ImportError as exc:
        raise RuntimeError("未安装 bilibili-api-python") from exc

    allowed = {
        key: value
        for key, value in (credential_data or {}).items()
        if key in {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
        and value not in (None, "")
    }
    credential = Credential(**allowed) if allowed else None
    response = await dynamic.Dynamic(int(normalized_id), credential=credential).get_info()
    item = response.get("item") if isinstance(response, dict) else None
    if not isinstance(item, dict):
        raise ValueError("动态详情响应缺少 item 字段")

    normalized_item = dict(item)
    normalized_item["id_str"] = str(
        item.get("id_str") or item.get("id") or normalized_id
    )
    if normalized_item["id_str"] != normalized_id:
        raise ValueError("动态详情返回的 ID 与请求不一致")

    modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
    author = modules.get("module_author") if isinstance(modules, dict) else {}
    author = author if isinstance(author, dict) else {}
    basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
    try:
        uid = int(author.get("mid") or author.get("uid") or basic.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0

    dynamic_module = modules.get("module_dynamic") if isinstance(modules, dict) else {}
    dynamic_module = dynamic_module if isinstance(dynamic_module, dict) else {}
    major = dynamic_module.get("major") if isinstance(dynamic_module.get("major"), dict) else {}
    try:
        comment_type = int(basic.get("comment_type") or 0)
    except (TypeError, ValueError):
        comment_type = 0
    if str(normalized_item.get("type") or "") not in SUPPORTED_DYNAMIC_TYPES:
        major_type = str(major.get("type") or "")
        if item.get("orig"):
            normalized_item["type"] = "DYNAMIC_TYPE_FORWARD"
        elif comment_type == 12:
            normalized_item["type"] = "DYNAMIC_TYPE_ARTICLE"
        elif major_type == "MAJOR_TYPE_ARCHIVE":
            normalized_item["type"] = "DYNAMIC_TYPE_AV"
        elif major_type in {"MAJOR_TYPE_OPUS", "MAJOR_TYPE_DRAW"}:
            opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
            normalized_item["type"] = (
                "DYNAMIC_TYPE_DRAW" if opus.get("pics") else "DYNAMIC_TYPE_WORD"
            )
        elif major_type == "MAJOR_TYPE_ARTICLE":
            normalized_item["type"] = "DYNAMIC_TYPE_ARTICLE"
        elif not major:
            normalized_item["type"] = "DYNAMIC_TYPE_WORD"

    model = parse_dynamic(normalized_item, uid)
    if model is None:
        raise ValueError("该动态类型暂不支持推送")
    return model


def _absolute_url(value: str) -> str:
    if value.startswith("//"):
        candidate = f"https:{value}"
    elif value.startswith(("http://", "https://")):
        candidate = value
    else:
        candidate = urljoin("https://www.bilibili.com/", value) if value else ""
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").lower()
    allowed = ("bilibili.com", "hdslb.com")
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed):
        return ""
    return candidate
