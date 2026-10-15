"""Local hook daemon: keeps the FalkorDB connection open, so a hook costs milliseconds instead of a Python start-up.
Started on demand by hook_client.py; exits by itself after 30 idle minutes. One daemon per port (one per repo setup).

    python -m precedent serve [--port 7341]
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import hook

IDLE_SECONDS = 30 * 60
last_request = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global last_request
        last_request = time.time()
        ev = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        o = hook.handle(ev)
        body = json.dumps(o).encode() if o else b""
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse lets a second daemon bind the same port; it must fail and exit
    daemon_threads = True


def serve(port=7341):
    server = Server(("127.0.0.1", port), Handler)

    def stop_when_idle():
        while time.time() - last_request < IDLE_SECONDS:
            time.sleep(30)
        server.shutdown()

    threading.Thread(target=stop_when_idle, daemon=True).start()
    server.serve_forever()
