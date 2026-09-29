from pathlib import Path
from urllib.parse import quote

from PIL import Image

from plugin_core.renderer import VIDEO_CARD_TEMPLATE, PlaywrightRenderer


async def test_article_scroll_triggers_lazy_images_in_700_pixel_steps() -> None:
    class FakeArticle:
        async def bounding_box(self) -> dict[str, float]:
            return {"y": 100.0, "height": 2500.0}

    class FakePage:
        def __init__(self) -> None:
            self.scroll_y = 0.0
            self.scroll_targets: list[float] = []

        async def evaluate(self, script: str, value: float | None = None) -> float | None:
            if "window.scrollY" in script:
                return self.scroll_y
            if "document.body.scrollHeight" in script:
                return 2800.0
            if "window.scrollTo" in script:
                assert value is not None
                self.scroll_y = value
                self.scroll_targets.append(value)
                return None
            raise AssertionError(f"Unexpected page script: {script}")

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    renderer = PlaywrightRenderer()
    page = FakePage()

    await renderer._scroll_article(page, FakeArticle())

    assert page.scroll_targets == [100, 800, 1500, 2200, 2900]


async def test_tall_article_is_split_into_full_width_pages(tmp_path) -> None:
    html = """
    <html><body>
      <div class="container-all news" style="width:420px;background:#182333;color:white">
        <div id="NIE-art" style="height:13000px;padding:20px">
          <h1>长篇公告</h1><p>正文内容。</p>
        </div>
      </div>
    </body></html>
    """
    data_url = "data:text/html," + quote(html)

    pages = await PlaywrightRenderer(timeout_ms=5000)._capture_page(
        data_url,
        ".container-all.news",
        tmp_path / "article.png",
        hide_css="",
        require_images=False,
    )

    assert len(pages) == 2
    with Image.open(pages[0]) as first, Image.open(pages[1]) as second:
        assert first.width == second.width == 420
        assert first.height == 12000
        assert second.height > 900
    assert all(path.stat().st_size > 0 for path in pages)


async def test_bilibili_renderer_normalizes_stored_t_route(monkeypatch, tmp_path) -> None:
    renderer = PlaywrightRenderer(timeout_ms=5000)
    captured = {}

    async def capture_page(url, selector, output, hide_css, require_images):
        captured.update(
            {
                "url": url,
                "selector": selector,
                "hide_css": hide_css,
                "require_images": require_images,
            }
        )
        return [Path(output)]

    monkeypatch.setattr(renderer, "_capture_page", capture_page)

    await renderer.render(
        {
            "source_type": "bilibili",
            "category": "图文",
            "external_id": "1252945138288689153",
            "source_url": "https://t.bilibili.com/1252945138288689153",
        },
        tmp_path / "legacy-task.png",
    )

    assert captured["url"] == "https://www.bilibili.com/opus/1252945138288689153"
    assert captured["selector"] == ".bili-opus-view, .bili-dyn-detail__content"
    assert captured["require_images"] is True


async def test_bilibili_renderer_preserves_legacy_text_forward_and_article_routes(
    monkeypatch, tmp_path
) -> None:
    renderer = PlaywrightRenderer(timeout_ms=5000)
    captured_urls: list[str] = []

    async def capture_page(url, _selector, output, *, hide_css, require_images):
        assert hide_css
        assert require_images is True
        captured_urls.append(url)
        return [Path(output)]

    monkeypatch.setattr(renderer, "_capture_page", capture_page)

    for index, category in enumerate(("文字", "专栏", "转发"), start=1):
        await renderer.render(
            {
                "source_type": "bilibili",
                "category": category,
                "external_id": str(index),
                "source_url": f"https://t.bilibili.com/{index}",
            },
            tmp_path / f"legacy-{index}.png",
        )

    assert captured_urls == [
        "https://t.bilibili.com/1",
        "https://t.bilibili.com/2",
        "https://t.bilibili.com/3",
    ]


async def test_video_dynamic_uses_dedicated_card_template(monkeypatch, tmp_path) -> None:
    renderer = PlaywrightRenderer(timeout_ms=5000)
    captured = {}

    async def render_card(task, output):
        captured["task"] = task
        captured["output"] = output
        return output

    def paginate(path, image_format, quality=88):
        captured["pagination"] = (image_format, quality)
        return [path]

    monkeypatch.setattr(renderer, "_render_video_card", render_card)
    monkeypatch.setattr(renderer, "_paginate", paginate)
    pages = await renderer.render(
        {
            "source_type": "bilibili",
            "category": "视频",
            "title": "测试视频标题",
            "author": "测试 UP",
            "body_text": "视频动态文案",
            "video_cover_url": "https://i0.hdslb.com/bfs/cover/test.jpg",
            "video_url": "https://www.bilibili.com/video/BV1xx",
        },
        tmp_path / "video-card.png",
    )

    assert captured["output"].suffix == ".jpg"
    assert captured["task"]["title"] == "测试视频标题"
    assert captured["pagination"] == ("JPEG", 88)
    assert pages == [captured["output"]]
    assert '.bilibili-video-card' in VIDEO_CARD_TEMPLATE
    assert '{{ qrcode_url }}' in VIDEO_CARD_TEMPLATE
