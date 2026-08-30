"""Admission linting: pluggable, external, and applied only to what is being admitted.

Linting is not part of the registry's guarantee.  The digest pins the bytes and git
enforces immutability; a schema that fails its own meta-schema damages only itself, and
the remedy -- a new release -- is the one the publisher already has.  What linting buys
is catching that before the URL is spent, which is worth having and worth keeping *out*
of the steady state.

So a linter runs once, at admission, on new artifacts only.  A rule added later never
re-applies to frozen bytes, which is what stops a future validator release from failing
a nightly run over a schema published years earlier.

Each linter is an external command, chosen by file suffix and invoked as:

    <argv...> <file> <canonical-url>

Exit 0 accepts.  Anything else rejects, and stderr is reported.  Nothing about the
contract is language-specific: a CDDL linter can be a Rust binary without the publisher
knowing or caring.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .model import SCHEMA_SUFFIXES, SchemaDocument, ValidationError


LINT_TIMEOUT_SECONDS = 60


def load(config: object) -> dict[str, list[str]]:
    """Read the `[lint]` table: a suffix mapped to the argv that checks it."""
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValidationError("manifest [lint] must be a table of suffix -> command")
    linters: dict[str, list[str]] = {}
    for suffix, argv in config.items():
        if suffix not in SCHEMA_SUFFIXES:
            known = ", ".join(sorted(SCHEMA_SUFFIXES))
            raise ValidationError(f"[lint] key {suffix!r} is not a known suffix ({known})")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValidationError(f"[lint] {suffix!r} must be a non-empty array of strings")
        linters[suffix] = list(argv)
    return linters


def linter_for(linters: dict[str, list[str]], basename: str) -> list[str] | None:
    for suffix, argv in linters.items():
        if basename.endswith(suffix):
            return argv
    return None


def check(
    linters: dict[str, list[str]],
    documents: list[SchemaDocument],
    base_url: str,
    root: Path | None = None,
) -> list[str]:
    """Lint every document that has a linter.  Returns diagnostics, empty when clean."""
    diagnostics: list[str] = []
    with tempfile.TemporaryDirectory() as staging:
        for document in documents:
            argv = linter_for(linters, document.basename)
            if argv is None:
                continue
            target = Path(staging) / document.basename
            target.write_bytes(document.body)
            url = document.key.url(base_url, document.basename)
            try:
                result = subprocess.run(
                    [*argv, str(target), url],
                    capture_output=True,
                    text=True,
                    timeout=LINT_TIMEOUT_SECONDS,
                    cwd=root or Path.cwd(),
                    check=False,
                )
            except FileNotFoundError as error:
                raise ValidationError(f"linter not found: {argv[0]!r}") from error
            except subprocess.TimeoutExpired as error:
                raise ValidationError(f"linter timed out on {document.basename}") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
                diagnostics.append(f"{document.key} {document.basename}: {detail}")
    return diagnostics


def unlinted(linters: dict[str, list[str]], documents: list[SchemaDocument]) -> list[str]:
    """Documents with no configured linter, so a plan can say so rather than imply a pass."""
    return sorted(
        {document.basename for document in documents if linter_for(linters, document.basename) is None}
    )
