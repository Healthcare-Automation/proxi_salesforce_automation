# Pytest conftest: add project src to path so tests can import utils
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
if _src.exists() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


@pytest.fixture(autouse=True)
def _deterministic_date_ai(monkeypatch):
    """Keep the suite deterministic regardless of the developer's shell: with no
    OPENAI_API_KEY the active-dates override uses the regex fallback. Tests that
    exercise the AI set the key + stub the client themselves; the opt-in
    live-prompt tests restore the key explicitly."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
