"""Reconcile the manifest tree against the publication tree.

`main:manifest/{project}/v{compat}/{version}/artifact.lock` declares intent.
`gh-pages:{project}/v{compat}/{version}/` is what exists.  Strip one path segment and
the two correspond exactly, so the set difference between them *is* the work queue --
there is no separate ledger to drift from either one.

Five states, four of them terminal:

  in both, locks equal      steady state, nothing to do
  declared, not published   admit: fetch, lint, publish
  published, no lock        backfill: re-admit at the pinned digest to record provenance
  published, not declared   halt -- a redaction is in progress, or something was lost
  locks disagree            halt -- one of the two trees has been tampered with

The reconciler never deletes.  Removing a release is a human action in both trees, and
the halt above is what keeps a half-finished removal loud instead of silent.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable

from . import fetch, lint, registry
from .config import Config
from .model import ArtifactLock, ReleaseKey, SchemaDocument, ValidationError


MANIFEST_ROOT = "manifest"
LOCK_NAME = registry.LOCK_NAME


@dataclasses.dataclass(frozen=True)
class Declaration:
    key: ReleaseKey
    lock: ArtifactLock
    body: bytes          # verbatim, so the published copy can be compared byte for byte


@dataclasses.dataclass(frozen=True)
class Plan:
    admit: tuple[ReleaseKey, ...]
    backfill: frozenset[ReleaseKey]
    halt: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.admit and not self.halt


@dataclasses.dataclass(frozen=True)
class Admission:
    """What admitting one release did, for the plan and apply reports."""

    key: ReleaseKey
    tag: str
    documents: tuple[SchemaDocument, ...]
    unlinted: tuple[str, ...]
    is_backfill: bool


def declared(manifest_root: Path) -> dict[ReleaseKey, Declaration]:
    """Intent: every lock in the manifest tree, keyed by the path it was found at."""
    root = manifest_root / MANIFEST_ROOT
    found: dict[ReleaseKey, Declaration] = {}
    if not root.is_dir():
        return found
    for lock_path in sorted(root.glob(f"*/v*/*/{LOCK_NAME}")):
        relative = lock_path.relative_to(root).parent.as_posix()
        key = ReleaseKey.from_path(relative)
        body = lock_path.read_bytes()
        lock = ArtifactLock.parse(body, f"{MANIFEST_ROOT}/{relative}/{LOCK_NAME}")
        fetch.validate_lock_url(lock.url)
        found[key] = Declaration(key, lock, body)
    stray = sorted(
        path.relative_to(manifest_root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != LOCK_NAME
    )
    if stray:
        raise ValidationError(
            "manifest tree may contain only artifact.lock files: " + ", ".join(stray)
        )
    return found


def plan(
    declarations: dict[ReleaseKey, Declaration],
    published: dict[ReleaseKey, registry.PublishedRelease],
) -> Plan:
    """Compare the two trees.  Reads nothing but what the callers already hold."""
    admit: list[ReleaseKey] = []
    backfill: set[ReleaseKey] = set()
    halt: list[str] = []
    for key in sorted(set(declarations) | set(published), key=lambda key: key.sort_key):
        # `key not in published` and `published[key].lock is None` are different states:
        # the first is a release to publish, the second is one to record provenance for.
        # Collapsing them would turn every pre-lock release into a phantom admission.
        if key not in published:
            admit.append(key)
        elif key not in declarations:
            halt.append(f"published but not declared: {key.path}")
        elif not published[key].is_complete:
            admit.append(key)
            backfill.add(key)
        elif published[key].lock != declarations[key].body:
            halt.append(f"lock differs between main and gh-pages: {key.path}/{LOCK_NAME}")
    return Plan(tuple(admit), frozenset(backfill), tuple(halt))


def admit(
    keys: tuple[ReleaseKey, ...],
    declarations: dict[ReleaseKey, Declaration],
    config: Config,
    backfill: frozenset[ReleaseKey],
    on_download: Callable[[ArtifactLock], bytes] | None = None,
    lint_root: Path | None = None,
) -> list[Admission]:
    """Fetch, verify, and lint each admission.  Writes nothing."""
    admissions: list[Admission] = []
    diagnostics: list[str] = []
    for key in keys:
        declaration = declarations[key]
        payload = (on_download or fetch.download)(declaration.lock)
        documents = fetch.verify(key, declaration.lock, payload)
        diagnostics.extend(lint.check(config.linters, documents, config.base_url, lint_root))
        admissions.append(
            Admission(
                key=key,
                tag=fetch.release_tag(declaration.lock.url),
                documents=tuple(documents),
                unlinted=tuple(lint.unlinted(config.linters, documents)),
                is_backfill=key in backfill,
            )
        )
    if diagnostics:
        raise ValidationError("lint refused admission:\n  " + "\n  ".join(diagnostics))
    return admissions


def apply(
    manifest_root: Path,
    site: Path,
    config: Config,
    *,
    on_download: Callable[[ArtifactLock], bytes] | None = None,
) -> dict[str, object]:
    """Reconcile in full: plan, admit, publish, then regenerate everything derived."""
    declarations = declared(manifest_root)
    site.mkdir(parents=True, exist_ok=True)
    published = registry.read_published(site)
    proposal = plan(declarations, published)
    if proposal.halt:
        raise ValidationError("reconciliation refused:\n  " + "\n  ".join(proposal.halt))

    # Nothing is fetched until the plan is clean, so a halt costs no network at all.
    admissions = admit(
        proposal.admit, declarations, config, proposal.backfill, on_download, manifest_root
    )
    written = 0
    for admission in admissions:
        written += registry.write_release(
            site,
            admission.key,
            list(admission.documents),
            declarations[admission.key].body,
            config.base_url,
        )

    # Re-read rather than merge: the tree is the record, and after the writes above it
    # holds the dialects the release indexes and the catalog are built from.
    published = registry.read_published(site)
    for key, release in sorted(published.items(), key=lambda item: item[0].sort_key):
        if release.is_complete:
            # Idempotent, and it restores an index that went missing out of band.
            registry.write_canonical(
                site / key.path / registry.INDEX_NAME,
                registry.json_bytes(registry.release_index(release, config.base_url)),
            )
    aliases = registry.rebuild_derived(site, config.base_url, published, config.custom_domain)
    return {
        "admitted": len(admissions),
        "published": written,
        "aliases": aliases,
        "releases": len(published),
    }
