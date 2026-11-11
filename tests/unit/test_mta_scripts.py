"""mta/bin/forward-bounce and ship-postfix-log: what they send is what the app
accepts, and failures make Postfix retry instead of losing mail."""

import importlib.machinery
import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from api.events import _signature_ok

BIN = Path(__file__).resolve().parents[2] / "mta" / "bin"


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


forward = load("forward-bounce")
ship = load("ship-postfix-log")


@pytest.fixture
def intake():
    """A local HTTP server recording requests; answers with ``status``."""
    seen = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, dict(self.headers), body))
            self.send_response(state["status"])
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}", "seen": seen, "state": state}
    server.shutdown()


def test_signature_matches_the_apps_check():
    headers = forward.sign("k" * 32, b"body", "1700000000")
    assert _signature_ok(
        "k" * 32, headers["X-Webhook-Timestamp"], b"body", headers["X-Webhook-Signature"]
    )
    assert not _signature_ok(
        "other", headers["X-Webhook-Timestamp"], b"body", headers["X-Webhook-Signature"]
    )


def run_forward(monkeypatch, intake, raw=b"From: x\r\n\r\nbounce"):
    monkeypatch.setenv("INTAKE_URL", intake["url"])
    monkeypatch.setenv("MTA_INGEST_SECRET", "s" * 32)
    monkeypatch.setattr(forward, "CONFIG_FILE", "/nonexistent")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    return forward.main()


def test_forward_posts_the_raw_message_signed(monkeypatch, intake):
    assert run_forward(monkeypatch, intake) == forward.EX_OK
    path, headers, body = intake["seen"][0]
    assert path == "/api/v1/events/mail" and body == b"From: x\r\n\r\nbounce"
    assert headers["Content-Type"] == "message/rfc822"
    assert _signature_ok(
        "s" * 32, headers["X-Webhook-Timestamp"], body, headers["X-Webhook-Signature"]
    )


@pytest.mark.parametrize(
    ("status", "exit_code"), [(500, 75), (503, 75), (401, 75), (400, 0), (413, 0)]
)
def test_forward_retries_unless_retrying_cannot_help(monkeypatch, intake, status, exit_code):
    intake["state"]["status"] = status
    assert run_forward(monkeypatch, intake) == exit_code


def test_forward_tempfails_when_the_app_is_down(monkeypatch, intake):
    monkeypatch.setenv("INTAKE_URL", "http://127.0.0.1:9")  # discard port: nothing listens
    monkeypatch.setenv("MTA_INGEST_SECRET", "s" * 32)
    monkeypatch.setattr(forward, "CONFIG_FILE", "/nonexistent")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"x")))
    assert forward.main() == forward.EX_TEMPFAIL


def test_forward_without_config_keeps_the_mail(monkeypatch):
    monkeypatch.delenv("INTAKE_URL", raising=False)
    monkeypatch.delenv("MTA_INGEST_SECRET", raising=False)
    monkeypatch.setattr(forward, "CONFIG_FILE", "/nonexistent")
    assert forward.main() == forward.EX_TEMPFAIL


@pytest.mark.django_db  # the parser looks the queue id up
def test_shipped_lines_parse_in_the_app():
    from workers.services.events import parse_postfix_line

    entry = {"__REALTIME_TIMESTAMP": "1760540000000000", "SYSLOG_IDENTIFIER": "postfix-marketing/smtp",
             "SYSLOG_PID": "4242", "_HOSTNAME": "mta1",
             "MESSAGE": "ABC123: to=<dana@example.org>, relay=mx.example.org[192.0.2.1]:25, delay=1, "
                        "delays=0/0/0/1, dsn=2.0.0, status=sent (250 2.0.0 Ok)"}  # fmt: skip
    assert ship.is_delivery_status(entry["MESSAGE"])
    assert not ship.is_delivery_status("connect from unknown[192.0.2.9]")
    parsed = parse_postfix_line(ship.to_line(entry))
    assert parsed is not None and parsed.type == "DELIVERED" and parsed.enhanced_code == "2.0.0"


def test_ship_posts_a_signed_batch(intake):
    config = {"INTAKE_URL": intake["url"], "MTA_INGEST_SECRET": "s" * 32}
    assert ship.post_lines(config, ["line one", "line two"]) == 200
    path, headers, body = intake["seen"][0]
    assert path == "/api/v1/events/postfix-log" and json.loads(body) == {
        "lines": ["line one", "line two"]
    }
    assert _signature_ok(
        "s" * 32, headers["X-Webhook-Timestamp"], body, headers["X-Webhook-Signature"]
    )


def test_cursor_is_saved_atomically(tmp_path):
    cursor = ship.Cursor(str(tmp_path / "state"))
    assert cursor.load() is None
    cursor.save("s=abc;i=1")
    assert cursor.load() == "s=abc;i=1"
