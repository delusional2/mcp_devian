import pytest

from mcp_devian import db, forecast, mcp_server
from mcp_devian.config import Config, save_config


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEVIAN_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mcp_server.background, "is_enabled", lambda: False)
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(path or tmp_path / "s.sqlite3"))
    save_config(Config(configured=True))


def test_forecast_tools_on_empty_db(server):
    for out in (mcp_server.niche_forecast(), mcp_server.price_plan(species="dragon", style="moth"),
                mcp_server.forecast_accuracy(), mcp_server.price_plan(currency="points")):
        assert isinstance(out, str) and out
    assert forecast.GENTLE in mcp_server.niche_forecast()


def test_manifest_lists_every_tool():
    import asyncio
    import json
    from pathlib import Path

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    manifest = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())
    assert {t["name"] for t in manifest["tools"]} == names
