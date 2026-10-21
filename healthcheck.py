import os
import sys
import urllib.request

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{os.getenv('WEB_PORT', '8095')}/healthz", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except OSError:
    sys.exit(1)
