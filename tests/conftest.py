import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatfun-test-"))
