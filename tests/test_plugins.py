"""Plugin loading: discovery, metadata, failure isolation and the API."""

from __future__ import annotations

from pathlib import Path

from havoc.plugin_loader import PluginLoader

GOOD_PLUGIN = '''
"""A tiny demo plugin."""

__plugin_name__ = "demo"
__plugin_version__ = "1.0"
__plugin_description__ = "Demo plugin for tests"

_events = []


def setup(api):
    async def ping(ctx):
        await ctx.reply("pong from plugin")

    api.register_command(name="plugping", handler=ping, description="Plugin ping")

    async def on_message(event):
        _events.append(event.chat_id)

    api.subscribe("demo.watch", on_message)
'''

BROKEN_IMPORT = "import does_not_exist_anywhere\n"


def _write(directory: Path, name: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source)
    return path


async def test_loads_plugin_and_registers_command(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "demo.py", GOOD_PLUGIN)
    loader = PluginLoader(plugins, runtime, runtime.logger)
    records = await loader.load_all()
    assert [record.name for record in records] == ["demo"]
    assert records[0].loaded is True
    assert runtime.registry.resolve("plugping") is not None
    assert any(sub.name == "plugin:demo:demo.watch" for sub in runtime.event_engine.subscriptions)


async def test_plugin_command_works_end_to_end(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "demo.py", GOOD_PLUGIN)
    await PluginLoader(plugins, runtime, runtime.logger).load_all()
    from conftest import invoke

    sent = await invoke(runtime, ".plugping")
    assert any("pong from plugin" in message.text for message in sent)


async def test_broken_plugin_does_not_break_loading(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "broken.py", BROKEN_IMPORT)
    _write(plugins, "demo.py", GOOD_PLUGIN)
    records = await PluginLoader(plugins, runtime, runtime.logger).load_all()
    by_name = {record.name: record for record in records}
    assert by_name["demo"].loaded is True
    assert by_name["broken"].loaded is False
    assert by_name["broken"].error


async def test_missing_directory_is_not_an_error(runtime, tmp_path: Path) -> None:
    records = await PluginLoader(tmp_path / "absent", runtime, runtime.logger).load_all()
    assert records == []


async def test_plugin_without_setup_is_rejected(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "nosetup.py", "__plugin_name__ = 'nosetup'\nvalue = 1\n")
    records = await PluginLoader(plugins, runtime, runtime.logger).load_all()
    assert records[0].loaded is False


async def test_metadata_falls_back_to_filename(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "plain.py", "def setup(api):\n    api.add_strings('en', {'x': 'y'})\n")
    records = await PluginLoader(plugins, runtime, runtime.logger).load_all()
    assert records[0].name == "plain"


async def test_unload_removes_commands(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "demo.py", GOOD_PLUGIN)
    loader = PluginLoader(plugins, runtime, runtime.logger)
    await loader.load_all()
    assert await loader.unload("demo") is True
    assert runtime.registry.resolve("plugping") is None
    # the engine must not keep calling a handler from an unloaded plugin
    assert not any(sub.name == "plugin:demo:demo.watch" for sub in runtime.event_engine.subscriptions)


async def test_snapshot_is_json_friendly(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    _write(plugins, "demo.py", GOOD_PLUGIN)
    loader = PluginLoader(plugins, runtime, runtime.logger)
    await loader.load_all()
    snapshot = loader.snapshot()
    assert snapshot[0]["name"] == "demo"
    assert isinstance(snapshot[0]["loaded"], bool)


async def test_plugin_api_settings_roundtrip(runtime, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    source = '''
__plugin_name__ = "settings"

def setup(api):
    async def remember(ctx):
        await api.set_setting("greeting", "hello")
        value = await api.get_setting("greeting", "none")
        await ctx.reply(str(value))

    api.register_command(name="remember", handler=remember)
'''
    _write(plugins, "settings.py", source)
    await PluginLoader(plugins, runtime, runtime.logger).load_all()
    from conftest import invoke

    sent = await invoke(runtime, ".remember")
    assert any("hello" in message.text for message in sent)
    assert await runtime.store.get_setting("plugin.settings.greeting") == "hello"


GATED_PLUGIN = '''
__plugin_name__ = "gated"


def setup(api):
    async def report(ctx):
        await ctx.reply("gated ran")

    api.register_command(
        name="gatedcmd",
        handler=report,
        description="Only runs while a feature is enabled",
        feature="autoreply",
        requires_feature=True,
    )
'''


async def test_requires_feature_blocks_until_enabled(runtime, tmp_path: Path) -> None:
    """``requires_feature`` is the plugin-facing counterpart of a feature gate."""
    from conftest import invoke, sent_text

    plugins = tmp_path / "plugins"
    _write(plugins, "gated.py", GATED_PLUGIN)
    await PluginLoader(plugins, runtime, runtime.logger).load_all()

    await invoke(runtime, ".gatedcmd")
    blocked = sent_text(runtime)
    assert "autoreply" in blocked
    assert "gated ran" not in blocked

    await runtime.store.set_feature("autoreply", True)
    await invoke(runtime, ".gatedcmd")
    assert "gated ran" in sent_text(runtime)
