# columnzero-schemas

Immutable JSON Schema publication for `https://schemas.columnzero.com`.

Canonical resources use:

```text
/{project}/rel/{version}/{name}.schema.json
```

Convenience aliases are intentionally mutable:

```text
/{project}/compat/{compat}/{name}.schema.json
/{project}/latest/{name}.schema.json
```

The publisher consumes a locked GitHub release artifact rather than source-tree
contents. It validates the schema index, archive safety, artifact SHA-256, JSON
Schema meta-schema, and canonical `$id`; it refuses to modify an existing
canonical resource.

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
  "$id": "https://schemas.columnzero.com/planr/rel/1.4.2/planr.schema.json"
}
```

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

The scheduled GitHub workflow preserves any existing `rel/` resources from the
`gh-pages` branch and regenerates aliases and indexes around them. A differing
byte at an existing canonical URL aborts the run; it is never republished.
