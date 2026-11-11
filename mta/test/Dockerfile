# Throwaway Postfix + OpenDKIM for CI and local checks ONLY (mta/test/run.sh).
# Production MTAs are not containerised (spec §13); they are built by
# infrastructure/ansible from the same templates.
FROM debian:bookworm-slim

RUN echo "postfix postfix/main_mailer_type select No configuration" | debconf-set-selections \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        postfix opendkim opendkim-tools ssl-cert ca-certificates python3 procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system mtaintake \
    && useradd --system --no-create-home --shell /usr/sbin/nologin -G mtaintake mtaforward

COPY etc/postfix/ /etc/postfix/
COPY etc/opendkim/opendkim.conf /etc/opendkim.conf
COPY etc/opendkim/KeyTable etc/opendkim/SigningTable etc/opendkim/TrustedHosts /etc/opendkim/
COPY bin/ /usr/local/bin/
COPY test/ /opt/test/
RUN chmod 0755 /usr/local/bin/* /opt/test/*.sh

EXPOSE 25
ENTRYPOINT ["/opt/test/entrypoint.sh"]
