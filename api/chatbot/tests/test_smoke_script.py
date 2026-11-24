"""scripts/smoke.sh, the post-deploy check, against a throwaway local server."""

import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "smoke.sh"

# The dev container mounts only api/ and has no curl. CI checks out the whole
# repository on a runner that has curl, so the test runs there.
pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or not shutil.which("curl"),
    reason="needs scripts/smoke.sh (outside api/) and curl",
)

READY_OK = (200, "application/json", b'{"status": "ok", "checks": {}}')
READY_DOWN = (503, "application/json", b'{"status": "unavailable"}')
SHELL = (200, "text/html", b'<html><div id="root"></div></html>')


@pytest.fixture
def serve():
    """Start a server answering {path: [response, ...]}; the last one repeats."""
    servers = []

    def start(routes):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                queue = routes.get(self.path)
                if not queue:
                    self.send_error(404)
                    return
                status, ctype, body = queue.pop(0) if len(queue) > 1 else queue[0]
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def run(base_url, timeout="3"):
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), base_url],
        env={**os.environ, "SMOKE_TIMEOUT": timeout, "SMOKE_INTERVAL": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, time.monotonic() - started


def test_passes_when_api_is_ready_and_frontend_serves_the_app(serve):
    result, _ = run(serve({"/api/health/ready/": [READY_OK], "/": [SHELL]}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_retries_until_the_new_version_comes_up(serve):
    routes = {"/api/health/ready/": [READY_DOWN, READY_DOWN, READY_OK], "/": [SHELL]}
    result, _ = run(serve(routes), timeout="10")
    assert result.returncode == 0, result.stdout + result.stderr


def test_fails_within_the_time_limit_when_not_ready(serve):
    result, elapsed = run(serve({"/api/health/ready/": [READY_DOWN], "/": [SHELL]}))
    assert result.returncode != 0
    assert elapsed < 15


def test_fails_when_readiness_path_returns_the_frontend_instead(serve):
    # A misrouted path can return 200 with the app's HTML rather than JSON.
    result, _ = run(serve({"/api/health/ready/": [SHELL], "/": [SHELL]}))
    assert result.returncode != 0


def test_fails_when_frontend_is_down(serve):
    result, _ = run(serve({"/api/health/ready/": [READY_OK]}))
    assert result.returncode != 0


def test_fails_on_connection_failure(serve):
    base = serve({})
    port = base.rsplit(":", 1)[1]
    # Nothing listens on port 1; connecting is refused immediately.
    result, elapsed = run(base.replace(port, "1"))
    assert result.returncode != 0
    assert elapsed < 15


def test_requires_a_base_url():
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
