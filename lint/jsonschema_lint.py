#!/usr/bin/env python3
"""Admission linter for JSON Schema documents.

Invoked by the publisher as `jsonschema_lint.py <file> <canonical-url>`; exits 0 to
accept, non-zero with diagnostics on stderr to refuse.  Nothing about that contract is
Python-specific -- a CDDL linter can be a Rust binary invoked the same way.

Two of these checks are quality control and one is not.  `$id` must equal the URL the
document is about to be published at, because `$id` is the base for `$ref` resolution:
a schema filed at the wrong path resolves its references wrong, silently, in a way the
digest cannot catch.  That check protects the URL space.  The meta-schema check protects
only the document itself, which is why it lives out here in a linter rather than in the
registry, and why it runs once at admission rather than on every rebuild.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def check(path: Path, canonical_url: str) -> list[str]:
    try:
        import jsonschema
        from jsonschema.validators import validator_for
    except ImportError:
        return ["jsonschema is not installed (pip install '.[lint]')"]

    try:
        value = json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        return [f"invalid JSON: {error}"]
    if not isinstance(value, dict):
        return ["schema root must be an object"]

    problems: list[str] = []
    dialect = value.get("$schema")
    if not isinstance(dialect, str):
        problems.append("no $schema declared")
    validator = validator_for(value, default=None)
    if validator is None:
        problems.append(f"unsupported dialect: {dialect!r}")
    else:
        try:
            validator.check_schema(value)
        except jsonschema.exceptions.SchemaError as error:
            problems.append(f"fails declared meta-schema: {error.message}")
    if value.get("$id") != canonical_url:
        problems.append(f"$id must equal {canonical_url}, got {value.get('$id')!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {Path(argv[0]).name} <file> <canonical-url>", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]), argv[2])
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
