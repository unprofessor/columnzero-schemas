"""Immutable schema publication for schemas.columnzero.com.

The registry is append-only and format-agnostic.  It hosts bytes at canonical URLs,
pins each release by digest, and enforces immutability against git.  What the bytes
*mean* is not its concern: that is admission linting, which is pluggable and runs once.

Components:
    model       parsed identities -- version, compat line, release key, lock
    fetch       the only component that touches the network or opens an archive
    lint        pluggable admission checks, external and per-format
    registry    the publication tree and the derived resources around it
    pages       the browsable HTML projection of the publication tree
    reconcile   the manifest tree vs. the publication tree; the difference is the queue
    gate        the immutability verdict, taken against git rather than the tree
"""

from .model import (
    ArtifactLock,
    CompatLine,
    ImmutabilityError,
    ReleaseKey,
    SchemaDocument,
    ValidationError,
    Version,
)

__all__ = [
    "ArtifactLock",
    "CompatLine",
    "ImmutabilityError",
    "ReleaseKey",
    "SchemaDocument",
    "ValidationError",
    "Version",
]
