import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Return a loader for files in tests/fixtures/.

    ``.json`` files are parsed; anything else is returned as raw text.
    """

    def _load(name: str) -> Any:
        path = FIXTURES_DIR / name
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return json.loads(text)
        return text

    return _load
