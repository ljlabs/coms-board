"""Container health probe for the local dashboard."""
import os
from urllib.request import urlopen
from urllib.request import Request

try:
    public_port = os.environ.get('COMS_PUBLIC_PORT', '8765')
    request = Request('http://127.0.0.1:8765/', headers={'Host': f'localhost:{public_port}'})
    with urlopen(request, timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
