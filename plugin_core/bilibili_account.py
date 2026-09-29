from __future__ import annotations

from typing import Any


async def fetch_self_username(credential_data: dict[str, Any]) -> str:
    """使用已登录凭据读取当前账号昵称。"""
    try:
        from bilibili_api import Credential, user
    except ImportError as exc:
        raise RuntimeError("未安装 bilibili-api-python") from exc
    allowed = {
        key: value
        for key, value in credential_data.items()
        if key in {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
        and value not in (None, "")
    }
    credential = Credential(**allowed)
    profile = await user.get_self_info(credential=credential)
    return extract_self_username(profile)


def extract_self_username(profile: Any) -> str:
    if not isinstance(profile, dict):
        return ""
    candidates = [profile]
    seen = {id(profile)}
    for candidate in candidates:
        for key in ("name", "uname", "username", "nickname"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
        for key in ("data", "info", "profile"):
            nested = candidate.get(key)
            if isinstance(nested, dict) and id(nested) not in seen:
                candidates.append(nested)
                seen.add(id(nested))
    return ""
