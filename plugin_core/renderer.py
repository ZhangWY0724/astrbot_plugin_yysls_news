from __future__ import annotations

import base64
import io
import logging
import time
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jinja2 import Environment
from PIL import Image

from .bilibili_urls import source_url_for_task

LOGGER = logging.getLogger(__name__)
MAX_IMAGE_HEIGHT = 12_000
PAGE_OVERLAP = 80

VIDEO_CARD_TEMPLATE = files(__package__).joinpath("templates", "bilibili_video.html").read_text(
    encoding="utf-8"
)
VIDEO_CARD_ENVIRONMENT = Environment(autoescape=True)


class ImageRenderError(RuntimeError):
    """资讯页面截图失败。"""


class PlaywrightRenderer:
    def __init__(self, timeout_ms: int = 30_000) -> None:
        self.timeout_ms = max(timeout_ms, 5_000)

    async def render(self, task: dict[str, Any], output_path: str | Path) -> list[Path]:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if str(task.get("source_type")) == "bilibili" and str(task.get("category")) == "视频":
            original = await self._render_video_card(task, output.with_suffix(".jpg"))
            return self._paginate(original, "JPEG", quality=88)

        allowed_hosts = (
            ("bilibili.com", "hdslb.com")
            if str(task.get("source_type") or "") == "bilibili"
            else ("yysls.cn",)
        )
        url = _safe_http_url(source_url_for_task(task), allowed_hosts)
        if not url:
            raise ImageRenderError("内容缺少有效原文链接")
        if task.get("source_type") == "bilibili":
            selector = ".bili-opus-view, .bili-dyn-detail__content"
            hide_css = ".bili-header__menu, .login-tip { display: none !important; }"
            output = output.with_suffix(".jpg")
            require_images = True
        else:
            selector = ".container-all.news"
            hide_css = ""
            output = output.with_suffix(".png")
            require_images = False
        return await self._capture_page(
            url,
            selector,
            output,
            hide_css=hide_css,
            require_images=require_images,
        )

    async def _render_video_card(self, task: dict[str, Any], output: Path) -> Path:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 Playwright") from exc

        output.parent.mkdir(parents=True, exist_ok=True)
        safe_cover_url = _bilibili_cover_url(str(task.get("video_cover_url") or ""))
        safe_video_url = _safe_http_url(
            str(task.get("video_url") or task.get("source_url") or ""),
            ("bilibili.com", "hdslb.com"),
        )
        html = VIDEO_CARD_ENVIRONMENT.from_string(VIDEO_CARD_TEMPLATE).render(
            author=str(task.get("author") or "未知 UP 主"),
            title=str(task.get("title") or "投稿了新视频"),
            body_text=str(task.get("body_text") or ""),
            cover_url=safe_cover_url,
            video_url=safe_video_url,
            qrcode_url=_video_qrcode_data_url(safe_video_url),
        )
        started = time.monotonic()
        LOGGER.info(
            "B站视频卡片渲染开始: selector=.bilibili-video-card "
            "viewport=1920x1080 output=%s",
            output.name,
        )
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1920, "height": 1080},
                        device_scale_factor=1,
                        extra_http_headers={"Referer": "https://www.bilibili.com/"},
                    )
                    await page.set_content(
                        html,
                        wait_until="domcontentloaded",
                        timeout=max(self.timeout_ms, 30_000),
                    )
                    card = page.locator(".bilibili-video-card").first
                    await card.wait_for(state="visible", timeout=self.timeout_ms)
                    if safe_cover_url:
                        await page.wait_for_function(
                            """() => {
                                const image = document.querySelector('.video-cover img');
                                return image && image.complete && image.naturalWidth > 0;
                            }""",
                            timeout=self.timeout_ms,
                        )
                    await page.evaluate("() => document.fonts.ready")
                    await card.screenshot(
                        path=str(output),
                        type="jpeg",
                        quality=88,
                        animations="disabled",
                        timeout=120_000,
                    )
                finally:
                    await browser.close()
        except ImageRenderError:
            raise
        except Exception as exc:
            output.unlink(missing_ok=True)
            error = (
                "视频封面加载超时"
                if isinstance(exc, PlaywrightTimeoutError) and safe_cover_url
                else f"视频卡片截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            )
            LOGGER.warning(
                "B站视频卡片渲染失败: error=%s duration_ms=%s",
                error[:120],
                int((time.monotonic() - started) * 1000),
            )
            raise ImageRenderError(error) from exc
        _require_file(output)
        with Image.open(output) as screenshot:
            width, height = screenshot.size
        LOGGER.info(
            "B站视频卡片渲染完成: size=%sx%s bytes=%s duration_ms=%s",
            width,
            height,
            output.stat().st_size,
            int((time.monotonic() - started) * 1000),
        )
        return output

    async def _capture_page(
        self,
        url: str,
        selector: str,
        output: Path,
        hide_css: str,
        require_images: bool,
    ) -> list[Path]:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 Playwright") from exc
        started = time.monotonic()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1920, "height": 1080}, device_scale_factor=1
                    )
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=max(self.timeout_ms, 30_000)
                    )
                    if (response is None and url.startswith(("http://", "https://"))) or (
                        response is not None and response.status >= 400
                    ):
                        raise ImageRenderError("原文页面不可访问")
                    article = page.locator(selector).first
                    await article.wait_for(state="visible", timeout=self.timeout_ms)
                    await self._scroll_article(page, article)
                    try:
                        await _wait_images(page, selector, self.timeout_ms)
                    except PlaywrightTimeoutError as exc:
                        loaded, total = await _image_load_counts(page, selector)
                        if require_images:
                            LOGGER.warning(
                                "B站正文图片未全部加载: loaded=%s total=%s",
                                loaded,
                                total,
                            )
                            raise ImageRenderError(
                                f"B站正文图片加载超时（{loaded}/{total} 张已加载）"
                            ) from exc
                        LOGGER.warning(
                            "官网正文图片有加载超时，继续截取已加载内容: loaded=%s total=%s",
                            loaded,
                            total,
                        )
                    await page.evaluate("() => window.scrollTo(0, 0)")
                    await page.evaluate("() => document.fonts.ready")
                    if hide_css:
                        await page.add_style_tag(content=hide_css)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    dimensions = await article.evaluate(
                        "element => ({width: element.getBoundingClientRect().width, "
                        "height: Math.max(element.scrollHeight, "
                        "element.getBoundingClientRect().height)})"
                    )
                    image_type = "jpeg" if output.suffix.lower() in {".jpg", ".jpeg"} else "png"
                    pages = await self._screenshot_slices(
                        page,
                        selector,
                        output,
                        int(dimensions["width"]),
                        int(dimensions["height"]),
                        image_type,
                    )
                finally:
                    await browser.close()
        except ImageRenderError:
            self._cleanup_pages(output)
            raise
        except Exception as exc:
            self._cleanup_pages(output)
            LOGGER.warning(
                "资讯截图失败: host=%s error=%s duration_ms=%s",
                urlsplit(url).hostname,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise ImageRenderError(f"资讯截图失败: {type(exc).__name__}") from exc
        for path in pages:
            _require_file(path)
        return pages

    async def _screenshot_slices(
        self,
        page: Any,
        selector: str,
        output: Path,
        width: int,
        height: int,
        image_type: str,
    ) -> list[Path]:
        if width <= 0 or height <= 0:
            raise ImageRenderError("截图区域尺寸无效")
        pages: list[Path] = []
        start = 0
        index = 1
        try:
            while start < height:
                end = min(height, start + MAX_IMAGE_HEIGHT)
                slice_height = end - start
                wrapper_id = f"astrbot-news-slice-{index}"
                await page.evaluate(
                    """({selector, start, width, height, wrapperId}) => {
                        const source = document.querySelector(selector);
                        if (!source) throw new Error('screenshot selector disappeared');
                        document.getElementById(wrapperId)?.remove();
                        const frame = document.createElement('div');
                        frame.id = wrapperId;
                        frame.style.cssText =
                            `position:fixed;left:0;top:0;z-index:2147483647;` +
                            `width:${width}px;height:${height}px;` +
                            `overflow:hidden;background:#fff;`;
                        const clone = source.cloneNode(true);
                        clone.style.setProperty('position','absolute','important');
                        clone.style.setProperty('left','0','important');
                        clone.style.setProperty('top',`-${start}px`,'important');
                        clone.style.setProperty('width',`${width}px`,'important');
                        clone.style.setProperty('height','auto','important');
                        clone.style.setProperty('margin','0','important');
                        clone.style.setProperty('max-width','none','important');
                        clone.style.setProperty('transform','none','important');
                        frame.appendChild(clone);
                        document.body.appendChild(frame);
                    }""",
                    {
                        "selector": selector,
                        "start": start,
                        "width": width,
                        "height": slice_height,
                        "wrapperId": wrapper_id,
                    },
                )
                page_path = (
                    output
                    if index == 1
                    else output.with_name(f"{output.stem}-part-{index:02d}{output.suffix}")
                )
                options: dict[str, Any] = {
                    "path": str(page_path),
                    "type": image_type,
                    "animations": "disabled",
                    "timeout": self.timeout_ms * 2,
                }
                if image_type == "jpeg":
                    options["quality"] = 88
                try:
                    await page.locator(f"#{wrapper_id}").screenshot(**options)
                    pages.append(page_path)
                finally:
                    await page.evaluate(
                        "wrapperId => document.getElementById(wrapperId)?.remove()",
                        wrapper_id,
                    )
                if end >= height:
                    break
                start = end - PAGE_OVERLAP
                index += 1
        except Exception:
            self._cleanup_pages(output)
            raise
        return pages

    @staticmethod
    def _cleanup_pages(output: Path) -> None:
        candidates = [output]
        candidates.extend(output.parent.glob(f"{output.stem}-part-*{output.suffix}"))
        for path in candidates:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("截图分页清理失败: file=%s", path.name)

    async def _scroll_article(self, page: Any, article: Any) -> None:
        bounds = await article.bounding_box()
        if bounds is None:
            raise ImageRenderError("正文区域不可见")
        scroll_y = float(await page.evaluate("() => window.scrollY"))
        start = int(bounds["y"] + scroll_y)
        end = int(start + bounds["height"]) + 700
        for y in range(start, end, 700):
            await page.evaluate("y => window.scrollTo(0, y)", y)
            await page.wait_for_timeout(80)

    @staticmethod
    def _paginate(path: Path, image_format: str, quality: int = 88) -> list[Path]:
        try:
            with Image.open(path) as source:
                image = source.convert("RGB") if image_format == "JPEG" else source.copy()
        except OSError as exc:
            raise ImageRenderError("截图文件无法读取") from exc
        width, height = image.size
        if height <= MAX_IMAGE_HEIGHT:
            return [path]
        pages: list[Path] = []
        start = 0
        index = 1
        while start < height:
            end = min(height, start + MAX_IMAGE_HEIGHT)
            page_path = path.with_name(f"{path.stem}-part-{index:02d}{path.suffix}")
            page = image.crop((0, start, width, end))
            options = {"format": image_format}
            if image_format == "JPEG":
                options["quality"] = quality
            page.save(page_path, **options)
            pages.append(page_path)
            if end >= height:
                break
            start = end - PAGE_OVERLAP
            index += 1
        image.close()
        path.unlink(missing_ok=True)
        return pages


async def _wait_images(page: Any, selector: str, timeout_ms: int) -> None:
    await page.wait_for_function(
        "selector => { const root=document.querySelector(selector); if(!root)return false; "
        "return [...root.querySelectorAll('img')].filter(i=>{const r=i.getBoundingClientRect(); "
        "return r.width>0 && r.height>0;})"
        ".every(i=>i.complete && i.naturalWidth>0); }",
        arg=selector,
        timeout=timeout_ms,
    )


async def _image_load_counts(page: Any, selector: str) -> tuple[int, int]:
    counts = await page.evaluate(
        "selector => { const root=document.querySelector(selector); if(!root) "
        "return {loaded:0,total:0}; const images=[...root.querySelectorAll('img')]"
        ".filter(i=>{const r=i.getBoundingClientRect(); return r.width>0 && r.height>0;}); "
        "return {loaded:images.filter(i=>i.complete && i.naturalWidth>0).length, "
        "total:images.length}; }",
        selector,
    )
    return int(counts.get("loaded") or 0), int(counts.get("total") or 0)


def _safe_http_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_hosts
    ):
        return value.strip()
    return ""


def _bilibili_cover_url(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower()
    allowed_domains = ("hdslb.com", "bilivideo.com", "biliimg.com")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in allowed_domains
        )
    ):
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _video_qrcode_data_url(video_url: str) -> str:
    if not video_url:
        return ""
    try:
        import qrcode

        image = qrcode.make(video_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    except Exception as exc:
        LOGGER.warning("B站视频二维码生成失败: error=%s", type(exc).__name__)
        return ""
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _require_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ImageRenderError("截图未生成有效文件")
