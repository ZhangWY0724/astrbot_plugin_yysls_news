import ast
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def test_plugin_business_imports_are_package_relative() -> None:
    plugin_dir = Path(__file__).resolve().parents[1]
    source_files = [plugin_dir / "main.py", *plugin_dir.joinpath("plugin_core").glob("*.py")]

    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("plugin_core") for alias in node.names), path
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("plugin_core"):
                assert node.level > 0, path

    main_tree = ast.parse((plugin_dir / "main.py").read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "_clear_cached_plugin_core"
        for node in main_tree.body
    )


async def test_main_imports_business_modules_inside_astrbot_namespace(
    monkeypatch, tmp_path
) -> None:
    plugin_dir = Path(__file__).resolve().parents[1]
    namespace = "_test_astrbot_plugin_yysls_news"

    def _module(name: str, **attributes) -> ModuleType:
        module = ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        return module

    class FilterStub:
        @staticmethod
        def command_group(_name):
            class CommandGroup:
                def __init__(self, function):
                    self.function = function

                def __call__(self, *args, **kwargs):
                    return self.function(*args, **kwargs)

                @staticmethod
                def command(_command_name):
                    return lambda function: function

            return lambda function: CommandGroup(function)

        @staticmethod
        def command(_name):
            return lambda function: function

    class StarStub:
        def __init__(self, _context):
            pass

    class PlainStub:
        def __init__(self, text):
            self.text = text

    class ImageStub:
        def __init__(self, path):
            self.path = path

        @staticmethod
        def fromFileSystem(path):
            return ImageStub(path)

    class NodeStub:
        def __init__(self, uin, name, content):
            self.uin = uin
            self.name = name
            self.content = content

    class NodesStub:
        def __init__(self, nodes):
            self.nodes = nodes

    class MessageChainStub:
        def __init__(self, chain=None):
            self.chain = chain or []

        def message(self, text):
            self.chain.append(PlainStub(text))
            return self

    astrbot = _module("astrbot")
    astrbot.__path__ = []
    api = _module(
        "astrbot.api",
        AstrBotConfig=type("AstrBotConfig", (), {}),
        logger=logging.getLogger("test.astrbot"),
    )
    api.__path__ = []
    event = _module(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        MessageChain=MessageChainStub,
        filter=FilterStub(),
    )
    components = _module(
        "astrbot.api.message_components",
        Image=ImageStub,
        Node=NodeStub,
        Nodes=NodesStub,
        Plain=PlainStub,
    )
    star = _module(
        "astrbot.api.star",
        Context=type("Context", (), {}),
        Star=StarStub,
        register=lambda *_args, **_kwargs: lambda plugin_class: plugin_class,
    )
    web = _module(
        "astrbot.api.web",
        error_response=lambda *_args, **_kwargs: None,
        json_response=lambda *_args, **_kwargs: None,
        request=SimpleNamespace(username="test"),
    )
    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event),
        ("astrbot.api.message_components", components),
        ("astrbot.api.star", star),
        ("astrbot.api.web", web),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    package = _module(namespace)
    package.__path__ = [str(plugin_dir)]
    module_name = f"{namespace}.main"
    monkeypatch.setitem(sys.modules, namespace, package)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    try:
        spec.loader.exec_module(module)
        assert module.OutboxWorker.__module__ == f"{namespace}.plugin_core.delivery"
        assert "progress_sender" in inspect.signature(module.OutboxWorker).parameters

        class FakeContext:
            last_chain = None

            async def send_message(self, _umo, chain):
                self.last_chain = chain
                return True

        plugin = object.__new__(module.YyslsNewsPlugin)
        plugin.context = FakeContext()
        source_url = "https://www.bilibili.com/opus/123"
        await plugin._send_forward_batch(
            "platform:GroupMessage:target",
            [
                {
                    "task": {
                        "delivery_target_id": 1,
                        "source_type": "bilibili",
                        "category": "图文",
                        "external_id": "123",
                        "title": "动态标题应已在截图中",
                        "source_url": source_url,
                    },
                    "pages": [tmp_path / "image.jpg"],
                }
            ],
        )
        node = plugin.context.last_chain.chain[0].nodes[0]
        assert len(node.content) == 2
        assert isinstance(node.content[0], ImageStub)
        assert isinstance(node.content[1], PlainStub)
        assert node.content[1].text == source_url
    finally:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(f"{namespace}."):
                sys.modules.pop(name, None)
