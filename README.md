# columnzero-schemas

Immutable JSON Schema publication for `https://schemas.columnzero.com`.

Canonical resources are immutable and nest under their compatibility line:

```text
/{project}/v{compat}/{version}/{name}.schema.json
```

Convenience aliases are intentionally mutable. Each points at the newest stable
release it covers:

```text
/{project}/v{compat}/{name}.schema.json
/{project}/latest/{name}.schema.json
```

A compat line is the SemVer major (`v1`, `v2`), or `v0.{minor}` before 1.0,
where the minor is the breaking boundary. The line is a prefix of every
canonical URL beneath it, so `/planr/v1/planr.schema.json` and
`/planr/v1/1.4.2/planr.schema.json` visibly describe the same line.

The publisher consumes a locked GitHub release artifact rather than source-tree
contents. It validates the schema index, archive safety, artifact SHA-256, JSON
Schema meta-schema, and canonical `$id`; it refuses to modify or drop an
existing canonical resource.

## Status

The repository is scaffolded, but no upstream project has shipped a schema
artifact yet. `manifest.toml` and `manifest.lock` are therefore intentionally
empty apart from the site configuration.

## Upstream artifact contract

A release supplies deterministic `schemas.tar.gz` containing a root `index.json`
and the files named by that index:

```json
{
  "schema_index": 1,
  "project": "planr",
  "release": "1.4.2",
  "schemas": [{
    "path": "planr.schema.json",
    "compat": "1",
    "dialect": "https://json-schema.org/draft/2020-12/schema"
  }]
}
```

Each listed schema must contain a matching `$schema` and canonical `$id`, for
example:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://schemas.columnzero.com/planr/v1/1.4.2/planr.schema.json"
}
```

Two naming rules keep the release directories and the alias files that share a
parent from ever colliding:

- `compat` must be exactly the line the release belongs to. Publishing `1.4.2`
  under `compat: "2"` is rejected rather than filed in the wrong line.
- every schema file must be named `<name>.schema.json`. No SemVer version can
  end in that suffix, so an alias file can never shadow a version directory.

## Self-test

`selftest/` holds this repository's own schema: `schema-index.schema.json`, which
describes the artifact `index.json` above. Upstreams can validate against it, and
because the artifact carries both the schema and an index conforming to it, it is
the one artifact that checks its own payload.

```sh
.venv/bin/python selftest/pack.py    # deterministic selftest/schemas.tar.gz + sha256
```

`tests/test_selftest.py` rebuilds the archive, asserts it is byte-reproducible,
validates the index against the shipped schema, and — once the artifact is locked —
asserts the recorded SHA-256 still matches the source tree. Editing the schema after
release therefore fails the suite rather than silently diverging from the published
bytes; the fix is a new release, never a republish.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python src/czschemas.py build
```

The empty initial lockfile produces only `site/index.json` and `site/CNAME`.
Publishing begins only after a locked artifact has been added.

## Deployment invariant

The scheduled GitHub workflow copies the whole `gh-pages` tree into the build
directory, regenerates the mutable resources around what is already there, and
pushes the result back.

Two independent checks protect published bytes. Writing a canonical resource
compares against what is already on disk and aborts on any difference. Then,
after the tree is built, every entry in the *previous* catalog is re-read and
re-hashed: a release that went missing or changed fails the build, whether or
not the current lockfile still names it.

The purge that clears stale aliases walks a compat line file by file and never
recurses, so no code path in the publisher can remove a release directory.

## Lockfile completeness

`manifest.lock` is cumulative: it names every artifact that should have a live
alias, not just the newest one. Aliases are rebuilt from the lockfile on every
run, so removing an entry would take its compat line's alias down while the
releases underneath stayed published. The build refuses to do that — dropping
the last artifact in a line fails with `lockfile does not cover published compat
line(s)`.

Prereleases are published canonically but never move an alias, and never create
a compat line of their own.
