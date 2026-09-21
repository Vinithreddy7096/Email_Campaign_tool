#!/usr/bin/env python
"""
Open-relay assertion against an MTA host (§5.4 `make relay-test`, spec §12).

Not yet implemented. Belongs with the MTA phase. Exits non-zero: a missing
open-relay test must never read as "not an open relay".
"""

import sys

sys.exit(
    "relay_test.py is not implemented yet — it belongs to the MTA phase.\n"
    "Do NOT interpret this as an open-relay pass. See docs/OPEN_QUESTIONS.md."
)
