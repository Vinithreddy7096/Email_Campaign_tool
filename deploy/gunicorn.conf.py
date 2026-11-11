"""Gunicorn settings for the web container (staging/production).

Every value can be overridden by an environment variable of the same name in
upper case with a GUNICORN_ prefix, e.g. GUNICORN_WORKERS=6.
"""

import multiprocessing
import os


def _env(name: str, default: str) -> str:
    return os.environ.get(f"GUNICORN_{name.upper()}", default)


bind = _env("bind", "0.0.0.0:8000")

# Sync-style threaded workers: the app is ordinary blocking Django + Postgres.
worker_class = "gthread"
workers = int(_env("workers", str(min(2 * multiprocessing.cpu_count() + 1, 9))))
threads = int(_env("threads", "4"))

# A request slower than this is a bug or an outage, not something to wait for.
timeout = int(_env("timeout", "30"))
graceful_timeout = int(_env("graceful_timeout", "30"))
keepalive = int(_env("keepalive", "5"))

# Recycle workers now and then so a slow leak never becomes an outage.
max_requests = int(_env("max_requests", "2000"))
max_requests_jitter = int(_env("max_requests_jitter", "200"))

# Heartbeat files in RAM, not on the container's overlay filesystem.
worker_tmp_dir = "/dev/shm"  # noqa: S108 — RAM-backed, gunicorn-owned
# No runtime control socket (gunicorn ≥ 25): the root filesystem is read-only
# and a release is a container restart, not a live reconfiguration.
control_socket_disable = True

# Request limits (the API accepts at most 64 KB of merge data; bounces ≤ 2.5 MB).
limit_request_line = 8190
limit_request_fields = 100

# The reverse proxy is the only client. It sets X-Forwarded-For/-Proto and
# must strip any client-supplied value. Restrict to its address in production.
forwarded_allow_ips = _env("forwarded_allow_ips", "127.0.0.1")

# Logs to stdout/stderr for the container runtime to collect. Access log as
# JSON so Loki can parse it. Credentials travel in headers, never in query
# strings, so the logged path holds none.
accesslog = "-"
errorlog = "-"
loglevel = _env("loglevel", "info")
access_log_format = (
    '{"t":"%(t)s","remote":"%({x-forwarded-for}i)s","method":"%(m)s","path":"%(U)s",'
    '"status":%(s)s,"bytes":%(B)s,"ms":%(M)s,"ua":"%(a)s"}'
)

proc_name = "email-platform-web"
