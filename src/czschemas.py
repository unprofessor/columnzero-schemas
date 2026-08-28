#!/usr/bin/env python3
"""Build immutable JSON Schema resources for schemas.columnzero.com.

The publisher deliberately trusts only locked release artifacts.  Existing canonical
`rel/` paths are never overwritten; mutable `compat/`, `latest/`, and index paths are
regenerated on every build.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request

import jsonschema
from jsonschema.validators import validator_for
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_INDEX_VERSION = 1
MAX_ARCHIVE_MEMBERS = 1_000
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ValidationError(ValueError):
    """A locked artifact does not meet the publisher contract."""


class ImmutabilityError(ValidationError):
    """A previously published canonical resource would change."""


@dataclasses.dataclass(frozen=True)
class LockedArtifact:
    project: str
    repo: str
    tag: str
    version: str
    asset: str
    url: str
    sha256: str
    published_at: str


@dataclasses.dataclass(frozen=True)
class SchemaDocument:
    artifact: LockedArtifact
    path: str
    compat: str
    dialect: str
    body: bytes

    @property
    def basename(self) -> str:
        return PurePosixPath(self.path).name


def require_safe_segment(value: str, label: str) -> None:
    if not value or not SAFE_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise ValidationError(f"unsafe {label}: {value!r}")


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not name or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"unsafe archive path: {name!r}")
    return path


def download(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # nosec B310 - URLs are lockfile data
        return response.read()


def read_artifact(archive_path: Path) -> dict[str, bytes]:
    """Read a gzip tar safely into memory, rejecting dangerous entry types and sizes."""
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValidationError("archive has too many members")
        for member in members:
            safe_archive_path(member.name)
            if not member.isfile():
                raise ValidationError(f"archive member is not a regular file: {member.name!r}")
            if member.name in files:
                raise ValidationError(f"duplicate archive member: {member.name!r}")
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise ValidationError("archive exceeds unpacked size limit")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValidationError(f"cannot read archive member: {member.name!r}")
            content = handle.read()
            if len(content) != member.size:
                raise ValidationError(f"truncated archive member: {member.name!r}")
            files[member.name] = content
    return files


def parse_index(files: dict[str, bytes], artifact: LockedArtifact) -> list[SchemaDocument]:
    try:
        index = json.loads(files["index.json"])
    except KeyError as error:
        raise ValidationError("artifact is missing root index.json") from error
    except json.JSONDecodeError as error:
        raise ValidationError("index.json is not valid JSON") from error
    if not isinstance(index, dict) or index.get("schema_index") != SCHEMA_INDEX_VERSION:
        raise ValidationError("unsupported schema_index")
    if index.get("project") != artifact.project:
        raise ValidationError("index project does not match lockfile project")
    if index.get("release") != artifact.version:
        raise ValidationError("index release does not match lockfile version")
    schemas = index.get("schemas")
    if not isinstance(schemas, list) or not schemas:
        raise ValidationError("index schemas must be a non-empty array")

    documents: list[SchemaDocument] = []
    seen: set[str] = set()
    seen_basenames: set[str] = set()
    for item in schemas:
        if not isinstance(item, dict):
            raise ValidationError("index schemas entries must be objects")
        path, compat, dialect = item.get("path"), item.get("compat"), item.get("dialect")
        if not all(isinstance(value, str) for value in (path, compat, dialect)):
            raise ValidationError("schema index entries require string path, compat, and dialect")
        safe_archive_path(path)
        require_safe_segment(compat, "compat")
        basename = PurePosixPath(path).name
        if path == "index.json" or path not in files or path in seen or basename in seen_basenames:
            raise ValidationError(f"invalid or duplicate schema path: {path!r}")
        seen.add(path)
        seen_basenames.add(basename)
        documents.append(SchemaDocument(artifact, path, compat, dialect, files[path]))
    return documents


def canonical_url(base_url: str, document: SchemaDocument) -> str:
    require_safe_segment(document.artifact.project, "project")
    require_safe_segment(document.artifact.version, "version")
    require_safe_segment(document.basename, "schema basename")
    return f"{base_url.rstrip('/')}/{document.artifact.project}/rel/{document.artifact.version}/{document.basename}"


def validate_document(base_url: str, document: SchemaDocument) -> None:
    try:
        value = json.loads(document.body)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{document.path}: invalid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{document.path}: schema root must be an object")
    if value.get("$schema") != document.dialect:
        raise ValidationError(f"{document.path}: $schema does not match index dialect")
    validator = validator_for(value, default=None)
    if validator is None:
        raise ValidationError(f"{document.path}: unsupported schema dialect")
    try:
        validator.check_schema(value)
    except jsonschema.exceptions.SchemaError as error:
        raise ValidationError(f"{document.path}: fails declared meta-schema: {error.message}") from error
    expected_id = canonical_url(base_url, document)
    if value.get("$id") != expected_id:
        raise ValidationError(f"{document.path}: $id must equal {expected_id}")


def fetch_document_set(base_url: str, artifacts: list[LockedArtifact]) -> list[SchemaDocument]:
    documents: list[SchemaDocument] = []
    for artifact in artifacts:
        for value, label in ((artifact.project, "project"), (artifact.version, "version")):
            require_safe_segment(value, label)
        payload = download(artifact.url)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != artifact.sha256:
            raise ValidationError(f"sha256 mismatch for {artifact.project} {artifact.tag}")
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as temporary:
            temporary.write(payload)
            temporary.flush()
            parsed = parse_index(read_artifact(Path(temporary.name)), artifact)
        for document in parsed:
            validate_document(base_url, document)
        documents.extend(parsed)
    return documents


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != body:
        raise ImmutabilityError(f"would change existing canonical resource: {path}")
    path.write_bytes(body)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def stable_version_key(version: str) -> tuple[int, int, int, str]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?", version)
    if not match:
        raise ValidationError(f"version is not SemVer: {version!r}")
    major, minor, patch, prerelease = match.groups()
    # The caller excludes prereleases from aliases. The final field makes ordering total.
    return int(major), int(minor), int(patch), prerelease or "~"


def build_site(base_url: str, artifacts: list[LockedArtifact], site: Path) -> dict[str, int]:
    documents = fetch_document_set(base_url, artifacts)
    site.mkdir(parents=True, exist_ok=True)
    catalog_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    old_catalog = site / "index.json"
    if old_catalog.exists():
        try:
            existing = json.loads(old_catalog.read_text())
            for record in existing.get("schemas", []):
                key = (record["project"], record["version"], record["schema"])
                catalog_by_key[key] = record
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValidationError("existing catalog is invalid") from error
    aliases: dict[tuple[str, str, str], SchemaDocument] = {}
    latest: dict[tuple[str, str], SchemaDocument] = {}
    published = 0

    for document in documents:
        target = site / document.artifact.project / "rel" / document.artifact.version / document.basename
        existed = target.exists()
        atomic_write(target, document.body)
        published += not existed
        catalog_by_key[(document.artifact.project, document.artifact.version, document.basename)] = {
            "project": document.artifact.project,
            "version": document.artifact.version,
            "schema": document.basename,
            "compat": document.compat,
            "dialect": document.dialect,
            "url": canonical_url(base_url, document),
            "sha256": hashlib.sha256(document.body).hexdigest(),
            "published_at": document.artifact.published_at,
        }
        if "-" not in document.artifact.version:
            compat_key = (document.artifact.project, document.compat, document.basename)
            latest_key = (document.artifact.project, document.basename)
            if compat_key not in aliases or stable_version_key(document.artifact.version) > stable_version_key(aliases[compat_key].artifact.version):
                aliases[compat_key] = document
            if latest_key not in latest or stable_version_key(document.artifact.version) > stable_version_key(latest[latest_key].artifact.version):
                latest[latest_key] = document

    for (project, compat, basename), document in aliases.items():
        destination = site / project / "compat" / compat / basename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(document.body)
        write_json(destination.parent / "index.json", {
            "project": project,
            "compat": compat,
            "schema": basename,
            "version": document.artifact.version,
            "url": canonical_url(base_url, document),
            "sha256": hashlib.sha256(document.body).hexdigest(),
            "published_at": document.artifact.published_at,
        })
    for (project, basename), document in latest.items():
        destination = site / project / "latest" / basename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(document.body)
        write_json(destination.parent / "index.json", {
            "project": project,
            "schema": basename,
            "version": document.artifact.version,
            "url": canonical_url(base_url, document),
            "sha256": hashlib.sha256(document.body).hexdigest(),
            "published_at": document.artifact.published_at,
        })

    catalog = sorted(
        catalog_by_key.values(),
        key=lambda record: (record["project"], stable_version_key(record["version"]), record["schema"]),
    )
    write_json(site / "index.json", {"schema_catalog": 1, "schemas": catalog})
    hostname = urllib.parse.urlparse(base_url).hostname
    if hostname is None:
        raise ValidationError("site base URL has no hostname")
    (site / "CNAME").write_text(f"{hostname}\n")
    return {"published": published, "aliases": len(aliases) + len(latest)}


def read_lock(path: Path) -> list[LockedArtifact]:
    raw = tomllib.loads(path.read_text())
    entries = raw.get("artifact", [])
    if not isinstance(entries, list):
        raise ValidationError("manifest.lock artifact must be an array of tables")
    required = {field.name for field in dataclasses.fields(LockedArtifact)}
    artifacts: list[LockedArtifact] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValidationError("each lock entry must contain exactly the supported fields")
        artifacts.append(LockedArtifact(**entry))
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--manifest", type=Path, default=Path("manifest.toml"))
    parser.add_argument("--lock", type=Path, default=Path("manifest.lock"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args()
    manifest = tomllib.loads(args.manifest.read_text())
    base_url = manifest.get("site", {}).get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValidationError("manifest site.base_url must be an HTTPS URL")
    result = build_site(base_url, read_lock(args.lock), args.site)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
