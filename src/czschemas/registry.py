"""The publication tree: what it holds, and how the derived resources around it are made.

The tree is the record.  Directory names give project, line, and version; the release
directory gives the schemas, their digests, and the lock the release was admitted under.
Everything else on the site -- aliases, every index, the catalog -- is regenerated from
that on each run and never read back as input.

Canonical resources go through `write_canonical`, which refuses to change a file that
already exists.  That is a convenience: it fails a local build at the point of cause.
The guarantee is the git check in `gate`, which the tree cannot influence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterator

from .model import (
    CompatLine,
    ImmutabilityError,
    ReleaseKey,
    SchemaDocument,
    ValidationError,
    Version,
    require_safe_segment,
    schema_format,
)


LOCK_NAME = "artifact.lock"
INDEX_NAME = "index.json"
CATALOG_NAME = "catalog.json"
# What a release directory may hold besides schemas.
RESERVED_NAMES = frozenset({INDEX_NAME, LOCK_NAME})


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def write_canonical(path: Path, body: bytes) -> bool:
    """Write an immutable resource.  Returns True if it is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ImmutabilityError(f"would change existing canonical resource: {path}")
        return False
    path.write_bytes(body)
    return True


def write_mutable(path: Path, body: bytes) -> None:
    """Aliases and indexes; `purge_mutable` has already cleared the previous generation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


@dataclasses.dataclass(frozen=True)
class PublishedSchema:
    name: str
    body: bytes
    dialect: str | None      # None when no release index records one

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclasses.dataclass(frozen=True)
class PublishedRelease:
    key: ReleaseKey
    schemas: tuple[PublishedSchema, ...]
    lock: bytes | None       # None for a release published before locks existed

    @property
    def is_complete(self) -> bool:
        """A release the reconciler has nothing left to do for.

        Both halves have to hold: without the lock there is no record of where the bytes
        came from, and without a dialect on every schema the release index is missing.
        Neither is recoverable from the tree -- only from the artifact -- so an
        incomplete release is one to re-admit, not one to repair in place.
        """
        return self.lock is not None and all(schema.dialect for schema in self.schemas)


def release_directories(site: Path) -> Iterator[Path]:
    """Every `{project}/v{compat}/{version}/` directory in the tree."""
    if not site.is_dir():
        return
    for project_dir in sorted(entry for entry in site.iterdir() if entry.is_dir()):
        require_safe_segment(project_dir.name, "published project")
        for line_dir in sorted(entry for entry in project_dir.glob("v*") if entry.is_dir()):
            for release_dir in sorted(entry for entry in line_dir.iterdir() if entry.is_dir()):
                yield release_dir


def read_published(site: Path) -> dict[ReleaseKey, PublishedRelease]:
    """Read what the tree already publishes.  This verifies nothing -- `gate` does that."""
    releases: dict[ReleaseKey, PublishedRelease] = {}
    for release_dir in release_directories(site):
        key = ReleaseKey.from_path(release_dir.relative_to(site).as_posix())
        dialects: dict[str, str] = {}
        index_path = release_dir / INDEX_NAME
        if index_path.is_file():
            for entry in read_json_object(index_path).get("schemas", []):
                if isinstance(entry, dict) and isinstance(entry.get("schema"), str):
                    if isinstance(entry.get("dialect"), str):
                        dialects[entry["schema"]] = entry["dialect"]
        schemas = []
        for entry in sorted(release_dir.iterdir()):
            if not entry.is_file() or entry.name in RESERVED_NAMES:
                continue
            schema_format(entry.name)
            schemas.append(
                PublishedSchema(entry.name, entry.read_bytes(), dialects.get(entry.name))
            )
        lock_path = release_dir / LOCK_NAME
        releases[key] = PublishedRelease(
            key=key,
            schemas=tuple(schemas),
            lock=lock_path.read_bytes() if lock_path.is_file() else None,
        )
    return releases


def write_release(
    site: Path,
    key: ReleaseKey,
    documents: list[SchemaDocument],
    lock: bytes,
    base_url: str,
) -> int:
    """Publish one release: its schemas, its lock, and its index.  Returns new schemas.

    The lock is written verbatim, so the published copy is byte-identical to the one in
    the manifest tree and the reconciler can compare the two directly.

    The index is built here, from the artifact, because the dialects come from the
    artifact and nowhere else -- the tree cannot recover them on a later run, which is
    exactly what makes a release missing its index incomplete rather than repairable.
    """
    directory = site / key.path
    written = sum(write_canonical(directory / doc.basename, doc.body) for doc in documents)
    write_canonical(directory / LOCK_NAME, lock)
    admitted = PublishedRelease(
        key=key,
        schemas=tuple(
            PublishedSchema(doc.basename, doc.body, doc.dialect)
            for doc in sorted(documents, key=lambda doc: doc.basename)
        ),
        lock=lock,
    )
    write_canonical(directory / INDEX_NAME, json_bytes(release_index(admitted, base_url)))
    return written


def release_index(release: PublishedRelease, base_url: str) -> dict[str, Any]:
    """The tree's own record of one release: membership, digests, and dialects.

    `compat` is derived in the model and stored here anyway.  The published JSON is a
    wire format frozen by immutability, not a view of the domain model: a consumer
    should not have to parse SemVer to learn which line a release belongs to, and the
    field could never be removed once published.
    """
    key = release.key
    return {
        "project": key.project,
        "version": str(key.version),
        "compat": str(key.compat),
        "schemas": [
            {
                "schema": schema.name,
                "dialect": schema.dialect,
                "url": key.url(base_url, schema.name),
                "sha256": schema.sha256,
            }
            for schema in sorted(release.schemas, key=lambda schema: schema.name)
        ],
    }


def alias_record(key: ReleaseKey, schema: PublishedSchema, base_url: str) -> dict[str, Any]:
    """Describe the release a mutable alias currently points at."""
    return {
        "schema": schema.name,
        "version": str(key.version),
        "dialect": schema.dialect,
        "url": key.url(base_url, schema.name),
        "sha256": schema.sha256,
    }


def purge_mutable(site: Path, projects: set[str]) -> None:
    """Remove regenerated resources so a rebuild cannot leave a stale alias behind.

    Mutable resources are the files directly inside a project or compat-line directory;
    canonical releases live one level deeper.  This walks a line file by file and never
    recurses, so no code path here can remove a release directory.
    """
    for project in sorted(projects):
        project_root = site / project
        latest = project_root / "latest"
        if latest.is_dir():
            shutil.rmtree(latest)
        index = project_root / INDEX_NAME
        if index.exists():
            index.unlink()
        for line in sorted(project_root.glob("v*")):
            if not line.is_dir():
                continue
            for entry in sorted(line.iterdir()):
                if entry.is_file():
                    entry.unlink()


Alias = tuple[ReleaseKey, PublishedSchema]


def newest_by(
    releases: dict[ReleaseKey, PublishedRelease],
    group: Callable[[ReleaseKey, PublishedSchema], Any],
) -> dict[Any, Alias]:
    """For each group, the schema from the newest stable release that carries it.

    Grouping by schema *name* rather than by release means a file dropped in a later
    release keeps serving from the last release that had it: an alias URL does not
    vanish because some unrelated schema was added to the line.  Prereleases are skipped
    entirely, so they publish canonically without ever moving an alias.
    """
    winners: dict[Any, Alias] = {}
    for key, release in releases.items():
        if key.version.is_prerelease:
            continue
        for schema in release.schemas:
            bucket = group(key, schema)
            current = winners.get(bucket)
            if current is None or key.version.sort_key > current[0].version.sort_key:
                winners[bucket] = (key, schema)
    return winners


def rebuild_derived(
    site: Path,
    base_url: str,
    releases: dict[ReleaseKey, PublishedRelease],
    custom_domain: bool,
) -> int:
    """Regenerate every mutable resource from the tree.  Returns the alias count.

    Aliases come from what is *published*, not from what the manifest happens to name.
    A release therefore cannot lose its line's alias by leaving the manifest, which is
    what lets the manifest stop being cumulative.
    """
    root = base_url.rstrip("/")
    projects = {key.project for key in releases}
    purge_mutable(site, projects)

    by_line = newest_by(releases, lambda key, schema: (key.project, str(key.compat), schema.name))
    by_project = newest_by(releases, lambda key, schema: (key.project, schema.name))

    for (project, compat, _name), (_key, schema) in by_line.items():
        write_mutable(site / project / f"v{compat}" / schema.name, schema.body)
    for (project, _name), (_key, schema) in by_project.items():
        write_mutable(site / project / "latest" / schema.name, schema.body)

    lines: dict[str, set[CompatLine]] = {}
    versions_by_line: dict[tuple[str, str], set[Version]] = {}
    for key in releases:
        lines.setdefault(key.project, set()).add(key.compat)
        versions_by_line.setdefault((key.project, str(key.compat)), set()).add(key.version)

    for (project, compat), versions in sorted(versions_by_line.items()):
        members = sorted(
            (alias for (p, c, _n), alias in by_line.items() if (p, c) == (project, compat)),
            key=lambda alias: alias[1].name,
        )
        write_json(site / project / f"v{compat}" / INDEX_NAME, {
            "project": project,
            "compat": compat,
            "versions": [str(v) for v in sorted(versions, key=lambda v: v.sort_key)],
            "schemas": [alias_record(key, schema, root) for key, schema in members],
        })

    latest_members: dict[str, list[Alias]] = {}
    for (project, _name), alias in by_project.items():
        latest_members.setdefault(project, []).append(alias)
    for project, members in latest_members.items():
        write_json(site / project / "latest" / INDEX_NAME, {
            "project": project,
            "schemas": [
                alias_record(key, schema, root)
                for key, schema in sorted(members, key=lambda alias: alias[1].name)
            ],
        })

    for project in sorted(projects):
        index: dict[str, Any] = {
            "project": project,
            "compat_lines": [
                str(line) for line in sorted(lines[project], key=lambda line: line.sort_key)
            ],
        }
        if project in latest_members:
            index["latest"] = f"{root}/{project}/latest/"
        write_json(site / project / INDEX_NAME, index)

    # One flat entry per published schema, so mirroring or auditing is a single fetch.
    # Derived output: regenerated whole from the tree, never read back, so a stale or
    # tampered catalog cannot affect a build.  It is simply overwritten.
    catalog = [
        {
            "project": key.project,
            "version": str(key.version),
            "schema": schema.name,
            "compat": str(key.compat),
            "dialect": schema.dialect,
            "url": key.url(root, schema.name),
            "sha256": schema.sha256,
        }
        for key, release in releases.items()
        for schema in release.schemas
    ]
    catalog.sort(key=lambda r: (r["project"], Version.parse(r["version"]).sort_key, r["schema"]))
    write_json(site / CATALOG_NAME, {"schema_catalog": 1, "schemas": catalog})
    write_json(site / INDEX_NAME, {
        "schema_site": 1,
        "catalog": f"{root}/{CATALOG_NAME}",
        "projects": sorted(projects),
    })

    hostname = urllib.parse.urlparse(base_url).hostname
    if hostname is None:
        raise ValidationError("site base URL has no hostname")
    # Pages runs Jekyll unless told otherwise, which does nothing useful for a JSON tree
    # and silently drops any path segment that starts with an underscore.
    (site / ".nojekyll").write_text("")
    cname = site / "CNAME"
    if custom_domain:
        cname.write_text(f"{hostname}\n")
    elif cname.exists():
        # A published CNAME switches Pages to the custom domain and takes the default
        # *.github.io URL down with it, so opting out has to remove the file.
        cname.unlink()
    return len(by_line) + len(by_project)
