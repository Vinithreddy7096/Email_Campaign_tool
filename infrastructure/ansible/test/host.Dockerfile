# A throwaway "server" for testing the Ansible roles: Debian 12 booting systemd.
# Used only by infrastructure/ansible/test/run.sh — never in production.
FROM debian:bookworm
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        systemd systemd-sysv python3 sudo ca-certificates curl gnupg openssl iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    # Units that make no sense in a container.
    && systemctl mask systemd-firstboot.service getty.target console-getty.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
