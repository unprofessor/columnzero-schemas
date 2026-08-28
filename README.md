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

## Indexes

Every directory carries an `index.json` listing exactly one level down, which
Pages serves at the directory URL. Nothing restates the tree beneath it:

| Path | Lists |
| --- | --- |
| `/index.json` | projects |
| `/{project}/index.json` | compat lines, and `latest/` when one exists |
| `/{project}/v{compat}/index.json` | versions, and the aliases sitting beside them |
| `/{project}/v{compat}/{version}/index.json` | the schemas in that release |
| `/{project}/latest/index.json` | the schemas `latest/` currently serves |

Walking from the root reaches everything published, one fetch per level. Each
index is also a *witness* for the level below it: the line index names the
versions that must exist, the release index names the schemas and their digests.
That is what makes the tree self-describing, and it is why nothing in the build
needs a separate record of what was published.

No index records *when* a resource went live. `gh-pages` is a git branch, so the
commit that added the file already says, more accurately than a timestamp copied
out of a lockfile could:

```sh
git log --diff-filter=A --format='%aI' origin/gh-pages -- {path}
```

The release index is the only immutable one. It is rebuilt from the catalog on
every run rather than from the current lockfile, so every release that has ever
been published has one — including releases that have since left the lockfile,
which the line index above them still lists.

A compat line appears as soon as it has any release. A line holding only
prereleases resolves and lists its versions, but serves no alias, so its
`schemas` is empty.

## Catalog

`/catalog.json` lists every published schema in one flat file, deliberately
outside the hierarchy:

```text
https://schemas.columnzero.com/catalog.json
```

One entry per schema per release, each with its URL and digest. Fetching it once
gives you the whole site, where the indexes would take a walk down every level —
so it is what to use for mirroring or auditing. It is also the record the build
compares against to detect damage that predates it.

It is **derived output**. Nothing in the build reads it back — it is regenerated
whole from the tree on every run, so a stale, corrupt, or tampered catalog cannot
affect a build; it is simply overwritten. Every field in it comes from the tree:
the directory names give project, line, and version, and the release index gives
the rest.

It gains an entry with every release and never loses one, since canonical
resources are never removed, so prefer an index when you do not need all of it.

`/index.json` used to hold this catalog and is now the root index. A tree
published under the old layout needs no migration — the walk reads it from the
directories. Rolling the publisher *back* to a version that read the catalog from
`/index.json` is not safe: it would find no records there and republish a catalog
containing only what the current lockfile names.

## Status

`columnzero-schemas` publishes its own contract schema as the first and only
locked artifact:

```text
/columnzero-schemas/v1/1.0.0/schema-index.schema.json
```

No other project has shipped a `schemas.tar.gz` yet, so `manifest.toml` still
carries only the site configuration.

The `gh-pages` branch carries this tree, and Pages serves it at
`schemas.columnzero.com` — a DNS `CNAME` to `unprofessor.github.io`.

`site.custom_domain` is `true`, so the publisher writes the `CNAME` file and
preserves it across rebuilds. Setting it back to `false` **deletes** that file,
which returns Pages to the default URL and takes the canonical host down with
it. It stays true for as long as `base_url` is where these schemas live.

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

`build` downloads every artifact named in `manifest.lock` from its GitHub
release, verifies the recorded SHA-256, and writes the publication tree to
`site/` (git-ignored). It is safe to run repeatedly: a rebuild republishes
nothing and re-audits what is already there.

## Deployment invariant

The scheduled GitHub workflow copies the whole `gh-pages` tree into the build
directory, regenerates the mutable resources around what is already there, and
pushes the result back. Both copies exclude `.git`: the published tree is a
worktree whose `.git` is a file, and a `--delete` sync without that exclusion
detaches the checkout mid-run.

Four checks protect published bytes, and they fail on different things. **None of
them reads the catalog** — it is derived output, so trusting it would only mean
trusting a copy of the tree.

1. **The tree vouching for itself**, read before the build starts. Every index is
   checked against what actually exists one level down, and every schema is
   re-hashed against the digest its release index records. This catches damage
   that predates the build — an out-of-band deletion, a bad merge, a partial
   push — before the build regenerates the index that would otherwise have
   quietly stopped mentioning what went missing.
2. **Release membership.** A release's schema set is compared against what its
   release index says it contained. Check 3 cannot see this: adding a schema to
   a published release creates a file with nothing to compare against.
3. **Per-file, while writing.** Writing a canonical resource — a schema or a
   release index — compares against what is already on disk and aborts on any
   difference.
4. **The same read, again, afterwards.** Check 1 is repeated once the tree has
   been written, and everything it found the first time must still be there with
   the same digest. That catches anything this build removed or altered, and
   costs one walk rather than a second hashing pass of its own.

Structurally, the purge that clears stale aliases walks a compat line file by
file and never recurses, so no code path in the publisher can remove a release
directory. Check 4 exists because that is an argument about the code as written,
not a property the code enforces about itself.

## Lockfile completeness

`manifest.lock` is cumulative: it names every artifact that should have a live
alias, not just the newest one. Aliases are rebuilt from the lockfile on every
run, so removing an entry would take its compat line's alias down while the
releases underneath stayed published. The build refuses to do that — dropping
the last artifact in a line fails with `lockfile does not cover published compat
line(s)`.

Prereleases are published canonically but never move an alias, and never create
a compat line of their own.
