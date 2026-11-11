#!/bin/sh
# Apply playbooks/data.yml (common, postgres, redis, rabbitmq) to throwaway
# systemd containers, then prove it works:
#   - a second run changes nothing (idempotent)
#   - the app network can reach Postgres (TLS + password), Redis (password
#     only), RabbitMQ (the app user); the firewall drops everything else
#   - POINT-IN-TIME RECOVERY: rows written after a chosen moment are gone
#     after restoring to it, rows before it survive
#   - backup / WAL-archive metrics are written for node_exporter
# Needs docker (and internet for packages). Used by CI and `make infra-test`.
set -eu

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
NET=infra-test-net
HOSTS="db1 cache1 mq1"
HOST_IMG=email-platform-test-host
RUNNER_IMG=email-platform-ansible-runner
CERTS=infra-test-minio-certs
WORK=$(mktemp -d)

dk() { MSYS_NO_PATHCONV=1 docker "$@"; }
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1" 2>/dev/null || echo "$1"; else echo "$1"; fi; }
rand() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }
fail() { echo "FAIL: $*"; exit 1; }
step() { echo; echo "== $*"; }

cleanup() {
    status=$?
    docker rm -f $HOSTS minio >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker volume rm "$CERTS" >/dev/null 2>&1 || true
    rm -rf "$WORK"
    exit "$status"
}
trap cleanup EXIT INT TERM

step "images"
docker build -q -t "$HOST_IMG" -f "$(native "$ROOT/infrastructure/ansible/test/host.Dockerfile")" \
    "$(native "$ROOT/infrastructure/ansible/test")" >/dev/null
printf '%s\n' 'FROM python:3.11-slim' \
    'RUN pip install --no-cache-dir "ansible-core==2.17.5" "docker==7.1.0" \' \
    ' && ansible-galaxy collection install "community.docker:==3.13.1"' \
    | docker build -q -t "$RUNNER_IMG" - >/dev/null
echo ok

step "network, TLS MinIO (pgBackRest talks to S3 over HTTPS only)"
docker rm -f $HOSTS minio >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --subnet 10.251.0.0/24 "$NET" >/dev/null
S3_SECRET=$(rand)
dk run --rm -v "$CERTS:/certs" --entrypoint sh "$HOST_IMG" -c \
    'openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=minio -addext subjectAltName=DNS:minio \
       -keyout /certs/private.key -out /certs/public.crt 2>/dev/null'
dk run -d --name minio --network "$NET" --ip 10.251.0.5 -v "$CERTS:/root/.minio/certs" \
    -e MINIO_ROOT_USER=pitrtest -e MINIO_ROOT_PASSWORD="$S3_SECRET" \
    minio/minio:RELEASE.2024-10-13T13-34-11Z server /data >/dev/null
i=0
until dk run --rm --network "$NET" --entrypoint sh minio/mc:RELEASE.2024-10-08T09-37-26Z -c \
    "mc alias set --insecure t https://minio:9000 pitrtest $S3_SECRET >/dev/null && mc mb --insecure --ignore-existing t/pitr >/dev/null" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -gt 30 ] && fail "MinIO did not come up"; sleep 2
done
echo ok

step "boot hosts (systemd)"
n=10
for h in $HOSTS; do
    n=$((n + 1))
    dk run -d --name "$h" --hostname "$h" --network "$NET" --ip "10.251.0.$n" \
        --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
        "$HOST_IMG" >/dev/null
done
sleep 5
echo ok

# Test-only secrets, fresh every run.
APP_PW=$(rand); REDIS_PW=$(rand); RABBIT_PW=$(rand)
cat > "$WORK/secrets.json" <<EOF
{"vault_postgres_app_password": "$APP_PW", "vault_postgres_backup_password": "$(rand)",
 "vault_pgbackrest_s3_key": "pitrtest", "vault_pgbackrest_s3_key_secret": "$S3_SECRET",
 "vault_pgbackrest_cipher_pass": "$(rand)$(rand)",
 "vault_redis_password": "$REDIS_PW", "vault_rabbitmq_password": "$RABBIT_PW"}
EOF

play() {
    dk run --rm -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$(native "$ROOT"):/repo:ro" -v "$(native "$WORK"):/work" \
        -e ANSIBLE_CONFIG=/repo/infrastructure/ansible/ansible.cfg -e ANSIBLE_FORCE_COLOR=0 \
        -w /repo/infrastructure/ansible "$RUNNER_IMG" \
        ansible-playbook -i test/inventory.yml playbooks/data.yml -e @/work/secrets.json "$@"
}

step "apply playbooks/data.yml"
play > "$WORK/run1.log" 2>&1 || { tail -60 "$WORK/run1.log"; fail "first run failed"; }
grep -A10 "PLAY RECAP" "$WORK/run1.log"

step "second run changes nothing"
play > "$WORK/run2.log" 2>&1 || { tail -60 "$WORK/run2.log"; fail "second run failed"; }
grep -A10 "PLAY RECAP" "$WORK/run2.log" | grep -E "changed=[1-9]" && {
    grep -B2 -E "^changed:" "$WORK/run2.log" | head -40; fail "the second run changed something"; }
echo ok: idempotent

step "the app network reaches the services"
dk run --rm --network "$NET" --ip 10.251.0.50 -e PGPASSWORD="$APP_PW" postgres:16-bookworm \
    psql "host=db1 dbname=mailer user=mailer sslmode=require" -Atc "select 'postgres ok over ' || (select ssl::text from pg_stat_ssl where pid = pg_backend_pid())" \
    | grep -q "postgres ok over true" || fail "postgres over TLS with the app password"
echo "ok: postgres (TLS, scram)"
dk run --rm --network "$NET" --ip 10.251.0.50 -e REDISCLI_AUTH="$REDIS_PW" redis:7-alpine \
    redis-cli -h cache1 ping | grep -q PONG || fail "redis with the password"
dk run --rm --network "$NET" --ip 10.251.0.50 redis:7-alpine redis-cli -h cache1 ping 2>&1 | grep -q NOAUTH \
    || fail "redis without a password must be refused"
echo "ok: redis (password required)"
dk exec mq1 rabbitmqctl -q authenticate_user mailer "$RABBIT_PW" >/dev/null || fail "rabbitmq app user"
dk exec mq1 rabbitmqctl -q list_users | grep -q guest && fail "the guest account still exists"
echo "ok: rabbitmq (app user, no guest)"
dk run --rm --network "$NET" --ip 10.251.0.50 --entrypoint bash postgres:16-bookworm \
    -c 'timeout 3 bash -c "</dev/tcp/mq1/25672"' 2>/dev/null && fail "firewall: erlang distribution port is open"
echo "ok: firewall drops unlisted ports"

step "point-in-time recovery"
q() { dk exec -u postgres db1 psql -X -d mailer -Atc "$1"; }
q "CREATE TABLE pitr_probe (label text, at timestamptz DEFAULT clock_timestamp())" >/dev/null
q "INSERT INTO pitr_probe(label) VALUES ('before')" >/dev/null
sleep 2
TARGET=$(q "SELECT clock_timestamp()")
sleep 2
q "INSERT INTO pitr_probe(label) VALUES ('after')" >/dev/null
q "SELECT pg_switch_wal()" >/dev/null
dk exec -u postgres db1 pgbackrest --stanza=mailer check >/dev/null || fail "WAL archive check"
echo "restoring to $TARGET"
dk exec db1 systemctl stop postgresql@16-main
dk exec -u postgres db1 pgbackrest --stanza=mailer --delta --type=time "--target=$TARGET" \
    --target-action=promote restore > "$WORK/restore.log" 2>&1 || { tail -20 "$WORK/restore.log"; fail "restore"; }
dk exec db1 systemctl start postgresql@16-main
i=0
until q "SELECT 1" >/dev/null 2>&1; do i=$((i + 1)); [ "$i" -gt 30 ] && fail "postgres did not come back"; sleep 2; done
ROWS=$(q "SELECT string_agg(label, ',' ORDER BY at) FROM pitr_probe")
[ "$ROWS" = "before" ] || fail "after restoring to $TARGET the table holds '$ROWS' (want 'before')"
echo "ok: restored to $TARGET — 'before' kept, 'after' gone"

step "backup and archive metrics"
dk exec -u postgres db1 env PGBACKREST_STANZA=mailer /usr/local/bin/pgbackrest-metrics
dk exec db1 cat /var/lib/node_exporter/textfile/pgbackrest.prom > "$WORK/metrics.prom"
grep -q '^pgbackrest_last_backup_timestamp_seconds{type="full"}' "$WORK/metrics.prom" || fail "no full-backup metric"
grep -q '^pgbackrest_info_ok 1' "$WORK/metrics.prom" || fail "pgbackrest info not ok"
grep -q '^pg_wal_archive_last_success_timestamp_seconds [1-9]' "$WORK/metrics.prom" || fail "no WAL archive metric"
dk exec db1 curl -fsS localhost:9100/metrics | grep -q '^pgbackrest_info_ok 1' || fail "node_exporter does not serve it"
echo "ok: node_exporter serves pgbackrest_* and pg_wal_archive_*"

echo
echo "Infrastructure checks passed."
