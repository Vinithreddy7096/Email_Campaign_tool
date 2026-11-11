"""SMTP submission primitives: dot-stuffing, reply classification, VERP, and one
round trip over a real socket."""

import smtplib
import socket
import threading
import uuid

import pytest

from workers.services import smtp_submit
from workers.services.smtp_submit import dot_stuff
from workers.services.verp import parse_verp, verp_address


def test_dot_stuffing_doubles_leading_dots_and_terminates():
    out = dot_stuff(b"Subject: x\r\n\r\n.hidden\nnormal\r\n..two\r\n")
    assert out == b"Subject: x\r\n\r\n..hidden\r\nnormal\r\n...two\r\n.\r\n"


def test_a_lone_dot_line_cannot_end_the_message_early():
    out = dot_stuff(b"a\r\n.\r\nb\r\n")
    assert out.count(b"\r\n.\r\n") == 1 and out.endswith(b"b\r\n.\r\n")


@pytest.mark.parametrize(
    ("code", "outcome"),
    [(250, "ACCEPTED"), (251, "ACCEPTED"), (421, "DEFERRED"), (452, "DEFERRED"),
     (550, "REJECTED"), (554, "REJECTED"), (199, "DEFERRED")],
)  # fmt: skip
def test_reply_classification(code, outcome):
    assert smtp_submit._classify(code, b"text").outcome == outcome


def test_postfix_queue_id_is_extracted():
    result = smtp_submit._classify(250, b"2.0.0 Ok: queued as 4Xk9Pq1Zz7z")
    assert result.queue_id == "4Xk9Pq1Zz7z"


def test_verp_round_trip_and_forgery(settings):
    job = uuid.uuid4()
    address = verp_address(job, "bounce.example.com")
    assert address.endswith("@bounce.example.com") and parse_verp(address) == job
    assert parse_verp(f"<{address.upper()}>") == job
    local, domain = address.split("@")
    forged = local[:-1] + ("0" if local[-1] != "0" else "1") + "@" + domain
    assert parse_verp(forged) is None
    other_job = verp_address(uuid.uuid4(), "b.example.com").split("-")[2]
    assert parse_verp(f"b-{job.hex}-{other_job}") is None
    assert parse_verp("postmaster@example.com") is None


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_round_trip_over_a_real_socket():
    """A real SMTP server on localhost (stdlib smtpd, Python ≤ 3.11)."""
    smtpd = pytest.importorskip("smtpd")
    asyncore = pytest.importorskip("asyncore")
    received = []

    class Sink(smtpd.SMTPServer):
        def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
            received.append((mailfrom, rcpttos, data))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = Sink(("127.0.0.1", port), None, decode_data=False)
    thread = threading.Thread(target=asyncore.loop, kwargs={"timeout": 0.05, "count": 200},
                              daemon=True)  # fmt: skip
    thread.start()
    try:
        result = smtp_submit.submit(
            envelope_from="b-x@bounce.example.com", recipient="r@example.net",
            payload=b"Subject: hi\r\n\r\n.dot\r\nbody\r\n", host="127.0.0.1", port=port,
            timeout=5, smtp_class=smtplib.SMTP,
        )  # fmt: skip
    finally:
        server.close()
        thread.join(timeout=5)
    assert result.outcome == "ACCEPTED" and result.code == 250
    mailfrom, rcpttos, data = received[0]
    assert (mailfrom, rcpttos) == ("b-x@bounce.example.com", ["r@example.net"])
    assert b"\n.dot\n" in data.replace(b"\r\n", b"\n")  # server un-stuffed our stuffing


def test_connection_refused_is_deferred():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once closed
    result = smtp_submit.submit(envelope_from="a@b.c", recipient="d@e.f", payload=b"x\r\n",
                                host="127.0.0.1", port=port, timeout=2)  # fmt: skip
    assert result.outcome == "DEFERRED"
