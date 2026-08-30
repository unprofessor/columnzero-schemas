"""The immutability verdict, taken against git rather than against either tree.

A record kept *inside* a tree is editable by whoever edits the tree: drop the field an
index is checked on and the check disappears with it, and a release deleted along with
the index that listed it leaves nothing behind to notice.  The previous commit is not
reachable from the working copy, so the same edit cannot scrub the evidence.

This reads a `git diff --name-status` on stdin and reports the paths that are not
additive.  It parses no file and trusts nothing on disk.
"""

from __future__ import annotations

import re


# Everything below a release directory on gh-pages is canonical.
CANONICAL_PATH = re.compile(r"^[^/]+/v[^/]+/[^/]+/")
# Every lock in the manifest tree on main is append-only, for the same reason.
MANIFEST_PATH = re.compile(r"^manifest/")
TREES = {"site": CANONICAL_PATH, "manifest": MANIFEST_PATH}

# The only git statuses that leave every existing byte where it was: a file that did not
# exist before (A), and a copy, whose source survives untouched (C).  Everything else is
# a violation, including letters git has yet to invent.
#
# The rule is an allowlist on purpose.  Naming the bad statuses instead would mean the
# check is only as complete as that list: `T`, a regular file swapped for a symlink, was
# missing from exactly such a list, and it leaves every index byte-identical.
ADDITIVE_STATUSES = frozenset("AC")


def violations(name_status: str, tree: str = "site") -> list[str]:
    """Paths a `git diff --name-status` reports as anything but additive."""
    pattern = TREES[tree]
    found: list[str] = []
    for line in name_status.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        kind = fields[0][:1]
        # Renames and copies list the source first, and for a rename it is the path that
        # disappears; for everything else there is only one path.
        if kind not in ADDITIVE_STATUSES and pattern.match(fields[1]):
            found.append(f"{kind} {fields[1]}")
    return found
