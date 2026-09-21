#!/usr/bin/env python
"""
SPF / DKIM / DMARC / PTR / MX checker (§5.4 `make dns-check`).

Not yet implemented. Belongs with domain onboarding. Exits non-zero so a green
run can never be mistaken for a passing DNS posture.
"""

import sys

sys.exit(
    "validate_dns.py is not implemented yet — it belongs to the domain-onboarding phase.\n"
    "Do NOT interpret this as a DNS pass. See docs/OPEN_QUESTIONS.md."
)
