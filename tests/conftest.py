import sys
from pathlib import Path

# Ensure repository root is on sys.path so module-level imports in tests work when
# running pytest from within the repo directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
