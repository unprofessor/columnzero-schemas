"""Parsed identities for the registry.

Every name in the publication tree is a projection of one of these.  A release is
identified by its project and version, and nothing else: the compatibility line is a
function of the version, so it is derived here and only ever *checked* against a path
rather than read from one.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import PurePosixPath


# A path segment safe to place in a URL and in a directory name.
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# SemVer without build metadata.  Metadata is excluded from precedence, so two versions
# differing only in metadata would compete for one directory; rejecting it keeps the URL
# namespace and the ordering in agreement.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?$"
)
NUMERIC_IDENTIFIER = re.compile(r"^(?:0|[1-9]\d*)$")

# Canonical file suffixes, mapping the published extension to the format it names.  The
# suffix is the discriminator: a URL says what it serves, and a declared format can never
# disagree with the extension because there is no declared format.
SCHEMA_SUFFIXES = {".schema.json": "json-schema"}


class ValidationError(ValueError):
    """Input does not satisfy the registry's rules."""


class ImmutabilityError(ValidationError):
    """A previously published canonical resource would change."""


def require_safe_segment(value: str, label: str) -> str:
    if not value or not SAFE_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise ValidationError(f"unsafe {label}: {value!r}")
    return value


def schema_format(basename: str) -> str:
    """The format a published filename names, or raise if the suffix is unregistered."""
    require_safe_segment(basename, "schema basename")
    for suffix, name in SCHEMA_SUFFIXES.items():
        if basename.endswith(suffix) and len(basename) > len(suffix):
            return name
    known = ", ".join(sorted(SCHEMA_SUFFIXES))
    raise ValidationError(f"schema file must end in one of {known}: {basename!r}")


@dataclasses.dataclass(frozen=True)
class CompatLine:
    """A breaking-change boundary: the SemVer major, or `0.minor` before 1.0."""

    major: int
    minor: int | None = None

    def __str__(self) -> str:
        return str(self.major) if self.minor is None else f"{self.major}.{self.minor}"

    @property
    def segment(self) -> str:
        return f"v{self}"

    @property
    def sort_key(self) -> tuple[int, int]:
        return self.major, self.minor or 0

    @classmethod
    def parse_segment(cls, segment: str) -> CompatLine:
        if not segment.startswith("v"):
            raise ValidationError(f"compat line segment must start with v: {segment!r}")
        text = segment[1:]
        major, _, minor = text.partition(".")
        if not re.fullmatch(r"[1-9]\d*", major) and not (major == "0" and minor):
            raise ValidationError(f"not a compat line: {segment!r}")
        if major == "0":
            if not re.fullmatch(r"0|[1-9]\d*", minor):
                raise ValidationError(f"not a compat line: {segment!r}")
            return cls(0, int(minor))
        if minor:
            raise ValidationError(f"only 0.x lines carry a minor: {segment!r}")
        return cls(int(major))


@dataclasses.dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str | int, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        match = SEMVER.fullmatch(text or "")
        if not match:
            raise ValidationError(f"version is not SemVer: {text!r}")
        major, minor, patch, prerelease = match.groups()
        identifiers = tuple(
            int(part) if NUMERIC_IDENTIFIER.fullmatch(part) else part
            for part in (prerelease.split(".") if prerelease else ())
        )
        return cls(int(major), int(minor), int(patch), identifiers)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        if not self.prerelease:
            return core
        return core + "-" + ".".join(str(part) for part in self.prerelease)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def compat(self) -> CompatLine:
        return CompatLine(self.major) if self.major else CompatLine(0, self.minor)

    @property
    def sort_key(self) -> tuple:
        """SemVer precedence (section 11).

        Numeric identifiers rank below alphanumeric ones and compare numerically, so
        `rc.9` precedes `rc.10`.  A release outranks every prerelease sharing its core,
        which is what the third element encodes; and a longer identifier list outranks a
        shorter one that it extends, which tuple comparison gives for free.
        """
        ranked = tuple(
            (0, part, "") if isinstance(part, int) else (1, 0, part)
            for part in self.prerelease
        )
        return (self.major, self.minor, self.patch, not self.prerelease, ranked)


@dataclasses.dataclass(frozen=True)
class ReleaseKey:
    """What identifies a release: a project and a version.  Everything else is derived."""

    project: str
    version: Version

    def __post_init__(self) -> None:
        require_safe_segment(self.project, "project")

    @property
    def compat(self) -> CompatLine:
        return self.version.compat

    @property
    def path(self) -> PurePosixPath:
        """The release directory, relative to the root of either tree."""
        return PurePosixPath(self.project) / self.compat.segment / str(self.version)

    @property
    def sort_key(self) -> tuple:
        return (self.project, self.version.sort_key)

    def url(self, base_url: str, basename: str) -> str:
        schema_format(basename)
        return f"{base_url.rstrip('/')}/{self.path}/{basename}"

    def __str__(self) -> str:
        return f"{self.project} {self.version}"

    @classmethod
    def from_path(cls, relative: PurePosixPath | str) -> ReleaseKey:
        """Parse a release directory path, checking the line segment against the version.

        The line is redundant with the version, which is exactly what makes it worth
        reading: a path that disagrees with the release it contains is a corruption the
        registry can catch for free.
        """
        parts = PurePosixPath(relative).parts
        if len(parts) != 3:
            raise ValidationError(f"not a release path: {relative}")
        project, segment, version = parts
        key = cls(project, Version.parse(version))
        if segment != key.compat.segment:
            raise ValidationError(
                f"{relative}: line {segment!r} does not describe release {version}"
            )
        return key


@dataclasses.dataclass(frozen=True)
class ArtifactLock:
    """A locator and a digest.  The digest is the pin; the URL is where to look.

    Nothing else belongs here.  A release tag is a substring of the URL, and the project
    and version are the path this lock was found at, so recording either would only
    create a way for them to disagree.
    """

    url: str
    sha256: str

    @classmethod
    def parse(cls, body: bytes, origin: str) -> ArtifactLock:
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValidationError(f"{origin}: lock is not valid JSON") from error
        if not isinstance(value, dict):
            raise ValidationError(f"{origin}: lock must be a JSON object")
        expected = {field.name for field in dataclasses.fields(cls)}
        if set(value) != expected:
            missing = ", ".join(sorted(expected.symmetric_difference(value)))
            raise ValidationError(f"{origin}: lock must contain exactly url, sha256 ({missing})")
        lock = cls(**value)
        if not isinstance(lock.url, str) or not isinstance(lock.sha256, str):
            raise ValidationError(f"{origin}: lock fields must be strings")
        if not re.fullmatch(r"[0-9a-f]{64}", lock.sha256):
            raise ValidationError(f"{origin}: sha256 must be 64 lowercase hex characters")
        return lock


@dataclasses.dataclass(frozen=True)
class SchemaDocument:
    """One schema file from a verified artifact, bound to the release that carries it."""

    key: ReleaseKey
    path: str
    dialect: str
    body: bytes

    @property
    def basename(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def format(self) -> str:
        return schema_format(self.basename)
