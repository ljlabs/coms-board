"""Exercise the real server main with a fixture-only graceful stop endpoint."""
import sys
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from coms import server


class FixtureHandler(server.Handler):
    def do_POST(self):
        if self.path == '/__test_shutdown' and self._trusted_request():
            self._json(200,{'stopping':True})
            threading.Thread(target=self.server.shutdown,daemon=True).start()
        else:
            super().do_POST()


server.Handler=FixtureHandler
sys.exit(server.main())
