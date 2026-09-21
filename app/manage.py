#!/usr/bin/env python
"""Django management entrypoint."""

import os
import sys


def main() -> None:
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
