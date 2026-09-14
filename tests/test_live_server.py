"""L5: a real uvicorn server, a real WebSocket client, a real file watcher.
FastAPI's test client bypasses uvicorn's protocol stack; this does not."""
import json
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from websockets.sync.client import connect

from plainsolid.server import create_app

pytestmark = pytest.mark.api


@pytest.fixture
def live(project):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(project, serve_client=False)
    app.state.workspace.open("bracket.py")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"127.0.0.1:{port}", project
    server.should_exit = True
    thread.join(timeout=10)


def test_external_edit_reaches_the_websocket(live):
    host, project = live
    docs = json.load(urllib.request.urlopen(f"http://{host}/api/documents"))
    did = docs[0]["id"]
    with connect(f"ws://{host}/api/documents/{did}/events", open_timeout=10) as ws:
        assert json.loads(ws.recv(timeout=10))["event"] == "hello"
        path = project / "bracket.py"
        path.write_text(path.read_text().replace("thickness = 4.0", "thickness = 6.0"))
        msg = json.loads(ws.recv(timeout=15))
        assert msg["event"] == "external"
        tree = json.load(urllib.request.urlopen(f"http://{host}/api/documents/{did}/tree"))
        assert tree["hash"] == msg["hash"]
        assert next(p["value"] for p in tree["params"] if p["name"] == "thickness") == 6.0
