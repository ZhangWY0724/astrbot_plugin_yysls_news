# AstrBot 燕云十六声资讯插件

监控 B 站 UP 主动态和燕云十六声官网新闻，通过 AstrBot 的统一消息接口推送图片及原文链接。这个项目与独立 QQBot 服务分开维护，插件不调用 QQBot OpenAPI。

## 安装

将整个目录复制或克隆到 AstrBot 的 `data/plugins/astrbot_plugin_yysls_news/`，由 AstrBot 安装 `requirements.txt` 中的依赖并启用插件。

打包本地上传 ZIP：

```shell
python build_zip.py
```

压缩包会生成在 `dist/astrbot_plugin_yysls_news-v0.1.13.zip`，内部包含单个插件根目录及 `metadata.yaml`、`main.py` 等运行文件，不包含测试或本地数据。

插件管理页使用 AstrBot Plugin Pages，最低版本范围在 `metadata.yaml` 中声明。

截图需要 Playwright Chromium。若 AstrBot Python 环境没有浏览器，运行：

```shell
python -m playwright install chromium
```

## 配置与使用

先用 `/yyslsnews whoami` 查看自己的发送者 ID，再在 AstrBot 插件配置页填写 `admin_ids` 和需要监控的 B 站 UID。UID、关键词和忽略的动态类型每行一个；B 站轮询最短 5 分钟，官网最短 10 分钟。修改配置后重载插件。首次发现只建立游标，不补发历史内容。

在需要接收资讯的群聊或单聊中，由已配置的管理员发送：

```text
/yyslsnews target-add
```

其他管理命令：

```text
/yyslsnews status
/yyslsnews whoami
/yyslsnews target-list
/yyslsnews target-remove <目标ID>
/yyslsnews poll
```

插件详情页的“资讯管理” Page 用于 B 站扫码登录、查看登录状态、手动 Cookie 备用录入、管理推送目标和查看失败记录。手动 Cookie 只通过 AstrBot 管理页提交，不经聊天消息传输。

管理页可输入 B 站动态 ID 执行测试推送。测试动态会通过 Outbox 推送到所有已启用目标，不推进监控游标，也不受动态忽略规则影响。

B 站凭据在 `data/plugin_data/astrbot_plugin_yysls_news/` 下使用独立 Fernet 密钥加密保存。凭据、AccessToken 和 Cookie 不写入插件配置、日志或代码仓库。

## 核心流程

- B 站动态发现和去重使用 `bilibili-api-python` 结构化数据；图文 Opus 使用 `major.opus.jump_url`，视频卡片使用 `plugin_core/templates/bilibili_video.html` 独立模板，仅从结构化字段生成。
- 官网采集公开 HTML 列表，过滤分页链接；正文截图使用 `.container-all.news`。
- SQLite 事务保存内容和 Outbox 任务，更新游标；一个内容的截图由多个目标共享，失败时重试，图片失败时发送原文链接。
- OneBot v11 按推送目标将待发内容合并为一条转发；先发送整理提示，每条动态各占一个节点，分页图片收在对应节点内。文本模式会合并为一条带原文链接的普通消息。
- 采集、渲染、推送和 AstrBot 消息发送分别实现，B 站登录凭据单独加密管理。

## 开发

```text
metadata.yaml / main.py / _conf_schema.json
pages/manage/index.html
plugin_core/                 # 采集、解析、存储、渲染和 Outbox
tests/
```

少量常规设置由 `_conf_schema.json` 生成配置界面；扫码、队列和目标管理等交互放在 Plugin Pages。页面通过 `window.AstrBotPluginPage` 调用 `context.register_web_api()` 注册的后端接口。
