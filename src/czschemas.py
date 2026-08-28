#!/usr/bin/env python3
"""Build immutable JSON Schema resources for schemas.columnzero.com.

Canonical release resources live at `/{project}/v{compat}/{version}/{name}.schema.json`
and are never overwritten or removed.  The mutable resources around them -- the
compat-line alias at `/{project}/v{compat}/`, `latest/`, and every index -- are
regenerated on every build.  The publisher trusts only locked release artifacts.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request

import jsonschema
from jsonschema.validators import validator_for
from pathlib import Path, PurePosixPath
from typing import Any, Callable


SCHEMA_INDEX_VERSION = 1
MAX_ARCHIVE_MEMBERS = 1_000
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A compat line is a SemVer major, or `0.MINOR` where the minor is the breaking boundary.
COMPAT_LINE = re.compile(r"^(?:[1-9]\d*|0\.(?:0|[1-9]\d*))$")
# Canonical schema files and release directories share a parent, so their names must not
# be able to collide.  No SemVer version can end in this suffix, and no index.json can.
SCHEMA_SUFFIX = ".schema.json"
# {project}/v{compat}/{version}/... - everything below a release directory is canonical.
CANONICAL_PATH = re.compile(r"^[^/]+/v[^/]+/[^/]+/")
# The only git statuses that leave every published byte where it was: a file that did
# not exist before (A), and a copy, whose source survives untouched (C).  Everything
# else is a violation, including letters git has yet to invent.
ADDITIVE_STATUSES = frozenset("AC")
GITHUB_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class ValidationError(ValueError):
    """A locked artifact does not meet the publisher contract."""


class ImmutabilityError(ValidationError):
    """A previously published canonical resource would change."""


@dataclasses.dataclass(frozen=True)
class LockedArtifact:
    project: str
    tag: str
    version: str
    url: str
    sha256: str


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


def require_compat_line(value: str) -> None:
    if not COMPAT_LINE.fullmatch(value or ""):
        raise ValidationError(f"compat must be a major or 0.minor line: {value!r}")


def require_schema_basename(value: str) -> None:
    require_safe_segment(value, "schema basename")
    if not value.endswith(SCHEMA_SUFFIX) or len(value) == len(SCHEMA_SUFFIX):
        raise ValidationError(f"schema file must be named <name>{SCHEMA_SUFFIX}: {value!r}")


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not name or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"unsafe archive path: {name!r}")
    return path


def validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme != "https" or host not in GITHUB_DOWNLOAD_HOSTS:
        raise ValidationError(f"unsafe artifact URL: {url!r}")


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Reject redirects to hosts (or schemes) outside the download allow-list."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_download_url(urllib.parse.urljoin(request.full_url, new_url))
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


_OPENER = urllib.request.build_opener(_RedirectGuard)


def download_origin(url: str, timeout: float) -> bytes:
    """Perform the network transfer, bounded by MAX_ARTIFACT_BYTES."""
    request = urllib.request.Request(url, method="GET")
    with _OPENER.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValidationError(f"artifact download failed: HTTP {response.status}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise ValidationError("artifact exceeds download size limit")
            chunks.append(chunk)
        return b"".join(chunks)


def download(artifact: LockedArtifact) -> bytes:
    """Download a locked artifact with strict allow-list, size, and timeout limits."""
    validate_download_url(artifact.url)
    try:
        body = download_origin(artifact.url, DOWNLOAD_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        raise ValidationError(f"artifact download failed: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ValidationError(f"artifact download failed: {error.reason}") from error
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValidationError("artifact exceeds download size limit")
    return body


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
        require_compat_line(compat)
        expected_compat = compat_line_for(artifact.version)
        if compat != expected_compat:
            raise ValidationError(
                f"{path}: compat {compat!r} does not describe release {artifact.version}"
                f" (expected {expected_compat!r})"
            )
        basename = PurePosixPath(path).name
        require_schema_basename(basename)
        if path not in files or path in seen or basename in seen_basenames:
            raise ValidationError(f"invalid or duplicate schema path: {path!r}")
        seen.add(path)
        seen_basenames.add(basename)
        documents.append(SchemaDocument(artifact, path, compat, dialect, files[path]))
    return documents


def canonical_url(base_url: str, document: SchemaDocument) -> str:
    require_safe_segment(document.artifact.project, "project")
    require_safe_segment(document.artifact.version, "version")
    require_compat_line(document.compat)
    require_schema_basename(document.basename)
    return (
        f"{base_url.rstrip('/')}/{document.artifact.project}"
        f"/v{document.compat}/{document.artifact.version}/{document.basename}"
    )


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


def validate_artifact(
    base_url: str,
    artifact: LockedArtifact,
    payload: bytes,
) -> list[SchemaDocument]:
    """Validate a downloaded artifact payload and return its schema documents."""
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValidationError("artifact exceeds download size limit")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != artifact.sha256:
        raise ValidationError(f"sha256 mismatch for {artifact.project} {artifact.tag}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as temporary:
        temporary.write(payload)
        temporary.flush()
        parsed = parse_index(read_artifact(Path(temporary.name)), artifact)
    for document in parsed:
        validate_document(base_url, document)
    return parsed


def fetch_document_set(
    base_url: str,
    artifacts: list[LockedArtifact],
    on_download: Callable[[LockedArtifact], bytes] | None = None,
) -> list[SchemaDocument]:
    """Fetch and validate every locked artifact. `on_download` is test injection."""
    documents: list[SchemaDocument] = []
    for artifact in artifacts:
        for value, label in ((artifact.project, "project"), (artifact.version, "version")):
            require_safe_segment(value, label)
        if on_download is not None:
            payload = on_download(artifact)
        else:
            payload = download(artifact)
        documents.extend(validate_artifact(base_url, artifact, payload))
    return documents


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != body:
        raise ImmutabilityError(f"would change existing canonical resource: {path}")
    path.write_bytes(body)


def purge_mutable_paths(site: Path, projects: set[str]) -> None:
    """Remove regenerated resources so a rebuild cannot leave stale aliases behind.

    Mutable resources are the files directly inside a project or compat-line directory;
    canonical releases live one level deeper, under `v{compat}/{version}/`.  This walks
    compat lines file by file and never recurses, so no code path can delete a release.
    `latest/` holds nothing else, so it is removed whole.
    """
    for project in projects:
        project_root = site / project
        latest = project_root / "latest"
        if latest.is_dir():
            shutil.rmtree(latest)
        index = project_root / "index.json"
        if index.exists():
            index.unlink()
        for line in sorted(project_root.glob("v*")):
            if not line.is_dir():
                continue
            for entry in sorted(line.iterdir()):
                if entry.is_file():
                    entry.unlink()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def release_index(project: str, compat: str, version: str, records: list[dict[str, str]]) -> dict[str, Any]:
    """Describe one release: its membership, digests, and dialects.

    This is the tree's own record of a release.  Everything the catalog holds is
    derivable from these plus the directory names around them, which is why nothing in
    the build ever reads the catalog back.  When a resource went live is not recorded
    here: `gh-pages` is a git branch, so the commit that added it already says.
    """
    return {
        "project": project,
        "version": version,
        "compat": compat,
        "schemas": [
            {
                "schema": record["schema"],
                "dialect": record["dialect"],
                "url": record["url"],
                "sha256": record["sha256"],
            }
            for record in sorted(records, key=lambda record: record["schema"])
        ],
    }


def stable_version_key(version: str) -> tuple[int, int, int, str]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?", version)
    if not match:
        raise ValidationError(f"version is not SemVer: {version!r}")
    major, minor, patch, prerelease = match.groups()
    # The caller excludes prereleases from aliases. The final field makes ordering total.
    return int(major), int(minor), int(patch), prerelease or "~"


def compat_line_for(version: str) -> str:
    """The compat line a release belongs to: its major, or `0.minor` before 1.0."""
    major, minor, _patch, _prerelease = stable_version_key(version)
    return str(major) if major > 0 else f"0.{minor}"


def compat_line_key(line: str) -> tuple[int, int]:
    require_compat_line(line)
    major, _, minor = line.partition(".")
    return int(major), int(minor or 0)


def alias_record(base_url: str, document: SchemaDocument) -> dict[str, str]:
    """Describe the release a mutable alias currently points at."""
    return {
        "schema": document.basename,
        "version": document.artifact.version,
        "dialect": document.dialect,
        "url": canonical_url(base_url, document),
        "sha256": hashlib.sha256(document.body).hexdigest(),
    }


def require_alias_coverage(
    aliases: dict[tuple[str, str, str], SchemaDocument],
    lines_by_project: dict[str, set[str]],
) -> None:
    """Refuse to leave a published compat line without its mutable alias.

    Aliases are rebuilt from the lockfile alone, so an artifact dropped from the lock
    would silently take its line's alias down while the releases underneath stayed
    published.  The lockfile is cumulative by design; this makes that a build failure
    rather than a 404 on a URL the project index still advertises.
    """
    covered: dict[str, set[str]] = {}
    for project, compat, _basename in aliases:
        covered.setdefault(project, set()).add(compat)
    for project, lines in sorted(lines_by_project.items()):
        missing = sorted(lines - covered.get(project, set()), key=compat_line_key)
        if missing:
            raise ValidationError(
                f"lockfile does not cover published compat line(s) for {project}: "
                + ", ".join(f"v{line}" for line in missing)
            )


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path.name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path.name} must contain a JSON object")
    return value


def changed_canonical_paths(name_status: str) -> list[str]:
    """Canonical paths that a `git diff --name-status` reports as anything but additive.

    Immutability is enforced against git rather than against anything inside the tree.
    A record kept in the tree can be edited by whoever edits the tree - drop the field
    an index is checked on and the check disappears with it - whereas the previous
    commit is not reachable from the working copy.

    The rule is an allowlist. Naming the bad statuses instead would mean the check is
    only as complete as that list: `T`, a regular file swapped for a symlink, was
    missing from exactly such a list, and it leaves the release index byte-identical.
    """
    violations: list[str] = []
    for line in name_status.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        kind = fields[0][:1]
        # Renames and copies list the source first, and for a rename it is the path
        # that disappears; for everything else there is only one path.
        if kind not in ADDITIVE_STATUSES and CANONICAL_PATH.match(fields[1]):
            violations.append(f"{kind} {fields[1]}")
    return violations


def read_published(site: Path, base_url: str) -> list[dict[str, str]]:
    """Read what the tree already publishes.

    The filesystem is the record: directory names give project, compat line, and
    version, the release index gives dialects and canonical URLs, and the bytes give
    the digests.  This establishes what exists so the build can regenerate the indexes
    around it.  It verifies nothing - `changed_canonical_paths` does that against git,
    the one record the tree cannot rewrite.
    """
    records: list[dict[str, str]] = []
    if not site.is_dir():
        return records
    root = base_url.rstrip("/")
    for project_dir in sorted(entry for entry in site.iterdir() if entry.is_dir()):
        project = project_dir.name
        require_safe_segment(project, "published project")
        for line_dir in sorted(entry for entry in project_dir.glob("v*") if entry.is_dir()):
            compat = line_dir.name[1:]
            require_compat_line(compat)
            for release_dir in sorted(entry for entry in line_dir.iterdir() if entry.is_dir()):
                version = release_dir.name
                require_safe_segment(version, "published version")
                stable_version_key(version)
                records.extend(read_release(release_dir, root, project, compat, version))
    return records


def read_release(
    release_dir: Path, root: str, project: str, compat: str, version: str
) -> list[dict[str, str]]:
    """Records for one release directory. Digests are computed to publish, not to police."""
    on_disk = sorted(
        entry.name
        for entry in release_dir.iterdir()
        if entry.is_file() and entry.name != "index.json"
    )
    index_path = release_dir / "index.json"
    listed: dict[str, dict[str, str]] = {}
    if index_path.is_file():
        for entry in read_json_object(index_path).get("schemas", []):
            if isinstance(entry, dict) and isinstance(entry.get("schema"), str):
                listed[entry["schema"]] = entry
    records: list[dict[str, str]] = []
    for name in on_disk:
        require_schema_basename(name)
        body = (release_dir / name).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        entry = listed.get(name, {})
        record = {
            "project": project,
            "version": version,
            "schema": name,
            "compat": compat,
            "dialect": entry.get("dialect") or declared_dialect(release_dir / name, body),
            "url": entry.get("url") or f"{root}/{project}/v{compat}/{version}/{name}",
            "sha256": digest,
        }
        records.append(record)
    return records


def declared_dialect(path: Path, body: bytes) -> str:
    """Fall back to the schema's own $schema when no release index records it."""
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path}: published schema is not valid JSON") from error
    dialect = value.get("$schema") if isinstance(value, dict) else None
    if not isinstance(dialect, str):
        raise ValidationError(f"{path}: published schema declares no $schema")
    return dialect


def build_site(
    base_url: str,
    artifacts: list[LockedArtifact],
    site: Path,
    on_download: Callable[[LockedArtifact], bytes] | None = None,
    custom_domain: bool = False,
) -> dict[str, int]:
    documents = fetch_document_set(base_url, artifacts, on_download)
    site.mkdir(parents=True, exist_ok=True)
    # What is already published, read from the tree itself.  Nothing reads the catalog
    # back: it is derived output, so trusting it would only be trusting a copy.
    published_before = read_published(site, base_url)
    catalog_by_key: dict[tuple[str, str, str], dict[str, str]] = {
        (record["project"], record["version"], record["schema"]): record
        for record in published_before
    }
    aliases: dict[tuple[str, str, str], SchemaDocument] = {}
    latest: dict[tuple[str, str], SchemaDocument] = {}
    released_documents: list[SchemaDocument] = []
    published = 0

    for document in documents:
        target = (
            site
            / document.artifact.project
            / f"v{document.compat}"
            / document.artifact.version
            / document.basename
        )
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
        }
        if "-" not in document.artifact.version:
            released_documents.append(document)

    for document in released_documents:
        compat_key = (document.artifact.project, document.compat, document.basename)
        latest_key = (document.artifact.project, document.basename)
        if compat_key not in aliases or stable_version_key(document.artifact.version) > stable_version_key(aliases[compat_key].artifact.version):
            aliases[compat_key] = document
        if latest_key not in latest or stable_version_key(document.artifact.version) > stable_version_key(latest[latest_key].artifact.version):
            latest[latest_key] = document

    # Everything ever published, not just what this build's lockfile happens to name.
    versions_by_line: dict[tuple[str, str], set[str]] = {}
    lines_by_project: dict[str, set[str]] = {}
    stable_lines_by_project: dict[str, set[str]] = {}
    releases: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for record in catalog_by_key.values():
        project, compat, version = record["project"], record["compat"], record["version"]
        versions_by_line.setdefault((project, compat), set()).add(version)
        lines_by_project.setdefault(project, set()).add(compat)
        releases.setdefault((project, compat, version), []).append(record)
        if "-" not in version:
            stable_lines_by_project.setdefault(project, set()).add(compat)
    projects = set(lines_by_project)
    # Only a line with a stable release owes an alias; a prerelease-only line has none.
    require_alias_coverage(aliases, stable_lines_by_project)

    # Every release the catalog knows about, not just the ones this build published, so
    # no line index can advertise a version whose directory has no index.  atomic_write
    # makes each one immutable, and restores one that went missing out of band.
    for (project, compat, version), records in sorted(releases.items()):
        atomic_write(
            site / project / f"v{compat}" / version / "index.json",
            json_bytes(release_index(project, compat, version, records)),
        )

    purge_mutable_paths(site, projects)
    line_members: dict[tuple[str, str], list[SchemaDocument]] = {}
    for (project, compat, basename), document in aliases.items():
        destination = site / project / f"v{compat}" / basename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(document.body)
        line_members.setdefault((project, compat), []).append(document)
    for (project, compat), versions in sorted(versions_by_line.items()):
        members = sorted(line_members.get((project, compat), []), key=lambda member: member.basename)
        write_json(site / project / f"v{compat}" / "index.json", {
            "project": project,
            "compat": compat,
            "versions": sorted(versions, key=stable_version_key),
            "schemas": [alias_record(base_url, member) for member in members],
        })

    latest_members: dict[str, list[SchemaDocument]] = {}
    for (project, basename), document in latest.items():
        destination = site / project / "latest" / basename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(document.body)
        latest_members.setdefault(project, []).append(document)
    for project, members in latest_members.items():
        write_json(site / project / "latest" / "index.json", {
            "project": project,
            "schemas": [alias_record(base_url, member) for member in sorted(members, key=lambda m: m.basename)],
        })

    root = base_url.rstrip("/")
    for project in sorted(projects):
        index: dict[str, Any] = {
            "project": project,
            "compat_lines": sorted(lines_by_project[project], key=compat_line_key),
        }
        if project in latest_members:
            index["latest"] = f"{root}/{project}/latest/"
        write_json(site / project / "index.json", index)

    # One flat entry per published schema, so mirroring or auditing the site is a
    # single fetch. Kept out of the hierarchy so every index lists one level only.
    catalog = sorted(
        catalog_by_key.values(),
        key=lambda record: (record["project"], stable_version_key(record["version"]), record["schema"]),
    )
    # Derived output for consumers: one fetch instead of a crawl.  Regenerated whole
    # from the tree every run, so it can never be a stale or poisoned input.
    write_json(site / "catalog.json", {"schema_catalog": 1, "schemas": catalog})
    write_json(site / "index.json", {
        "schema_site": 1,
        "catalog": f"{root}/catalog.json",
        "projects": sorted(projects),
    })
    hostname = urllib.parse.urlparse(base_url).hostname
    if hostname is None:
        raise ValidationError("site base URL has no hostname")
    # Pages runs Jekyll unless told otherwise, which does nothing useful for a JSON
    # tree and silently drops any path segment that starts with an underscore.
    (site / ".nojekyll").write_text("")
    cname = site / "CNAME"
    if custom_domain:
        cname.write_text(f"{hostname}\n")
    elif cname.exists():
        # A published CNAME switches Pages to the custom domain and takes the default
        # *.github.io URL down with it, so opting out has to remove the file.
        cname.unlink()
    return {"published": published, "aliases": len(aliases) + len(latest)}


def read_lock(path: Path) -> list[LockedArtifact]:
    """Parse and validate a lockfile. Fields are validated before any download."""
    raw = tomllib.loads(path.read_text())
    entries = raw.get("artifact", [])
    if not isinstance(entries, list):
        raise ValidationError("manifest.lock artifact must be an array of tables")
    required = {field.name for field in dataclasses.fields(LockedArtifact)}
    artifacts: list[LockedArtifact] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValidationError("each lock entry must contain exactly the supported fields")
        artifact = LockedArtifact(**entry)
        for value, label in (
            (artifact.project, "project"),
            (artifact.version, "version"),
            (artifact.tag, "tag"),
        ):
            require_safe_segment(value, label)
        stable_version_key(artifact.version)
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256):
            raise ValidationError(f"sha256 must be 64 hex characters for {artifact.project} {artifact.tag}")
        artifacts.append(artifact)
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "verify"])
    parser.add_argument("--manifest", type=Path, default=Path("manifest.toml"))
    parser.add_argument("--lock", type=Path, default=Path("manifest.lock"))
    parser.add_argument("--site", type=Path, default=Path("site"))
    args = parser.parse_args()
    if args.command == "verify":
        violations = changed_canonical_paths(sys.stdin.read())
        if violations:
            print("refusing to publish: canonical resources would change", file=sys.stderr)
            for violation in violations:
                print(f"  {violation}", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({"violations": 0}))
        return
    manifest = tomllib.loads(args.manifest.read_text())
    site_config = manifest.get("site", {})
    base_url = site_config.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValidationError("manifest site.base_url must be an HTTPS URL")
    custom_domain = site_config.get("custom_domain", False)
    if not isinstance(custom_domain, bool):
        raise ValidationError("manifest site.custom_domain must be a boolean")
    result = build_site(base_url, read_lock(args.lock), args.site, custom_domain=custom_domain)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
