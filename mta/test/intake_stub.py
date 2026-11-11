"""Stand-ins inside the test MTA container (mta/test/run.sh):

- the app's event intake on 127.0.0.1:8099 — accepts a POST only with a valid
  signature (the same check the app makes) and appends what it received to
  /tmp/intake.log;
- a next-hop SMTP server on 127.0.0.1:2526 (the test marketing stream's
  relayhost) that writes every message it receives to /tmp/captured/, so the
  test can read what actually left the MTA — DKIM signature included.
"""

import hashlib
import hmac
import os
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["TEST_INGEST_SECRET"].encode()
LOG = "/tmp/intake.log"  # noqa: S108 — throwaway test container
CAPTURED = "/tmp/captured"  # noqa: S108


class Intake(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        stamp = self.headers.get("X-Webhook-Timestamp", "")
        expected = hmac.new(SECRET, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        fresh = stamp.isdigit() and abs(time.time() - int(stamp)) < 300
        ok = fresh and hmac.compare_digest(expected, self.headers.get("X-Webhook-Signature", ""))
        with open(LOG, "ab") as handle:
            handle.write(
                f"--- {self.path} signature={'ok' if ok else 'BAD'}\n".encode() + body + b"\n"
            )
        self.send_response(200 if ok else 401)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class Sink(socketserver.StreamRequestHandler):
    """Just enough SMTP to accept and store a message."""

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        self.reply("220 sink.example.test ESMTP")
        data: list[bytes] = []
        while line := self.rfile.readline():
            cmd = line.strip().upper()
            if cmd.startswith((b"EHLO", b"HELO")):
                self.reply("250 sink.example.test")
            elif cmd.startswith((b"MAIL", b"RCPT", b"RSET", b"NOOP")):
                self.reply("250 ok")
            elif cmd == b"DATA":
                self.reply("354 go ahead")
                while (chunk := self.rfile.readline()) not in (b".\r\n", b".\n", b""):
                    data.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                os.makedirs(CAPTURED, exist_ok=True)
                with open(os.path.join(CAPTURED, f"{uuid.uuid4().hex}.eml"), "wb") as handle:
                    handle.write(b"".join(data))
                data = []
                self.reply("250 ok: queued as SINK")
            elif cmd == b"QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("502 not implemented")


socketserver.ThreadingTCPServer.allow_reuse_address = True
sink = socketserver.ThreadingTCPServer(("127.0.0.1", 2526), Sink)
threading.Thread(target=sink.serve_forever, daemon=True).start()
HTTPServer(("127.0.0.1", 8099), Intake).serve_forever()
