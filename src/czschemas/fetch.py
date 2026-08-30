"""Fetch and verify a locked release artifact.

This is the only component that touches the network, and the only one that opens an
archive.  What it produces is bytes with a checked identity: the digest matched the
pin, the archive was safe to read, and the index inside agrees with the release the
caller asked for.  Whether the *contents* are a well-formed schema is not asked here --
that is admission linting, which runs on what this returns.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

from .model import (
    ArtifactLock,
    ReleaseKey,
    SchemaDocument,
    ValidationError,
    schema_format,
)


SCHEMA_INDEX_VERSION = 1
MAX_ARCHIVE_MEMBERS = 1_000
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
GITHUB_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
# The form a lock may name.  The redirect targets above are reachable *during* a
# download but are opaque, and an opaque asset URL in a lock is unreviewable: a human
# cannot tell which release it names.
RELEASE_ASSET_PATH = PurePosixPath("releases") / "download"


def validate_download_url(url: str) -> None:
    """Any host a download may touch, including mid-redirect."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "") not in GITHUB_DOWNLOAD_HOSTS:
        raise ValidationError(f"unsafe artifact URL: {url!r}")


def validate_lock_url(url: str) -> None:
    """The stricter rule for a URL written into a lock: a named GitHub release asset."""
    validate_download_url(url)
    parsed = urllib.parse.urlsplit(url)
    parts = PurePosixPath(parsed.path).parts[1:]
    if parsed.hostname != "github.com" or len(parts) != 6 or parts[2:4] != RELEASE_ASSET_PATH.parts:
        raise ValidationError(
            f"lock URL must name a GitHub release asset "
            f"(https://github.com/{{owner}}/{{repo}}/releases/download/{{tag}}/{{asset}}): {url!r}"
        )


def release_tag(url: str) -> str:
    """The tag a lock URL names.  Derived for messages and plan output, never stored."""
    return PurePosixPath(urllib.parse.urlsplit(url).path).parts[5]


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


def download(lock: ArtifactLock) -> bytes:
    """Download a locked artifact with strict allow-list, size, and timeout limits."""
    validate_lock_url(lock.url)
    try:
        body = download_origin(lock.url, DOWNLOAD_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        raise ValidationError(f"artifact download failed: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ValidationError(f"artifact download failed: {error.reason}") from error
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValidationError("artifact exceeds download size limit")
    return body


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not name or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"unsafe archive path: {name!r}")
    return path


def read_archive(payload: bytes) -> dict[str, bytes]:
    """Read a gzip tar safely into memory, rejecting dangerous entry types and sizes."""
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
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


def parse_index(files: dict[str, bytes], key: ReleaseKey) -> list[SchemaDocument]:
    """Read the artifact's root index and bind each listed schema to `key`.

    Every check here is about the envelope: does the archive contain what it says, are
    the names safe and unique, and does the artifact agree about which release it is.
    """
    try:
        index = json.loads(files["index.json"])
    except KeyError as error:
        raise ValidationError("artifact is missing root index.json") from error
    except json.JSONDecodeError as error:
        raise ValidationError("index.json is not valid JSON") from error
    if not isinstance(index, dict) or index.get("schema_index") != SCHEMA_INDEX_VERSION:
        raise ValidationError("unsupported schema_index")
    if index.get("project") != key.project:
        raise ValidationError(
            f"index project {index.get('project')!r} does not match {key.project!r}"
        )
    if index.get("release") != str(key.version):
        raise ValidationError(
            f"index release {index.get('release')!r} does not match {key.version}"
        )
    schemas = index.get("schemas")
    if not isinstance(schemas, list) or not schemas:
        raise ValidationError("index schemas must be a non-empty array")

    documents: list[SchemaDocument] = []
    seen_paths: set[str] = set()
    seen_basenames: set[str] = set()
    for item in schemas:
        if not isinstance(item, dict):
            raise ValidationError("index schemas entries must be objects")
        path, compat, dialect = item.get("path"), item.get("compat"), item.get("dialect")
        if not all(isinstance(value, str) for value in (path, compat, dialect)):
            raise ValidationError("schema index entries require string path, compat, and dialect")
        safe_archive_path(path)
        if compat != str(key.compat):
            raise ValidationError(
                f"{path}: compat {compat!r} does not describe release {key.version}"
                f" (expected {str(key.compat)!r})"
            )
        basename = PurePosixPath(path).name
        schema_format(basename)
        if path not in files or path in seen_paths or basename in seen_basenames:
            raise ValidationError(f"invalid or duplicate schema path: {path!r}")
        seen_paths.add(path)
        seen_basenames.add(basename)
        documents.append(SchemaDocument(key, path, dialect, files[path]))
    return documents


def verify(key: ReleaseKey, lock: ArtifactLock, payload: bytes) -> list[SchemaDocument]:
    """Check a payload against its pin and return the schemas it carries."""
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValidationError("artifact exceeds download size limit")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != lock.sha256:
        raise ValidationError(f"sha256 mismatch for {key}: expected {lock.sha256}, got {actual}")
    return parse_index(read_archive(payload), key)
