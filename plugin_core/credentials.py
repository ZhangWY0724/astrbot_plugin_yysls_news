from __future__ import annotations

import base64
import io
import json
import os
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
LOGIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
CROSS_DOMAIN_HOSTS = {"passport.biligame.com", "passport.bilibili.com"}


class BilibiliLoginError(RuntimeError):
    """B 站登录或二维码轮询失败。"""


@dataclass(frozen=True)
class QrCodeInfo:
    session_id: str
    url: str
    image_data_url: str


@dataclass(frozen=True)
class LoginPollResult:
    code: int
    message: str
    credential: dict[str, Any] | None = None

    @property
    def is_done(self) -> bool:
        return self.code == 0 and self.credential is not None

    @property
    def is_expired(self) -> bool:
        return self.code == 86038


class BilibiliQrLoginClient:
    """短期二维码会话；每次登录都由管理页显式发起。"""

    def __init__(self, timeout_seconds: float = 15) -> None:
        self.timeout_seconds = max(5, timeout_seconds)
        self._sessions: dict[str, dict[str, str]] = {}

    async def generate(self) -> QrCodeInfo:
        try:
            async with httpx.AsyncClient(
                headers=LOGIN_HEADERS, timeout=self.timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(QR_GENERATE_URL)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliLoginError("请求 B 站二维码失败") from exc
        if payload.get("code") != 0:
            raise BilibiliLoginError(str(payload.get("message") or "生成二维码失败"))
        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        qrcode_key = str(data.get("qrcode_key") or "")
        if not url or not qrcode_key:
            raise BilibiliLoginError("二维码响应缺少必要字段")
        session_id = os.urandom(16).hex()
        self._sessions[session_id] = {"qrcode_key": qrcode_key, "url": url}
        return QrCodeInfo(session_id, url, _qrcode_data_url(url))

    async def poll(self, session_id: str) -> LoginPollResult:
        session = self._sessions.get(session_id)
        if not session:
            raise BilibiliLoginError("二维码会话不存在或已过期")
        try:
            async with httpx.AsyncClient(
                headers=LOGIN_HEADERS, timeout=self.timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(
                    QR_POLL_URL, params={"qrcode_key": session["qrcode_key"]}
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise BilibiliLoginError("B 站扫码状态接口返回错误")
                data = payload.get("data") or {}
                code = int(data.get("code", -1))
                message = str(data.get("message") or "")
                if code != 0:
                    return LoginPollResult(code, message)
                credential = await _extract_credential(client, data)
        except BilibiliLoginError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise BilibiliLoginError("请求 B 站扫码状态失败") from exc
        if not _credential_complete(credential):
            raise BilibiliLoginError("扫码已确认，但响应未包含完整登录凭据")
        self._sessions.pop(session_id, None)
        return LoginPollResult(0, message, credential)

    def discard(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class BilibiliCredentialManager:
    """单账号 B 站凭据生命周期；Cookie 在磁盘上始终为 Fernet 密文。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._key_path = self.data_dir / "credential.key"
        self._credential_path = self.data_dir / "credential.enc"
        self._fernet = Fernet(_load_or_create_key(self._key_path))
        self._last_check = 0.0
        self._status = "not_configured"
        self._last_error = ""
        self._username = ""

    def load(self) -> dict[str, Any] | None:
        if not self._credential_path.is_file():
            return None
        try:
            token = self._credential_path.read_bytes()
            data = json.loads(self._fernet.decrypt(token).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("credential payload must be an object")
            return data
        except (OSError, InvalidToken, ValueError, UnicodeError, json.JSONDecodeError):
            self._status = "failed"
            self._last_error = "凭据无法解密，请检查 credential.key"
            return None

    def save(self, credential: dict[str, Any]) -> None:
        if not _credential_complete(credential):
            raise ValueError("SESSDATA、bili_jct 和 DedeUserID 均不能为空")
        payload = json.dumps(credential, ensure_ascii=False, separators=(",", ":")).encode()
        encrypted = self._fernet.encrypt(payload)
        temp_path = self._credential_path.with_suffix(".tmp")
        temp_path.write_bytes(encrypted)
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(self._credential_path)
        self._username = str(credential.get("username") or "")
        self._status = "logged_in"
        self._last_error = ""

    def clear(self) -> None:
        self._credential_path.unlink(missing_ok=True)
        self._status = "not_configured"
        self._last_error = ""
        self._username = ""

    def status(self) -> dict[str, str]:
        data = self.load()
        if not data:
            if self._status != "failed":
                self._status = "not_configured"
            return {
                "status": self._status,
                "username": self._username,
                "last_error": self._last_error,
            }
        self._username = str(data.get("username") or self._username)
        if self._status in {"not_configured", "failed"}:
            self._status = "logged_in"
        return {"status": self._status, "username": self._username, "last_error": self._last_error}

    async def ensure_valid(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_check < 900:
            return self._status == "logged_in"
        self._last_check = now
        data = self.load()
        if not data:
            return False
        try:
            from bilibili_api import Credential

            credential = Credential(**_allowed_credential(data))
            if not await credential.check_valid():
                self._status = "relogin_required"
                self._last_error = "B 站登录态失效，请在插件管理页重新扫码"
                return False
            if await credential.check_refresh():
                await credential.refresh()
                refreshed = _credential_to_dict(credential)
                refreshed["username"] = self._username
                self.save(refreshed)
            self._status = "logged_in"
            self._last_error = ""
            return True
        except Exception as exc:
            # 登录检查错误进入 15 分钟低频窗口，不能自动循环刷新二维码。
            self._status = "failed"
            self._last_error = f"登录态检查失败: {type(exc).__name__}"
            return False


def _load_or_create_key(path: Path) -> bytes:
    if path.is_file():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _credential_complete(value: dict[str, Any] | None) -> bool:
    required = ("sessdata", "bili_jct", "dedeuserid")
    return bool(value) and all(str(value.get(key) or "") for key in required)


def _allowed_credential(data: dict[str, Any]) -> dict[str, Any]:
    keys = {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
    return {key: value for key, value in data.items() if key in keys and value not in (None, "")}


def _credential_to_dict(credential: Any) -> dict[str, Any]:
    return {
        key: getattr(credential, key, "")
        for key in ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")
    }


def _qrcode_data_url(url: str) -> str:
    try:
        import qrcode
    except ImportError as exc:
        raise BilibiliLoginError("未安装 qrcode，无法生成二维码") from exc
    image = qrcode.make(url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def _extract_credential(client: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
    login_url = str(data.get("url") or "")
    refresh_token = str(data.get("refresh_token") or "")
    parsed = urlparse(login_url)
    cookies: dict[str, str] = {}
    if parsed.hostname in CROSS_DOMAIN_HOSTS and parsed.path.endswith("/crossDomain"):
        response = await client.get(login_url)
        cookies = _select_cookie_values(client, response.headers.get_list("set-cookie"))
    else:
        query = parse_qs(parsed.query)
        cookies = {
            "SESSDATA": query.get("SESSDATA", [""])[0],
            "bili_jct": query.get("bili_jct", [""])[0],
            "buvid3": query.get("buvid3", [""])[0],
            "buvid4": query.get("buvid4", [""])[0],
            "DedeUserID": query.get("DedeUserID", [""])[0],
        }
    return {
        "sessdata": cookies.get("SESSDATA", ""),
        "bili_jct": cookies.get("bili_jct", ""),
        "buvid3": cookies.get("buvid3", ""),
        "buvid4": cookies.get("buvid4", ""),
        "dedeuserid": cookies.get("DedeUserID", ""),
        "ac_time_value": refresh_token,
    }


def _select_cookie_values(
    client: httpx.AsyncClient, set_cookie_headers: list[str]
) -> dict[str, str]:
    candidates: list[tuple[str, str, str, str]] = []
    for cookie in client.cookies.jar:
        candidates.append((cookie.name, cookie.value, cookie.domain, cookie.path))
    for header in set_cookie_headers:
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            candidates.append(
                (
                    name,
                    morsel.value,
                    str(morsel["domain"] or ""),
                    str(morsel["path"] or "/"),
                )
            )
    selected: dict[str, str] = {}
    for name in ("SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID"):
        matches = [item for item in candidates if item[0] == name]
        if matches:
            _name, value, _domain, _path = max(
                matches,
                key=lambda item: (
                    int(item[2].lower().lstrip(".") == "bilibili.com"),
                    int(item[2].lower().lstrip(".").endswith(".bilibili.com")),
                    int(item[3] == "/"),
                    len(item[2]),
                ),
            )
            selected[name] = value
    return selected
