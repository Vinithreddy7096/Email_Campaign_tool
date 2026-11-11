#!/usr/bin/env python
"""Django management entrypoint."""

import os
import sys
from pathlib import Path


def main() -> None:
    # The repo root holds top-level apps too (email_templates), and workers/.
    # The Dockerfile puts it on PYTHONPATH; this does the same when run directly.
    sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Django is not importable. Activate your virtualenv or run inside the "
            "container (`make shell`)."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
