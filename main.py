from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

from .plugin_core.bilibili import fetch_dynamic, normalize_dynamic_id, to_content
from .plugin_core.bilibili_urls import source_url_for_task
from .plugin_core.credentials import (
    BilibiliCredentialManager,
    BilibiliLoginError,
    BilibiliQrLoginClient,
)
from .plugin_core.delivery import OutboxWorker
from .plugin_core.monitor import Monitor, config_values
from .plugin_core.renderer import PlaywrightRenderer
from .plugin_core.store import Store
from .plugin_core.yysls import YyslsClient

PLUGIN_NAME = "astrbot_plugin_yysls_news"


@filter.command_group("yyslsnews")
def yyslsnews():
    """燕云十六声资讯插件命令。"""


@register(
    PLUGIN_NAME,
    "yyslsnews",
    "监控 B 站动态和燕云十六声官网新闻，通过 AstrBot 推送图片。",
    "0.1.13",
)
class YyslsNewsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir: Path | None = None
        self.store: Store | None = None
        self.credentials: BilibiliCredentialManager | None = None
        self.qr_client = BilibiliQrLoginClient()
        self.monitor: Monitor | None = None
        self.delivery: OutboxWorker | None = None
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._cycle_lock = asyncio.Lock()
        self._qr_task: asyncio.Task[None] | None = None
        self._qr_session_id = ""
        self._qr_state: dict[str, Any] = {
            "status": "not_started",
            "image_data_url": "",
            "message": "",
            "expires_at": 0,
        }
        self._config_fingerprint: tuple[Any, ...] | None = None
        self._register_page_apis()

    async def initialize(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._stop_event.clear()
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        values = config_values(self.config)
        self.store = Store(self.data_dir / "news.db")
        self.credentials = BilibiliCredentialManager(self.data_dir)
        self.monitor = Monitor(
            self.store,
            self.credentials,
            YyslsClient(timeout_seconds=values["http_timeout_seconds"]),
            lambda: config_values(self.config),
        )
        self.delivery = OutboxWorker(
            self.store,
            PlaywrightRenderer(timeout_ms=values["http_timeout_seconds"] * 1000),
            self._send_forward_batch,
            self.data_dir / "rendered",
            progress_sender=self._send_progress,
        )
        self._sync_configuration(force=True)
        self._worker_task = asyncio.create_task(self._run_worker())
        logger.info("燕云十六声资讯插件已启动。")

    async def terminate(self) -> None:
        self._stop_event.set()
        tasks = [task for task in (self._worker_task, self._qr_task) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._qr_session_id:
            self.qr_client.discard(self._qr_session_id)
        logger.info("燕云十六声资讯插件已停止。")

    async def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_cycle(limit=20, respect_enabled=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("资讯轮询周期失败: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _run_cycle(
        self, *, force: bool = False, limit: int = 20, respect_enabled: bool = False
    ) -> tuple[list[Any], int]:
        """串行执行采集和推送，避免定时轮询与手动命令互相抢任务。"""
        async with self._cycle_lock:
            self._sync_configuration()
            values = config_values(self.config)
            if (respect_enabled and not values["enabled"]) or not self.monitor or not self.delivery:
                return [], 0
            results = await self.monitor.poll_once(force=force)
            processed = await self.delivery.process_pending(limit=limit)
            return results, processed

    def _sync_configuration(self, force: bool = False) -> None:
        if not self.store:
            return
        values = config_values(self.config)
        fingerprint = (
            tuple(values["bilibili_uids"]),
            values["bilibili_poll_interval_seconds"],
            tuple(values["bilibili_filter_types"]),
            tuple(values["bilibili_filter_keywords"]),
        )
        if not force and fingerprint == self._config_fingerprint:
            return
        self.store.sync_subscriptions(
            values["bilibili_uids"],
            values["bilibili_poll_interval_seconds"],
            values["bilibili_filter_types"],
            values["bilibili_filter_keywords"],
        )
        self._config_fingerprint = fingerprint

    async def _send_progress(self, unified_msg_origin: str, text: str) -> Any:
        chain = MessageChain().message(text)
        return await self.context.send_message(unified_msg_origin, chain)

    async def _send_forward_batch(
        self,
        unified_msg_origin: str,
        items: list[dict[str, Any]],
    ) -> Any:
        if not items:
            raise ValueError("不能发送空的资讯批次")

        message_mode = str(items[0]["task"].get("message_mode") or "image").strip().lower()
        if message_mode == "text":
            message = "\n\n".join(
                f"{str(item['task'].get('title') or '资讯更新')}已更新，点击查看：\n"
                f"{_source_link(item['task'])}"
                for item in items
            )
            target_id = int(items[0]["task"].get("delivery_target_id") or 0)
            logger.info(
                "开始发送文本批次: target=%s items=%s",
                target_id,
                len(items),
            )
            result = await self.context.send_message(
                unified_msg_origin,
                MessageChain().message(message),
            )
            if result is False:
                raise RuntimeError("AstrBot 未找到匹配的推送目标")
            logger.info("AstrBot 文本批次发送完成: target=%s items=%s", target_id, len(items))
            return result

        nodes = []
        image_count = 0
        target_id = int(items[0]["task"].get("delivery_target_id") or 0)
        for item in items:
            task = item["task"]
            image_pages = item.get("pages")
            title = str(task.get("title") or "资讯更新")
            source_url = _source_link(task)
            if image_pages:
                content = [Image.fromFileSystem(str(path)) for path in image_pages]
                content.append(Plain(source_url))
                image_count += len(image_pages)
            else:
                content = [Plain(f"{title}已更新，点击查看：\n{source_url}")]
            nodes.append(Node(uin=0, name="燕云资讯", content=content))

        if image_count == 0:
            logger.warning(
                "合并转发没有可发送的截图，将使用原文文本兜底: target=%s nodes=%s",
                target_id,
                len(nodes),
            )
        logger.info(
            "开始发送合并转发: target=%s nodes=%s images=%s",
            target_id,
            len(nodes),
            image_count,
        )
        result = await self.context.send_message(
            unified_msg_origin,
            MessageChain(chain=[Nodes(nodes)]),
        )
        if result is False:
            raise RuntimeError("AstrBot 未找到匹配的推送目标")
        logger.info(
            "AstrBot 合并转发调用完成: target=%s nodes=%s",
            target_id,
            len(nodes),
        )
        return result

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = set(config_values(self.config)["admin_ids"])
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            return False
        return bool(sender_id and sender_id in admins)

    def _command_denied(self, event: AstrMessageEvent) -> str | None:
        return None if self._is_admin(event) else "此命令仅允许 admin_ids 中的发送者使用。"

    @yyslsnews.command("status")
    async def status_command(self, event: AstrMessageEvent):
        """查看采集、凭据和 Outbox 状态。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.store or not self.credentials:
            yield event.plain_result("插件尚未完成初始化。")
            return
        counts = self.store.status_counts()
        account = self.credentials.status()
        yield event.plain_result(
            f"B 站登录：{account['status']}（{account.get('username') or '未绑定'}）\n"
            f"UP 主：{len(self.store.list_subscriptions())}；"
            f"推送目标：{len(self.store.list_targets())}\n"
            f"待推送：{counts.get('pending', 0)}；重试：{counts.get('retry', 0)}；"
            f"失败：{counts.get('failed', 0)}"
        )

    @yyslsnews.command("whoami")
    async def whoami(self, event: AstrMessageEvent):
        """显示自己的发送者 ID，供管理员配置使用。"""
        try:
            sender_id = str(event.get_sender_id())
        except Exception:
            sender_id = ""
        yield event.plain_result(f"你的发送者 ID：{sender_id or '当前平台未提供'}")

    @yyslsnews.command("target-add")
    async def target_add(self, event: AstrMessageEvent):
        """把当前群聊或单聊加入资讯推送目标。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.store:
            yield event.plain_result("插件尚未完成初始化。")
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            yield event.plain_result("AstrBot 未提供当前会话标识。")
            return
        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", "") or "")
        target_id = self.store.upsert_target(
            umo, display_name="群聊" if group_id else "单聊", message_mode="image"
        )
        yield event.plain_result(f"已添加当前会话为资讯目标（ID {target_id}）。")

    @yyslsnews.command("target-list")
    async def target_list(self, event: AstrMessageEvent):
        """列出推送目标。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.store:
            yield event.plain_result("插件尚未完成初始化。")
            return
        targets = self.store.list_targets()
        if not targets:
            yield event.plain_result("尚无推送目标。")
            return
        yield event.plain_result(
            "推送目标：\n"
            + "\n".join(
                f"{row['id']}. {row.get('display_name') or '会话'} — {row['unified_msg_origin']}"
                for row in targets
            )
        )

    @yyslsnews.command("target-remove")
    async def target_remove(self, event: AstrMessageEvent, target_id: int):
        """按 ID 删除推送目标。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.store or not self.store.delete_target(target_id):
            yield event.plain_result("没有找到该推送目标。")
            return
        yield event.plain_result(f"已删除推送目标 {target_id}。")

    @yyslsnews.command("uid-add")
    async def uid_add(self, event: AstrMessageEvent, uid: int):
        """添加一个 B 站 UP 主 UID。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if uid <= 0:
            yield event.plain_result("UID 必须是正整数。")
            return
        values = config_values(self.config)["bilibili_uids"]
        if uid not in values:
            values.append(uid)
        self._save_uid_config(values)
        yield event.plain_result(f"已添加 UID {uid}；首次采集只建立游标。")

    @yyslsnews.command("uid-remove")
    async def uid_remove(self, event: AstrMessageEvent, uid: int):
        """停止监控一个 B 站 UP 主 UID。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        values = [value for value in config_values(self.config)["bilibili_uids"] if value != uid]
        self._save_uid_config(values)
        yield event.plain_result(f"已停止监控 UID {uid}。")

    def _save_uid_config(self, values: list[int]) -> None:
        self.config["bilibili_uids"] = "\n".join(str(value) for value in sorted(set(values)))
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        self._sync_configuration(force=True)

    @yyslsnews.command("poll")
    async def poll_command(self, event: AstrMessageEvent):
        """立即采集一轮并处理可发送任务。"""
        denied = self._command_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        if not self.monitor or not self.delivery:
            yield event.plain_result("插件尚未完成初始化。")
            return
        results, processed = await self._run_cycle(
            force=True,
            limit=50,
            respect_enabled=False,
        )
        yield event.plain_result(
            f"采集发现 {sum(item.discovered for item in results)} 条，"
            f"新增 {sum(item.inserted for item in results)} 条，"
            f"处理推送任务 {processed} 个。"
        )

    def _register_page_apis(self) -> None:
        prefix = f"/{PLUGIN_NAME}"
        routes = (
            ("/status", self._page_status, ["GET"], "读取资讯插件状态"),
            ("/login/start", self._page_login_start, ["POST"], "开始 B 站扫码登录"),
            ("/login/status", self._page_login_status, ["GET"], "读取 B 站扫码状态"),
            ("/credential/manual", self._page_manual_cookie, ["POST"], "加密保存手动 Cookie"),
            ("/credential/remove", self._page_remove_credential, ["POST"], "删除 B 站登录态"),
            ("/test-push", self._page_test_push, ["POST"], "按动态 ID 进行 B 站测试推送"),
            ("/targets", self._page_targets, ["GET"], "列出 AstrBot 推送目标"),
            ("/targets/add", self._page_add_target, ["POST"], "新增 AstrBot 推送目标"),
            ("/targets/<target_id>/delete", self._page_delete_target, ["POST"], "删除推送目标"),
            ("/failures", self._page_failures, ["GET"], "列出失败推送"),
        )
        for route, handler, methods, description in routes:
            self.context.register_web_api(prefix + route, handler, methods, description)

    def _require_dashboard_user(self):
        if not getattr(request, "username", None):
            return error_response("请先登录 AstrBot WebUI。", status_code=401)
        return None

    async def _page_status(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if not self.store or not self.credentials:
            return error_response("插件尚未完成初始化。", status_code=503)
        credential_status = self.credentials.status()
        if credential_status["status"] == "logged_in" and not credential_status["username"]:
            credential_data = self.credentials.load()
            if credential_data:
                username = await self._read_bilibili_username(credential_data)
                if username:
                    credential_data["username"] = username
                    self.credentials.save(credential_data)
                    credential_status = self.credentials.status()
        return json_response(
            {
                "credential": credential_status,
                "subscriptions": self.store.list_subscriptions(),
                "targets": self.store.list_targets(),
                "queue": self.store.status_counts(),
                "enabled": config_values(self.config)["enabled"],
            }
        )

    async def _page_login_start(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if self._qr_task and not self._qr_task.done():
            return json_response(self._qr_state)
        try:
            info = await self.qr_client.generate()
        except Exception as exc:
            logger.warning("生成 B 站二维码失败: %s", type(exc).__name__)
            return error_response("生成二维码失败，请检查 AstrBot 主机网络。", status_code=502)
        self._qr_session_id = info.session_id
        self._qr_state = {
            "status": "waiting",
            "image_data_url": info.image_data_url,
            "message": "请使用 B 站 App 扫描二维码。",
            "expires_at": int(time.time()) + 180,
        }
        self._qr_task = asyncio.create_task(self._poll_login(info.session_id))
        return json_response(self._qr_state)

    async def _page_login_status(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        return json_response(self._qr_state)

    async def _poll_login(self, session_id: str) -> None:
        if not self.credentials:
            return
        deadline = time.monotonic() + 180
        failures = 0
        try:
            while time.monotonic() < deadline and not self._stop_event.is_set():
                try:
                    result = await self.qr_client.poll(session_id)
                    failures = 0
                except BilibiliLoginError as exc:
                    failures += 1
                    logger.warning("B 站扫码状态检查失败: %s", type(exc).__name__)
                    if failures >= 5:
                        self._qr_state.update(
                            status="failed",
                            message="连续网络错误，已停止本次登录流程。",
                            image_data_url="",
                        )
                        return
                    await asyncio.sleep(min(2**failures, 30))
                    continue
                if result.is_done and result.credential:
                    credential = dict(result.credential)
                    username = await self._read_bilibili_username(credential)
                    if username:
                        credential["username"] = username
                    self.credentials.save(credential)
                    self._qr_state.update(
                        status="logged_in",
                        message=(
                            f"登录成功，当前账号：{username}，凭据已加密保存。"
                            if username
                            else "登录成功，凭据已加密保存；暂未获取到账号名称。"
                        ),
                        image_data_url="",
                    )
                    return
                if result.is_expired:
                    self._qr_state.update(
                        status="expired", message="二维码已过期，请重新生成。", image_data_url=""
                    )
                    return
                await asyncio.sleep(3)
            self._qr_state.update(
                status="expired", message="二维码等待超时，请手动重新生成。", image_data_url=""
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("B 站扫码登录失败: %s", type(exc).__name__)
            self._qr_state.update(
                status="failed", message="登录失败，请检查日志。", image_data_url=""
            )
        finally:
            self.qr_client.discard(session_id)
            self._qr_session_id = ""

    async def _page_manual_cookie(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if not self.credentials:
            return error_response("插件尚未完成初始化。", status_code=503)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容无效。", status_code=400)
        credential = {
            "sessdata": str(payload.get("sessdata") or "").strip(),
            "bili_jct": str(payload.get("bili_jct") or "").strip(),
            "dedeuserid": str(payload.get("dedeuserid") or "").strip(),
            "buvid3": str(payload.get("buvid3") or "").strip(),
        }
        if not all(credential[key] for key in ("sessdata", "bili_jct", "dedeuserid")):
            return error_response("SESSDATA、bili_jct 和 DedeUserID 必填。", status_code=400)
        try:
            username = await self._read_bilibili_username(credential)
            if username:
                credential["username"] = username
            self.credentials.save(credential)
        except (OSError, ValueError) as exc:
            logger.error("B 站手动 Cookie 保存失败: %s", type(exc).__name__)
            return error_response("加密保存失败。", status_code=500)
        return json_response({"saved": True, "message": "Cookie 已加密保存。"})

    async def _page_test_push(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if not self.store or not self.credentials or not self.delivery:
            return error_response("插件尚未完成初始化。", status_code=503)

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容无效。", status_code=400)
        try:
            dynamic_id = normalize_dynamic_id(payload.get("dynamic_id"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        targets = self.store.list_targets()
        if not targets:
            return error_response("请先添加至少一个推送目标。", status_code=409)

        async with self._cycle_lock:
            if not await self.credentials.ensure_valid(force=True):
                return error_response("B 站登录态不可用，请重新登录后再试。", status_code=401)
            credential_data = self.credentials.load()
            if not credential_data:
                return error_response("未找到有效的 B 站登录凭据，请重新登录。", status_code=401)

            try:
                model = await fetch_dynamic(dynamic_id, credential_data)
            except ValueError as exc:
                logger.info("手动测试推送动态不可用: dynamic_id=%s", dynamic_id)
                return error_response(str(exc), status_code=422)
            except Exception as exc:
                logger.warning(
                    "手动测试推送读取动态失败: dynamic_id=%s error=%s",
                    dynamic_id,
                    type(exc).__name__,
                )
                return error_response(
                    "读取动态失败，请确认动态 ID 有效且登录账号可以访问。",
                    status_code=502,
                )

            content = replace(
                to_content(model),
                source_key=f"manual-test:{model.uid}:{uuid4().hex}",
            )
            content_id, _inserted, task_count = self.store.insert_content(content)
            if task_count == 0:
                return error_response("没有可用的推送目标，请检查目标状态后重试。", status_code=409)

            logger.info(
                "B 站手动测试推送入队: dynamic_id=%s targets=%s",
                dynamic_id,
                task_count,
            )
            try:
                processed = await self.delivery.process_content(content_id)
            except Exception as exc:
                logger.error(
                    "B 站手动测试推送处理异常: dynamic_id=%s error=%s",
                    dynamic_id,
                    type(exc).__name__,
                )
                return error_response(
                    "动态已加入 Outbox，但本次处理异常；请检查失败记录。",
                    status_code=500,
                )

            delivery_counts = self.store.content_delivery_counts(content_id)
            logger.info(
                "B 站手动测试推送完成: dynamic_id=%s processed=%s statuses=%s",
                dynamic_id,
                processed,
                delivery_counts,
            )
            return json_response(
                {
                    "dynamic_id": dynamic_id,
                    "title": content.title,
                    "author": content.author,
                    "target_count": task_count,
                    "processed": processed,
                    "delivery": delivery_counts,
                }
            )

    async def _read_bilibili_username(self, credential: dict[str, Any]) -> str:
        try:
            from .plugin_core.bilibili_account import fetch_self_username

            return await fetch_self_username(credential)
        except Exception as exc:
            logger.warning("读取 B 站登录账号名称失败: %s", type(exc).__name__)
            return ""

    async def _page_remove_credential(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if self.credentials:
            self.credentials.clear()
        return json_response({"removed": True})

    async def _page_targets(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        return json_response({"targets": self.store.list_targets() if self.store else []})

    async def _page_add_target(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        if not self.store:
            return error_response("插件尚未完成初始化。", status_code=503)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容无效。", status_code=400)
        try:
            target_id = self.store.upsert_target(
                str(payload.get("unified_msg_origin") or ""),
                str(payload.get("display_name") or ""),
                "image" if payload.get("message_mode", "image") != "text" else "text",
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"id": target_id})

    async def _page_delete_target(self, target_id: str):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        try:
            parsed_id = int(target_id)
        except ValueError:
            return error_response("目标 ID 无效。", status_code=400)
        if not self.store or not self.store.delete_target(parsed_id):
            return error_response("未找到推送目标。", status_code=404)
        return json_response({"deleted": True})

    async def _page_failures(self):
        denied = self._require_dashboard_user()
        if denied:
            return denied
        failures = self.store.recent_failures(20) if self.store else []
        return json_response({"failures": failures})


def _source_link(task: dict[str, Any]) -> str:
    return source_url_for_task(task)
