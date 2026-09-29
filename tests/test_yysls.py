from plugin_core.yysls import parse_listing


def test_parse_listing_excludes_pagination_and_non_news_domains() -> None:
    html = """
    <html><body>
      <a href="/news/2026/09/22/100.html"><span>公告</span> 新版本更新说明</a>
      <a href="/news/2026/09/21/099.html">旧文章</a>
      <a href="/news/index_2.html">2</a>
      <a href="/news/index.html">末页</a>
      <a href="/news/page/2.html">2</a>
      <a href="https://example.com/news/2026/09/20/001.html">外部链接</a>
    </body></html>
    """

    items = parse_listing(html, "https://www.yysls.cn/news/")

    assert len(items) == 2
    assert items[0].title == "新版本更新说明"
    assert items[0].category == "公告"
    assert items[0].url.endswith("/100.html")
