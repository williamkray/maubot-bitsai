from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(text: str) -> dict:
    try:
        from ruamel.yaml import YAML
    except ImportError:
        pass
    else:
        return YAML(typ="safe").load(text)
    try:
        import yaml
    except ImportError:
        pytest.skip("no YAML parser available (ruamel.yaml or PyYAML)")
    return yaml.safe_load(text)


def test_maubot_yaml_parses_and_uses_asyncpg():
    data = _load_yaml((ROOT / "maubot.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["database_type"] == "asyncpg"


def test_load_fixture_returns_dict(load_fixture):
    data = load_fixture("mcp_initialize.json")
    assert isinstance(data, dict)
