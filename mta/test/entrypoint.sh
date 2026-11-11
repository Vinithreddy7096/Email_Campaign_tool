#!/bin/sh
# Test MTA start-up: throwaway DKIM keys, maps, config checks, then Postfix in
# the foreground. Any failed check stops the container (and the CI job).
set -eu

# Throwaway 2048-bit keys for every KeyTable entry (real keys come from the vault).
grep -v '^#' /etc/opendkim/KeyTable | grep . | while read -r _name spec; do
    domain=${spec%%:*}; rest=${spec#*:}; selector=${rest%%:*}
    mkdir -p "/etc/opendkim/keys/$domain"
    opendkim-genkey -b 2048 -d "$domain" -s "$selector" -D "/etc/opendkim/keys/$domain"
done
chown -R opendkim:opendkim /etc/opendkim/keys
chmod -R go-rwx /etc/opendkim/keys
chgrp opendkim /etc/opendkim/KeyTable /etc/opendkim/SigningTable /etc/opendkim/TrustedHosts
mkdir -p /run/opendkim && chown opendkim:opendkim /run/opendkim

# Intake config for forward-bounce, pointing at the stub below.
mkdir -p /etc/email-platform
printf 'INTAKE_URL=http://127.0.0.1:8099\nMTA_INGEST_SECRET=%s\n' "$TEST_INGEST_SECRET" \
    > /etc/email-platform/mta-intake.env
chgrp mtaintake /etc/email-platform/mta-intake.env && chmod 0640 /etc/email-platform/mta-intake.env

python3 /opt/test/intake_stub.py &

# Container-only: log to stdout, and give chrooted daemons what the Debian init
# script would have copied in.
postconf -e maillog_file=/dev/stdout
mkdir -p /var/spool/postfix/etc
cp /etc/resolv.conf /etc/services /etc/hosts /etc/nsswitch.conf /var/spool/postfix/etc/ 2>/dev/null || true

postmap /etc/postfix/transport /etc/postfix/sender_transport
newaliases
opendkim -n -x /etc/opendkim.conf
postfix check
echo "entrypoint: configuration checks passed"

opendkim -x /etc/opendkim.conf
exec postfix start-fg
