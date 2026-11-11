#!/usr/bin/env python
"""
Render the mta/ templates the way the Ansible roles do, for CI and local checks.

    python scripts/render_mta.py --vars mta/test/vars.yml --out build/mta

Variables come from the postfix and opendkim role defaults, then each --vars
file in order (later wins) — the same precedence Ansible gives group_vars over
role defaults. Undefined variables are an error, so a template that would
render differently under Ansible fails here first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "infrastructure" / "ansible" / "roles"
OUTPUTS = {
    "postfix/main.cf.j2": "postfix/main.cf",
    "postfix/master.cf.j2": "postfix/master.cf",
    "postfix/transport.j2": "postfix/transport",
    "postfix/sender_transport.j2": "postfix/sender_transport",
    "opendkim/opendkim.conf.j2": "opendkim/opendkim.conf",
    "opendkim/KeyTable.j2": "opendkim/KeyTable",
    "opendkim/SigningTable.j2": "opendkim/SigningTable",
    "opendkim/TrustedHosts.j2": "opendkim/TrustedHosts",
}
# Role defaults that only make sense inside Ansible.
ANSIBLE_ONLY = {"mta_source_dir", "mta_ingest_secret", "opendkim_private_keys"}


def load_vars(extra: list[Path]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path in [ROLES / "postfix/defaults/main.yml", ROLES / "opendkim/defaults/main.yml", *extra]:
        values.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return {k: v for k, v in values.items() if k not in ANSIBLE_ONLY}


def render(values: dict[str, Any], out: Path) -> list[Path]:
    env = jinja2.Environment(  # noqa: S701 — config files, not HTML
        loader=jinja2.FileSystemLoader(ROOT / "mta"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,  # Ansible's template module default
        keep_trailing_newline=True,
    )
    written = []
    for src, dest in OUTPUTS.items():
        target = out / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(env.get_template(src).render(**values), encoding="utf-8", newline="\n")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the MTA templates.")
    parser.add_argument("--vars", type=Path, action="append", default=[], help="YAML vars file")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        written = render(load_vars(args.vars), args.out)
    except jinja2.TemplateError as exc:
        print(f"render_mta: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
