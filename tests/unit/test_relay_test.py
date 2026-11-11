"""scripts/relay_test.py decides pass/fail correctly. The real check against a
live Postfix is mta/test/run.sh (CI job "mta")."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "relay_test", Path(__file__).resolve().parents[2] / "scripts" / "relay_test.py"
)
assert spec and spec.loader
relay_test = importlib.util.module_from_spec(spec)
sys.modules["relay_test"] = relay_test  # dataclasses look their module up here
spec.loader.exec_module(relay_test)


def fake_smtp(rcpt_reply, bounce_reply=(250, b"ok"), vrfy=(502, b"disabled"), connect_error=None):
    class Fake:
        def __init__(self, host, port, timeout=None):
            if connect_error:
                raise connect_error

        def ehlo(self, name):
            return 250, b"hi"

        def docmd(self, cmd, arg=""):
            if cmd == "VRFY":
                return vrfy
            if cmd.startswith("RCPT"):
                return bounce_reply if "b-relay-test@" in cmd else rcpt_reply
            return 250, b"ok"

        def quit(self):
            pass

        def close(self):
            pass

    return Fake


def run(factory, bounce=True):
    argv = ["--host", "mta.example.test"] + (
        ["--bounce-domain", "bounce.example.test"] if bounce else []
    )
    return relay_test.main(argv, smtp_factory=factory)


def test_refusing_everything_but_bounces_passes():
    assert run(fake_smtp((554, b"Relay access denied"))) == relay_test.PASS


def test_an_accepted_relay_fails():
    assert run(fake_smtp((250, b"ok"))) == relay_test.OPEN


def test_refusing_bounces_fails():
    """A host that refuses mail for our bounce domain loses every bounce."""
    assert run(fake_smtp((554, b"no"), bounce_reply=(550, b"no such user"))) == relay_test.OPEN


@pytest.mark.parametrize("factory", [
    fake_smtp((450, b"try later")),
    fake_smtp((554, b"no"), connect_error=ConnectionRefusedError("refused")),
])  # fmt: skip
def test_unproven_is_not_a_pass(factory):
    assert run(factory) == relay_test.INCONCLUSIVE


def test_vrfy_that_reveals_users_fails():
    assert run(fake_smtp((554, b"no"), vrfy=(250, b"root <root@mta>"))) == relay_test.OPEN


def test_probes_cover_sender_specified_routing():
    names = {p.name for p in relay_test.probes("bounce.example.test")}
    assert {"percent hack", "bang path", "source route", "quoted local part"} <= names
