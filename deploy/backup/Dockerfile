# Backup runner: Postgres 16 client tools (pg_dump must match the server's
# major version) + Python and boto3 for S3. No application code.
#
#   docker build -t email-platform-backup -f deploy/backup/Dockerfile .
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-boto3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10002 backup-runner

COPY scripts/backup/pgbackup.py /usr/local/bin/pgbackup
RUN chmod 0755 /usr/local/bin/pgbackup

USER backup-runner
WORKDIR /home/backup-runner
ENTRYPOINT ["python3", "/usr/local/bin/pgbackup"]
CMD ["backup"]
