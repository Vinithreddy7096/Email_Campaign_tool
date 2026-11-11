#!/bin/sh
# End-to-end check of the MTA configuration (BUILD.md §10.10, §13 "automated
# open-relay test in CI before activation"). Needs docker and a Python with
# jinja2 + PyYAML (the dev install). Used by CI and `make mta-test`.
#
#   1. renders mta/ with the role defaults + mta/test/vars.yml
#   2. boots it in a throwaway Debian container (postfix check, opendkim -n)
#   3. an outside client cannot relay; mail to the bounce domain is accepted
#   4. the app network can submit; the message is DKIM-signed and routed to
#      the marketing stream transport
#   5. a bounce is piped through forward-bounce to a signed HTTP intake
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3}
NET=mta-test-net
IMG=email-platform-mta-test
NAME=mta-test
MTA_IP=10.250.0.10
OUTSIDE_IP=10.250.0.20
APP_IP=10.250.0.130
BOUNCE_DOMAIN=bounce.news.example.test
SECRET=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')
WORK=$(mktemp -d)

# Git Bash on Windows rewrites /paths in arguments; containers need them as-is.
dk() { MSYS_NO_PATHCONV=1 docker "$@"; }
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1" 2>/dev/null || echo "$1"; else echo "$1"; fi; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then echo "--- MTA log:"; docker logs "$NAME" 2>&1 | tail -40 || true; fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    exit "$status"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*"; exit 1; }
step() { echo; echo "== $*"; }

step "render"
"$PYTHON" "$ROOT/scripts/render_mta.py" --vars "$ROOT/mta/test/vars.yml" --out "$WORK/etc" >/dev/null
cp -r "$ROOT/mta/bin" "$WORK/bin"
mkdir "$WORK/test"
cp "$ROOT/mta/test/entrypoint.sh" "$ROOT/mta/test/intake_stub.py" "$ROOT/mta/test/smtp_probe.py" \
   "$ROOT/scripts/relay_test.py" "$WORK/test/"

step "build test image"
docker build -q -t "$IMG" -f "$(native "$ROOT/mta/test/Dockerfile")" "$(native "$WORK")" >/dev/null

step "boot (postfix check, opendkim -n)"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --subnet 10.250.0.0/24 "$NET" >/dev/null
dk run -d --name "$NAME" --hostname mta-test.example.test --network "$NET" --ip "$MTA_IP" \
    -e TEST_INGEST_SECRET="$SECRET" "$IMG" >/dev/null
i=0
until docker logs "$NAME" 2>&1 | grep -q "postfix/master.*daemon started\|daemon started"; do
    i=$((i + 1)); [ "$i" -gt 60 ] && fail "Postfix did not start"
    docker ps -q -f name="^$NAME$" | grep -q . || fail "the MTA container exited (config check failed?)"
    sleep 1
done
docker logs "$NAME" 2>&1 | grep -q "configuration checks passed" || fail "config checks did not run"
echo "ok"

client() { ip=$1; shift; dk run --rm --network "$NET" --ip "$ip" --entrypoint python3 "$IMG" "$@"; }

step "open-relay test from outside the app network"
client "$OUTSIDE_IP" /opt/test/relay_test.py --host "$MTA_IP" --bounce-domain "$BOUNCE_DOMAIN" \
    --helo outside.example.test || fail "relay test did not pass"

step "the app network can submit; mail is signed and on the marketing transport"
client "$APP_IP" /opt/test/smtp_probe.py submit "$MTA_IP" | grep -q "submit: accepted" || fail "submission refused"
i=0
until dk exec "$NAME" sh -c 'ls /tmp/captured/*.eml >/dev/null 2>&1'; do
    i=$((i + 1)); [ "$i" -gt 30 ] && fail "the submitted message never left the MTA"
    sleep 1
done
# What actually left the MTA (the sink is the marketing stream's next hop).
dk exec "$NAME" sh -c 'cat /tmp/captured/*.eml' | tr -d '\r' > "$WORK/sent.eml"
grep -q "^DKIM-Signature:" "$WORK/sent.eml" || fail "the delivered message has no DKIM-Signature"
tr -d '\n\t ' < "$WORK/sent.eml" | grep -q "d=news.example.test;" \
    || fail "the DKIM signature is not for news.example.test"
echo "ok: delivered message is DKIM-signed d=news.example.test"
docker logs "$NAME" 2>&1 | grep -q "postfix-marketing/smtp\[[0-9]*\]: .*to=<someone@example.test>.*status=sent" \
    || fail "the message was not sent by the marketing stream transport"
echo "ok: sent by postfix-marketing/smtp"

step "a bounce is piped to the signed intake"
client "$OUTSIDE_IP" /opt/test/smtp_probe.py bounce "$MTA_IP" | grep -q "bounce: accepted" || fail "bounce refused"
i=0
until dk exec "$NAME" sh -c 'grep -q "signature=ok" /tmp/intake.log 2>/dev/null'; do
    i=$((i + 1)); [ "$i" -gt 30 ] && fail "the bounce never reached the intake"
    sleep 1
done
dk exec "$NAME" grep -q "^X-Original-To: b-0123abcd@$BOUNCE_DOMAIN" /tmp/intake.log \
    || fail "the intake did not get the VERP address (X-Original-To)"
dk exec "$NAME" sh -c '! grep -q "signature=BAD" /tmp/intake.log' || fail "a request had a bad signature"
echo "ok: bounce reached the intake with a valid signature and its VERP address"

echo
echo "MTA checks passed."
