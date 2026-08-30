#!/usr/bin/env python3
"""Build a sample publication tree so the HTML pages can be looked at while editing them.

Markup is the one part of the registry with no correct answer to assert, so it needs an
eye rather than a test.  This exists to put a realistic tree in front of one.

It is deliberately not a `czschemas` subcommand.  The CLI's three verbs are a contract --
`plan` writes nothing, `apply` reconciles, `verify` judges a diff -- and a fixture
generator that invents releases belongs nowhere near a published entry point.

The releases are synthesised in memory through the test harness's stubbed downloader, so
this touches no network and needs no published artifact.  Reusing the harness rather than
growing a second fixture builder is the point: there is one way to fabricate a release in
this repository, and if it drifts, this drifts with it.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# The harness lives in the tests, which are not a package; the test module bootstraps
# `src` onto the path the same way.
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

from test_czschemas import Registry, key  # noqa: E402

# Chosen to exercise the cases that are easy to get wrong in markup, not to look tidy:
# a 0.x line beside an integer one, a prerelease that must appear without moving an
# alias, and `epic` dropped in 2.0.0 -- so `latest/` serves it from 1.5.0 while serving
# `planr` from 2.0.0, and the two rows on one page disagree about their version.
PLANR = "planr.schema.json"
EPIC = "epic.schema.json"
FIXTURE: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "planr": [
        ("1.4.2", (PLANR,)),
        ("1.5.0", (PLANR, EPIC)),
        ("1.6.0-rc.1", (PLANR, EPIC)),
        ("2.0.0", (PLANR,)),
    ],
    "columnzero-schemas": [
        ("0.9.0", ("schema-index.schema.json",)),
        ("1.0.0", ("schema-index.schema.json",)),
    ],
}


def build(out: Path) -> list[str]:
    """Reconcile the fixture into `out`, and report the pages it produced."""
    scratch = Path(tempfile.mkdtemp(prefix="devsite-"))
    try:
        # The manifest tree goes to the scratch directory on purpose: `MANIFEST_ROOT` is
        # a relative "manifest", so a registry rooted at the repo would declare synthetic
        # locks into the real one.
        registry = Registry(scratch)
        for project, releases in FIXTURE.items():
            for version, names in releases:
                registry.publish(key(version, project), names)
        registry.apply()

        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(registry.site, out)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return sorted(page.relative_to(out).as_posix() for page in out.rglob("index.html"))


def serve(out: Path, port: int) -> None:
    """Serve `out` the way Pages does -- almost.

    `SimpleHTTPRequestHandler` resolves a directory URL to its `index.html`, which is the
    behaviour worth reproducing.  Where it differs: a directory with no `index.html` gets
    a generated listing here and a 404 on Pages, so a release URL looks reachable locally
    when it is not.  Nothing links to one, which is why the difference stays harmless.
    """
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {out} at http://127.0.0.1:{port}/  (ctrl-c to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # `site/` is gitignored and is what the workflow builds into, so it is the default.
    parser.add_argument("--out", type=Path, default=REPO / "site",
                        help="where to write the tree (default: ./site, gitignored)")
    parser.add_argument("--serve", action="store_true", help="serve the tree after building")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    pages = build(args.out)
    print(args.out)
    for page in pages:
        print(f"  {page}")
    if args.serve:
        serve(args.out, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
