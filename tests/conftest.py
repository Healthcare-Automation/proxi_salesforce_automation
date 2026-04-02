# Pytest conftest: add project src to path so tests can import utils
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
if _src.exists() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
