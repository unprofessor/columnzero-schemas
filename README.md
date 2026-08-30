# columnzero-schemas

An append-only schema registry, published as static files at
`https://schemas.columnzero.com`.

The only operation is *create*. Reads are the web server's job, and updates and deletes
do not exist: a canonical URL, once served, keeps serving the same bytes forever.

## Layout

Canonical resources are immutable and nest under their compatibility line:

```text
/{project}/v{compat}/{version}/{name}.schema.json
```

Convenience aliases are intentionally mutable. Each points at the newest stable release
it covers:

```text
/{project}/v{compat}/{name}.schema.json
/{project}/latest/{name}.schema.json
```

A compat line is the SemVer major (`v1`, `v2`), or `v0.{minor}` before 1.0, where the
minor is the breaking boundary. The line is a prefix of every canonical URL beneath it,
so `/planr/v1/planr.schema.json` and `/planr/v1/1.4.2/planr.schema.json` visibly
describe the same line.

The line segment is redundant — it is a function of the version — which is what makes it
worth reading. A path whose line disagrees with the release inside it is a corruption
the registry catches for free.

## The two trees

`main` declares intent. `gh-pages` is what exists. Strip one path segment and they
correspond exactly:

```text
main:     manifest/{project}/v{compat}/{version}/artifact.lock
gh-pages:          {project}/v{compat}/{version}/artifact.lock
```

The difference between the two trees *is* the work queue, so there is no central ledger
to drift from either one. Reconciling compares them and reaches one of five states:

| State | Action |
| --- | --- |
| in both, locks equal | steady state, nothing to do |
| declared, not published | admit: fetch, lint, publish |
| published, no lock | backfill: re-admit at the pinned digest to record provenance |
| published, not declared | halt — a redaction is in progress, or something was lost |
| locks disagree | halt — one of the two trees has been tampered with |

**The reconciler never deletes.** Removing a release is a human action in both trees, and
the halt above is what keeps a half-finished removal loud rather than silent.

In steady state nothing is fetched at all. A release that is already published at the
digest its lock names has nothing left to do, so a scheduled run makes no network calls —
it regenerates the derived resources over what is there and exits.

### A lock

```json
{
  "sha256": "aba2cac7a822416c365dd785baae8e223896e073506c3e3afc9f289bccf1347a",
  "url": "https://github.com/unprofessor/planr/releases/download/schemas-v1.4.2/schemas.tar.gz"
}
```

A locator and a digest, and nothing else. The path carries identity — project, line, and
version — and the release tag is a substring of the URL, so recording either would only
create a way for them to disagree.

The digest is the pin, and it is the *only* pin. A commit id would say nothing about the
bytes actually fetched: a release asset is an uploaded blob with no cryptographic link to
any commit, and SHA-256 is the stronger hash regardless. A lock URL must name a
reviewable release asset rather than an opaque redirect target.

The published copy is written verbatim, so the two trees can be compared byte for byte.

## What this does not check

The registry validates the **envelope**: archive safety, the digest, path and name
rules, and that the artifact agrees about which release it is. It does not validate the
**payload**. A schema that fails its own meta-schema is not the registry's business —
it damages only itself, and the remedy is a new release, which is the remedy anyway.

This matters because the tree is immutable. Re-validating published bytes on every run
means a stricter validator release can fail a nightly job over a schema published years
earlier that nobody is able to change. A check whose only possible outcome is "the build
is broken now" is not a safety feature.

### Admission linting

Payload checks live in linters instead: external commands, chosen by file suffix,
configured in `manifest.toml`, and run **once, on the artifact being admitted**.

```toml
[lint]
".schema.json" = ["python3", "lint/jsonschema_lint.py"]
```

```text
<argv...> <file> <canonical-url>      exit 0 accepts; anything else reports stderr
```

A rule added later never re-applies to bytes that are already frozen. Nothing about the
contract is language-specific — a CDDL linter can be a Rust binary without the registry
knowing what language it is written in. A bare `python`/`python3` is the one exception:
it resolves to the interpreter running the registry rather than to whatever `PATH` says,
so the linter shares the environment that `pip install '.[lint]'` populated. A suffix with no linter publishes unchecked;
`plan` reports that as `unlinted` rather than implying a pass.

`lint/jsonschema_lint.py` checks that a document parses, declares a `$schema`, satisfies
its declared meta-schema, and carries an `$id` equal to the URL it is about to occupy.
That last one is not quality control: `$id` is the base for `$ref` resolution, so a
schema filed at the wrong path resolves its references wrong, silently, in a way the
digest cannot catch.

## Review

A lock diff names a project, a version, and a tag. Nobody can verify a digest by reading
it, and the diff cannot show the part that reaches users. So the pull request check runs
`plan`, which fetches the artifact, checks it against its pin, lints it, and renders the
consequences — including which alias URLs move:

```text
admit     planr 1.4.2  -> line v1
          artifact  schemas-v1.4.2
          schema    planr.schema.json  https://json-schema.org/draft/2020-12/schema

aliases
  /planr/v1/planr.schema.json                          1.4.1 -> 1.4.2
  /planr/latest/planr.schema.json                      1.4.1 -> 1.4.2
```

`plan` writes nothing and holds no write permission. It is the job most exposed to
untrusted input, since it downloads and unpacks an artifact the pull request names.

## Indexes

Every directory carries an `index.json` listing exactly one level down, which Pages
serves at the directory URL. Nothing restates the tree beneath it:

| Path | Lists |
| --- | --- |
| `/index.json` | projects |
| `/{project}/index.json` | compat lines, and `latest/` when one exists |
| `/{project}/v{compat}/index.json` | versions, and the aliases sitting beside them |
| `/{project}/v{compat}/{version}/index.json` | the schemas in that release |
| `/{project}/latest/index.json` | the schemas `latest/` currently serves |

Walking from the root reaches everything published, one fetch per level.

The release index is the only immutable one. It records each schema's dialect and
digest, and it is written at admission because the dialect comes from the artifact and
nowhere else — which is why a release missing its index is one to re-admit rather than
repair. It stores `compat` even though the model derives it: the published JSON is a
wire format frozen by immutability, not a view of the domain model, and a consumer
should not have to parse SemVer to learn which line a release belongs to.

No index records *when* a resource went live. `gh-pages` is a git branch, so the commit
that added the file already says, more accurately than a copied timestamp could:

```sh
git log --diff-filter=A --format='%aI' origin/gh-pages -- {path}
```

Aliases come from the tree, not from the manifest. A release therefore cannot lose its
line's alias by leaving the manifest, which is what lets the manifest stop being
cumulative. A compat line appears as soon as it has any release; a line holding only
prereleases resolves and lists its versions but serves no alias, so its `schemas` is
empty.

## Catalog

`/catalog.json` lists every published schema in one flat file, deliberately outside the
hierarchy. One entry per schema per release, each with its URL and digest. Fetching it
once gives you the whole site, where the indexes would take a walk down every level — so
it is what to use for mirroring or auditing.

It is **derived output**. Nothing in the build reads it back; it is regenerated whole
from the tree on every run, so a stale, corrupt, or tampered catalog cannot affect a
build. It gains an entry with every release and never loses one.

## Browsing

Every directory that is not a release carries an `index.html`, so the tree is walkable in
a browser as well as by fetch:

| Path | Page |
| --- | --- |
| `/` | projects, and the catalog |
| `/{project}/` | compat lines, and `latest/` when one exists |
| `/{project}/v{compat}/` | the line's alias URLs, and every release with its schemas |
| `/{project}/latest/` | the schemas `latest/` currently serves |

The pages are **derived output** on the same terms as the catalog: regenerated whole from
the tree on every run and never read back. Nothing parses an `index.json` to build one. A
page is a second projection of the same releases rather than a rendering of the first, so
the published JSON stays free to change shape without a page quietly depending on it, and
a tampered page changes nothing but itself until the next run.

**No page is written inside a release directory.** Everything below one is canonical and
append-only, so a page written there would be frozen at whatever markup shipped first and
the next restyle would fail the build rather than reach it. A release directory therefore
has no page and its directory URL 404s in a browser. It stays browsable from its line
page, which links each version's schemas directly, alongside that release's `index.json`
and `artifact.lock`.

## Immutability

Immutability is enforced by **git**, not by anything inside either tree. Once a rebuild
is staged, the workflow refuses to publish if any path under a release directory is
reported as anything but additive:

```sh
git diff --cached --name-status | python -m czschemas verify --tree site
```

The same check guards the manifest tree on a pull request, because a lock that can be
edited is a pin that can be moved after review:

```sh
git diff --name-status origin/main...HEAD | python -m czschemas verify --tree manifest
```

The rule is an allowlist — `A` and `C` pass, everything else fails. Naming the bad
statuses instead would make the check only as complete as that list: `T`, a regular file
swapped for a symlink, was missing from exactly such a list, and it leaves every index
byte-identical.

This is deliberately a check the trees cannot influence. Any record kept *inside* a tree
is editable by whoever edits the tree: drop the field an index is checked on and the
check vanishes with it. The previous commit is not reachable from the working copy, so
the same edit cannot scrub the evidence.

```sh
git log --diff-filter=DM --format='%h %aI %s' --name-only origin/gh-pages
```

Writing a canonical resource still compares against what is on disk and aborts on a
difference. That is a convenience — a local run fails at the point of cause, with a
precise message — not the guarantee.

Structurally, the purge that clears stale aliases walks a compat line file by file and
never recurses, so no code path in the registry can remove a release directory.

## Redaction

Direct, out-of-band edits to `gh-pages` are the administrative escape hatch. This is
**not** part of normal operation. It exists because a mistake that reached a canonical
URL cannot be fixed in place, and is reserved for supervised, exceptional cases.

The hatch needs no code: the gate runs only inside the workflow, so a human with push
access bypasses it. That is the point — break-glass should require stepping outside the
automation rather than flipping a flag inside it. Restricting pushes to `gh-pages` to
the Actions identity is what makes it supervised rather than merely discouraged.

**Order matters. Remove the lock from `main` first.**

1. Merge a pull request deleting `manifest/{project}/v{compat}/{version}/artifact.lock`
2. Reconciling now halts with `published but not declared` — loudly, and publishing
   stops for every project until the removal finishes
3. `rm -rf {project}/v{compat}/{version}/` on `gh-pages` and push

Delete whole release directories, never single files: dropping one schema while its
release index still lists it wedges every later run on an index that can no longer be
regenerated. The reverse order fails differently — with the lock still in `main`, the
next run re-admits the release and silently undoes the redaction.

What you cannot know is whether anyone fetched it. Pages keeps no access logs, and these
are machine-fetched JSON files, so "no user saw it" is unfalsifiable rather than merely
unknown. The risk is not uniform, though: a prerelease never moves an alias, so it is
reachable only by someone who read a version index and built the URL deliberately.
Anything that has been a `latest/` target is a different matter. Mirrors are the one
place a deletion makes a sound — `catalog.json` is the advertised mirroring endpoint, and
a mirror holding a URL the origin denies is indistinguishable from one that invented it.
Put the reason in the `gh-pages` commit message; git is the only record of it.

## Upstream artifact contract

A release supplies a deterministic `schemas.tar.gz` containing a root `index.json` and
the files named by that index:

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

`project` and `release` must match the path the lock was declared at, and `compat` must
be exactly the line that release belongs to. Every schema file must be named
`<name>.schema.json`; the suffix is a closed registry, so an unrecognised extension is
refused rather than published as an unknown format.

## Self-test

`selftest/` holds this repository's own schema, `schema-index.schema.json`, which
describes the artifact `index.json` above. Because the artifact carries both the schema
and an index conforming to it, it is the one artifact that checks its own payload.

```sh
.venv/bin/python selftest/pack.py    # deterministic selftest/schemas.tar.gz + sha256
```

`tests/test_selftest.py` rebuilds the archive, asserts it is byte-reproducible, validates
the index against the shipped schema, and asserts the declared digest still matches the
source tree. Editing the schema after release therefore fails the suite rather than
silently diverging from the published bytes; the fix is a new release, never a republish.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[lint]'
.venv/bin/python -m unittest discover -s tests -v

.venv/bin/python -m czschemas plan  --site site
.venv/bin/python -m czschemas apply --site site
```

The registry itself is pure standard library. `jsonschema` is an optional extra needed
only by the JSON Schema linter, which runs as a separate process.

Components:

| Module | Responsibility |
| --- | --- |
| `model` | parsed identities: version, compat line, release key, lock |
| `fetch` | the only module that touches the network or opens an archive |
| `lint` | pluggable admission checks, external and per-format |
| `registry` | the publication tree and the derived resources around it |
| `reconcile` | the manifest tree vs. the publication tree |
| `gate` | the immutability verdict, taken against git |
