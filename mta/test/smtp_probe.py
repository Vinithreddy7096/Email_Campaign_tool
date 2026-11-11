"""Send one test message to the MTA under test (mta/test/run.sh).

python3 smtp_probe.py submit HOST   # as the app: VERP sender, From news.example.test
python3 smtp_probe.py bounce HOST   # as a remote MTA: a DSN to our bounce domain
"""

import smtplib
import sys
from email.message import EmailMessage

kind, host = sys.argv[1], sys.argv[2]
msg = EmailMessage()
if kind == "submit":
    sender, rcpt = "b-0123abcd@bounce.news.example.test", "someone@example.test"
    msg["From"] = "News <news@news.example.test>"
    msg["To"] = rcpt
    msg["Subject"] = "DKIM probe"
    msg["Message-ID"] = "<probe-submit@news.example.test>"
    msg.set_content("If this is signed, OpenDKIM is wired in.\n")
else:
    sender, rcpt = "", "b-0123abcd@bounce.news.example.test"
    msg["From"] = "MAILER-DAEMON@remote.example.org"
    msg["To"] = rcpt
    msg["Subject"] = "Undelivered Mail Returned to Sender"
    msg["Message-ID"] = "<probe-bounce@remote.example.org>"
    msg.set_content("Final-Recipient: rfc822; gone@example.org\nStatus: 5.1.1\n")

with smtplib.SMTP(host, 25, timeout=20) as smtp:
    smtp.ehlo("probe.example.test")
    refused = smtp.sendmail(sender, [rcpt], msg.as_bytes())
print(f"{kind}: accepted" if not refused else f"{kind}: refused {refused}")
