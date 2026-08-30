"""Tests for the registry: identity, fetching, the tree, and reconciliation.

The registry is append-only and format-agnostic, so most of what is asserted here is
about the *envelope* -- names, paths, digests, and immutability -- rather than about
what any schema means.  Meaning is the linter's business, and it has its own tests.
"""

import hashlib
import importlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from czschemas import gate, lint, model, reconcile, registry          # noqa: E402
from czschemas.config import Config                                   # noqa: E402
from czschemas.model import (                                         # noqa: E402
    ArtifactLock,
    ImmutabilityError,
    ReleaseKey,
    ValidationError,
    Version,
)
from czschemas import fetch                                           # noqa: E402


BASE_URL = "https://schemas.columnzero.com"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def key(version: str, project: str = "planr") -> ReleaseKey:
    return ReleaseKey(project, Version.parse(version))


def config(linters: dict | None = None, custom_domain: bool = False) -> Config:
    return Config(BASE_URL, custom_domain, linters or {})


def schema_body(release: ReleaseKey, name: str, id_url: str | None = None) -> bytes:
    return json.dumps({
        "$schema": DIALECT,
        "$id": id_url if id_url is not None else release.url(BASE_URL, name),
        "type": "object",
    }).encode()


def make_artifact(
    release: ReleaseKey,
    names: tuple[str, ...] = ("planr.schema.json",),
    *,
    index: dict | None = None,
    bodies: dict[str, bytes] | None = None,
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    """A gzip tar carrying a root index.json and the schemas it names."""
    if index is None:
        index = {
            "schema_index": 1,
            "project": release.project,
            "release": str(release.version),
            "schemas": [
                {"path": name, "compat": str(release.compat), "dialect": DIALECT}
                for name in names
            ],
        }
    members = [("index.json", json.dumps(index).encode())]
    for name in names:
        body = (bodies or {}).get(name) or schema_body(release, PurePosixPath(name).name)
        members.append((name, body))
    members.extend(extra_members)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class Registry:
    """A manifest tree and a publication tree, with a stubbed download between them."""

    def __init__(self, root: Path):
        self.root = root
        self.site = root / "site"
        self.payloads: dict[str, bytes] = {}
        self.downloads: list[str] = []

    def url(self, release: ReleaseKey) -> str:
        return (
            f"https://github.com/unprofessor/{release.project}/releases/download/"
            f"schemas-v{release.version}/schemas.tar.gz"
        )

    def declare(self, release: ReleaseKey, payload: bytes) -> ArtifactLock:
        lock = ArtifactLock(self.url(release), hashlib.sha256(payload).hexdigest())
        path = self.root / reconcile.MANIFEST_ROOT / release.path / registry.LOCK_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(registry.json_bytes({"sha256": lock.sha256, "url": lock.url}))
        self.payloads[lock.url] = payload
        return lock

    def publish(self, release: ReleaseKey, names=("planr.schema.json",), **kwargs) -> ArtifactLock:
        return self.declare(release, make_artifact(release, names, **kwargs))

    def undeclare(self, release: ReleaseKey) -> None:
        (self.root / reconcile.MANIFEST_ROOT / release.path / registry.LOCK_NAME).unlink()

    def download(self, lock: ArtifactLock) -> bytes:
        self.downloads.append(lock.url)
        return self.payloads[lock.url]

    def apply(self, cfg: Config | None = None, **kwargs):
        self.downloads.clear()
        return reconcile.apply(
            self.root, self.site, cfg or config(), on_download=self.download, **kwargs
        )

    def plan(self, **kwargs) -> reconcile.Plan:
        published = registry.read_published(self.site) if self.site.is_dir() else {}
        return reconcile.plan(
            reconcile.declared(self.root), published,
            enforce_undeclared=kwargs.pop("enforce_undeclared", True),
        )

    def read(self, relative: str) -> dict:
        return json.loads((self.site / relative).read_text())


class TempCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.registry = Registry(self.root)

    def tearDown(self):
        self._temp.cleanup()


class VersionTests(unittest.TestCase):
    def test_prerelease_identifiers_compare_numerically(self):
        """SemVer section 11: rc.9 precedes rc.10, which flat string ordering reverses."""
        order = sorted(
            (Version.parse(v) for v in ["1.0.0-rc.10", "1.0.0-rc.9", "1.0.0-rc.2"]),
            key=lambda v: v.sort_key,
        )
        self.assertEqual([str(v) for v in order], ["1.0.0-rc.2", "1.0.0-rc.9", "1.0.0-rc.10"])

    def test_a_release_outranks_every_prerelease_of_itself(self):
        newest = max(
            (Version.parse(v) for v in ["1.0.0", "1.0.0-rc.1", "1.0.0-zzz"]),
            key=lambda v: v.sort_key,
        )
        self.assertEqual(str(newest), "1.0.0")

    def test_a_longer_identifier_list_outranks_the_prefix_it_extends(self):
        self.assertGreater(Version.parse("1.0.0-rc.1").sort_key, Version.parse("1.0.0-rc").sort_key)

    def test_compat_line_is_the_major_or_zero_minor(self):
        self.assertEqual(Version.parse("2.1.0").compat.segment, "v2")
        self.assertEqual(Version.parse("0.3.1").compat.segment, "v0.3")

    def test_versions_round_trip_through_their_text(self):
        for text in ["1.4.2", "0.3.1", "1.0.0-rc.2", "2.0.0-alpha.1.beta"]:
            self.assertEqual(str(Version.parse(text)), text)

    def test_build_metadata_is_rejected(self):
        """Metadata is excluded from precedence, so two such versions would share a rank
        while competing for one directory."""
        with self.assertRaises(ValidationError):
            Version.parse("1.0.0+build.5")


class ReleaseKeyTests(unittest.TestCase):
    def test_path_is_project_line_version(self):
        self.assertEqual(str(key("1.4.2").path), "planr/v1/1.4.2")

    def test_a_path_whose_line_disagrees_with_its_version_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "does not describe"):
            ReleaseKey.from_path("planr/v2/1.4.2")

    def test_path_round_trips(self):
        for path in ["planr/v1/1.4.2", "planr/v0.3/0.3.1", "planr/v1/1.0.0-rc.1"]:
            self.assertEqual(str(ReleaseKey.from_path(path).path), path)

    def test_unregistered_suffixes_cannot_be_published(self):
        with self.assertRaisesRegex(ValidationError, "must end in"):
            key("1.4.2").url(BASE_URL, "planr.cddl")


class ArtifactLockTests(unittest.TestCase):
    def test_lock_holds_exactly_a_locator_and_a_digest(self):
        body = json.dumps({"url": "https://x", "sha256": "ab" * 32, "tag": "v1"}).encode()
        with self.assertRaisesRegex(ValidationError, "exactly url, sha256"):
            ArtifactLock.parse(body, "lock")

    def test_digest_must_be_lowercase_hex(self):
        body = json.dumps({"url": "https://x", "sha256": "AB" * 32}).encode()
        with self.assertRaisesRegex(ValidationError, "64 lowercase hex"):
            ArtifactLock.parse(body, "lock")

    def test_lock_url_must_name_a_reviewable_release_asset(self):
        """The redirect hosts are reachable during a download but opaque in a lock: a
        human cannot tell which release an asset URL names."""
        fetch.validate_lock_url(
            "https://github.com/o/r/releases/download/schemas-v1.0.0/schemas.tar.gz"
        )
        for bad in [
            "https://objects.githubusercontent.com/blob/abc123",
            "https://github.com/o/r/archive/refs/tags/v1.tar.gz",
            "http://github.com/o/r/releases/download/v1/a.tar.gz",
            "https://evil.test/o/r/releases/download/v1/a.tar.gz",
        ]:
            with self.assertRaises(ValidationError, msg=bad):
                fetch.validate_lock_url(bad)

    def test_tag_is_derived_from_the_url_rather_than_stored(self):
        url = "https://github.com/o/r/releases/download/schemas-v1.4.2/schemas.tar.gz"
        self.assertEqual(fetch.release_tag(url), "schemas-v1.4.2")


class FetchTests(TempCase):
    def test_digest_mismatch_is_refused(self):
        release = key("1.4.2")
        lock = ArtifactLock(self.registry.url(release), "ab" * 32)
        with self.assertRaisesRegex(ValidationError, "sha256 mismatch"):
            fetch.verify(release, lock, make_artifact(release))

    def test_archive_paths_cannot_escape(self):
        with self.assertRaisesRegex(ValidationError, "unsafe archive path"):
            fetch.read_archive(
                make_artifact(key("1.4.2"), extra_members=(("../escape.json", b"{}"),))
            )

    def test_index_must_agree_with_the_release_it_is_admitted_as(self):
        release = key("1.4.2")
        payload = make_artifact(key("1.4.3"))
        lock = ArtifactLock(self.registry.url(release), hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(ValidationError, "index release"):
            fetch.verify(release, lock, payload)

    def test_compat_that_disagrees_with_the_release_is_refused(self):
        release = key("1.4.2")
        index = {
            "schema_index": 1, "project": "planr", "release": "1.4.2",
            "schemas": [{"path": "planr.schema.json", "compat": "2", "dialect": DIALECT}],
        }
        payload = make_artifact(release, index=index)
        lock = ArtifactLock(self.registry.url(release), hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(ValidationError, "does not describe release"):
            fetch.verify(release, lock, payload)

    def test_basenames_must_be_unique_across_an_index(self):
        """Two schemas landing on one published filename would silently take turns."""
        release = key("1.4.2")
        index = {
            "schema_index": 1, "project": "planr", "release": "1.4.2",
            "schemas": [
                {"path": "a/planr.schema.json", "compat": "1", "dialect": DIALECT},
                {"path": "b/planr.schema.json", "compat": "1", "dialect": DIALECT},
            ],
        }
        payload = make_artifact(
            release, ("a/planr.schema.json", "b/planr.schema.json"), index=index
        )
        lock = ArtifactLock(self.registry.url(release), hashlib.sha256(payload).hexdigest())
        with self.assertRaisesRegex(ValidationError, "duplicate schema path"):
            fetch.verify(release, lock, payload)

    def test_a_malformed_schema_is_not_the_registrys_business(self):
        """The registry hosts bytes.  Whether they parse is admission linting's problem,
        and with no linter configured a release publishes unchecked by design."""
        release = key("1.4.2")
        self.registry.publish(release, bodies={"planr.schema.json": b"this is not JSON"})
        self.registry.apply()
        published = self.registry.site / release.path / "planr.schema.json"
        self.assertEqual(published.read_bytes(), b"this is not JSON")


class ReconcileTests(TempCase):
    def test_a_declared_release_is_admitted_and_published(self):
        release = key("1.4.2")
        self.registry.publish(release)
        result = self.registry.apply()
        self.assertEqual(result["admitted"], 1)
        self.assertEqual(result["published"], 1)
        directory = self.registry.site / release.path
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            ["artifact.lock", "index.json", "planr.schema.json"],
        )

    def test_steady_state_does_no_work_and_makes_no_network_call(self):
        """The queue is the difference between two trees, so once they agree there is
        nothing to fetch -- and nothing to discover about an upstream we cannot fix."""
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        result = self.registry.apply()
        self.assertEqual(result["admitted"], 0)
        self.assertEqual(self.registry.downloads, [])

    def test_the_published_lock_is_byte_identical_to_the_declared_one(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        declared = self.root / reconcile.MANIFEST_ROOT / release.path / registry.LOCK_NAME
        published = self.registry.site / release.path / registry.LOCK_NAME
        self.assertEqual(declared.read_bytes(), published.read_bytes())

    def test_a_release_published_but_not_declared_halts_the_run(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        self.registry.undeclare(release)
        plan = self.registry.plan()
        self.assertEqual(plan.admit, ())
        self.assertTrue(any("published but not declared" in reason for reason in plan.halt))

    def test_a_halt_costs_no_network(self):
        """Nothing is fetched until the plan is clean, so a halted run cannot spend a
        download discovering it was going to refuse anyway."""
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        self.registry.undeclare(key("1.4.2"))
        self.registry.publish(key("1.5.0"))
        with self.assertRaisesRegex(ValidationError, "reconciliation refused"):
            self.registry.apply()
        self.assertEqual(self.registry.downloads, [])

    def test_locks_that_disagree_between_the_trees_halt_the_run(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        tampered = registry.json_bytes({"sha256": "cd" * 32, "url": self.registry.url(release)})
        (self.root / reconcile.MANIFEST_ROOT / release.path / registry.LOCK_NAME).write_bytes(tampered)
        plan = self.registry.plan()
        self.assertTrue(any("lock differs" in reason for reason in plan.halt))

    def test_a_release_published_without_a_lock_is_backfilled(self):
        """The migration path: declare a lock for something already published, and the
        reconciler re-admits it at the pinned digest to record its provenance."""
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        (self.registry.site / release.path / registry.LOCK_NAME).unlink()
        (self.registry.site / release.path / registry.INDEX_NAME).unlink()
        plan = self.registry.plan()
        self.assertEqual(plan.admit, (release,))
        self.assertEqual(plan.backfill, frozenset({release}))
        result = self.registry.apply()
        self.assertEqual(result["published"], 0)      # the bytes were already there
        self.assertTrue((self.registry.site / release.path / registry.LOCK_NAME).is_file())

    def test_backfill_cannot_change_the_bytes_it_backfills(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        (self.registry.site / release.path / registry.LOCK_NAME).unlink()
        (self.registry.site / release.path / "planr.schema.json").write_bytes(b"{}")
        with self.assertRaises(ImmutabilityError):
            self.registry.apply()

    def test_undeclared_releases_are_tolerated_during_migration_only(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        self.registry.undeclare(release)
        self.assertTrue(self.registry.plan().halt)
        self.assertFalse(self.registry.plan(enforce_undeclared=False).halt)

    def test_the_manifest_tree_holds_only_locks(self):
        stray = self.root / reconcile.MANIFEST_ROOT / "planr" / "v1" / "1.4.2" / "notes.md"
        stray.parent.mkdir(parents=True)
        stray.write_text("hello")
        with self.assertRaisesRegex(ValidationError, "only artifact.lock"):
            reconcile.declared(self.root)


class AliasTests(TempCase):
    def test_aliases_follow_the_newest_stable_release(self):
        for version in ["1.4.1", "1.4.2"]:
            self.registry.publish(key(version))
        self.registry.apply()
        line = self.registry.read("planr/v1/index.json")
        self.assertEqual([s["version"] for s in line["schemas"]], ["1.4.2"])
        self.assertEqual(line["versions"], ["1.4.1", "1.4.2"])

    def test_a_prerelease_publishes_without_moving_an_alias(self):
        self.registry.publish(key("1.4.1"))
        self.registry.publish(key("1.5.0-rc.1"))
        self.registry.apply()
        line = self.registry.read("planr/v1/index.json")
        self.assertEqual([s["version"] for s in line["schemas"]], ["1.4.1"])
        self.assertIn("1.5.0-rc.1", line["versions"])
        self.assertTrue((self.registry.site / "planr/v1/1.5.0-rc.1").is_dir())

    def test_a_line_holding_only_prereleases_serves_no_alias(self):
        self.registry.publish(key("2.0.0-rc.1"))
        self.registry.apply()
        self.assertEqual(self.registry.read("planr/v2/index.json")["schemas"], [])

    def test_dropping_a_lock_does_not_take_its_alias_down(self):
        """Aliases come from the tree, not the manifest, so the manifest never has to be
        cumulative -- and removing an entry cannot 404 a URL the index advertises."""
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        self.registry.undeclare(release)
        self.registry.apply(enforce_undeclared=False)
        self.assertTrue((self.registry.site / "planr/v1/planr.schema.json").is_file())
        self.assertEqual(
            [s["version"] for s in self.registry.read("planr/v1/index.json")["schemas"]], ["1.4.2"]
        )

    def test_a_schema_dropped_by_a_later_release_keeps_serving_from_the_last_one(self):
        self.registry.publish(key("1.4.1"), ("planr.schema.json", "legacy.schema.json"))
        self.registry.publish(key("1.4.2"), ("planr.schema.json",))
        self.registry.apply()
        records = {s["schema"]: s["version"] for s in self.registry.read("planr/v1/index.json")["schemas"]}
        self.assertEqual(records, {"planr.schema.json": "1.4.2", "legacy.schema.json": "1.4.1"})

    def test_each_compat_line_keeps_its_own_alias(self):
        self.registry.publish(key("1.4.2"))
        self.registry.publish(key("2.0.0"))
        self.registry.apply()
        self.assertEqual(
            self.registry.read("planr/index.json")["compat_lines"], ["1", "2"]
        )
        self.assertEqual(
            [s["version"] for s in self.registry.read("planr/v1/index.json")["schemas"]], ["1.4.2"]
        )
        self.assertEqual(
            [s["version"] for s in self.registry.read("planr/latest/index.json")["schemas"]], ["2.0.0"]
        )

    def test_a_stale_alias_is_purged_rather_than_left_behind(self):
        self.registry.publish(key("1.4.1"), ("legacy.schema.json",))
        self.registry.apply()
        self.assertTrue((self.registry.site / "planr/v1/legacy.schema.json").is_file())
        # A later release drops the file; the alias must go with it once nothing serves it.
        (self.root / reconcile.MANIFEST_ROOT / key("1.4.1").path).rename(
            self.root / reconcile.MANIFEST_ROOT / "planr" / "v1" / "unused"
        )
        import shutil
        shutil.rmtree(self.root / reconcile.MANIFEST_ROOT / "planr" / "v1" / "unused")
        shutil.rmtree(self.registry.site / key("1.4.1").path)
        self.registry.publish(key("1.4.2"), ("planr.schema.json",))
        self.registry.apply()
        self.assertFalse((self.registry.site / "planr/v1/legacy.schema.json").exists())


class ImmutabilityTests(TempCase):
    def test_a_canonical_resource_cannot_be_changed(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        (self.registry.site / release.path / "planr.schema.json").write_bytes(b"{}")
        with self.assertRaises(ImmutabilityError):
            self.registry.apply()

    def test_re_declaring_a_version_with_a_different_artifact_halts(self):
        """Membership is pinned twice over: the digest fixes the bytes, and a lock that
        changed to admit different bytes is caught before anything is fetched."""
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        self.registry.publish(release, ("planr.schema.json", "extra.schema.json"))
        with self.assertRaisesRegex(ValidationError, "lock differs"):
            self.registry.apply()
        self.assertFalse((self.registry.site / release.path / "extra.schema.json").exists())

    def test_release_membership_cannot_change_at_the_write_layer_either(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        documents = fetch.verify(
            release,
            ArtifactLock(self.registry.url(release),
                         hashlib.sha256(make_artifact(release, ("planr.schema.json", "extra.schema.json"))).hexdigest()),
            make_artifact(release, ("planr.schema.json", "extra.schema.json")),
        )
        with self.assertRaises(ImmutabilityError):
            registry.write_release(self.registry.site, release, documents, b"{}", BASE_URL)

    def test_a_release_directory_survives_the_mutable_purge(self):
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        registry.purge_mutable(self.registry.site, {"planr"})
        self.assertTrue((self.registry.site / "planr/v1/1.4.2/planr.schema.json").is_file())
        self.assertFalse((self.registry.site / "planr/v1/planr.schema.json").exists())

    def test_the_catalog_is_output_only_and_cannot_poison_a_build(self):
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        (self.registry.site / registry.CATALOG_NAME).write_text('{"schemas": [{"bogus": 1}]}')
        self.registry.apply()
        catalog = self.registry.read(registry.CATALOG_NAME)
        self.assertEqual([s["version"] for s in catalog["schemas"]], ["1.4.2"])

    def test_a_tree_with_no_indexes_is_still_read_from_its_directories(self):
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        for path in self.registry.site.rglob("index.json"):
            path.unlink()
        self.registry.apply()
        self.assertEqual(
            [s["version"] for s in self.registry.read(registry.CATALOG_NAME)["schemas"]], ["1.4.2"]
        )


class GateTests(unittest.TestCase):
    def test_additions_and_copies_are_not_violations(self):
        diff = "A\tplanr/v1/1.4.2/planr.schema.json\nC100\tplanr/v1/1.4.2/b.schema.json"
        self.assertEqual(gate.violations(diff), [])

    def test_modification_deletion_rename_and_typechange_are_violations(self):
        for status in ["M", "D", "R100", "T"]:
            with self.subTest(status=status):
                diff = f"{status}\tplanr/v1/1.4.2/planr.schema.json"
                self.assertEqual(gate.violations(diff), [f"{status[0]} planr/v1/1.4.2/planr.schema.json"])

    def test_the_rule_is_an_allowlist_not_a_list_of_known_bad_statuses(self):
        """A status git has yet to invent must fail closed."""
        self.assertEqual(
            gate.violations("X\tplanr/v1/1.4.2/planr.schema.json"),
            ["X planr/v1/1.4.2/planr.schema.json"],
        )

    def test_a_published_lock_is_canonical_like_everything_beside_it(self):
        self.assertEqual(
            gate.violations("M\tplanr/v1/1.4.2/artifact.lock"),
            ["M planr/v1/1.4.2/artifact.lock"],
        )

    def test_mutable_resources_may_change_freely(self):
        diff = "M\tplanr/v1/index.json\nM\tcatalog.json\nM\tplanr/latest/planr.schema.json"
        self.assertEqual(gate.violations(diff), [])

    def test_the_manifest_tree_is_append_only_too(self):
        diff = "D\tmanifest/planr/v1/1.4.2/artifact.lock\nA\tmanifest/planr/v1/1.5.0/artifact.lock"
        self.assertEqual(
            gate.violations(diff, "manifest"), ["D manifest/planr/v1/1.4.2/artifact.lock"]
        )

    def test_blank_and_malformed_lines_are_ignored(self):
        self.assertEqual(gate.violations("\n\nnot-a-diff-line\n"), [])


class LintTests(TempCase):
    """The linter is an external process, so these run the real contract."""

    def linters(self) -> dict[str, list[str]]:
        return {".schema.json": [sys.executable, str(ROOT / "lint" / "jsonschema_lint.py")]}

    def test_a_schema_filed_at_the_wrong_url_is_refused(self):
        release = key("1.4.2")
        wrong = schema_body(release, "planr.schema.json", id_url=f"{BASE_URL}/planr/v1/9.9.9/planr.schema.json")
        self.registry.publish(release, bodies={"planr.schema.json": wrong})
        with self.assertRaisesRegex(ValidationError, r"\$id must equal"):
            self.registry.apply(config(self.linters()))

    def test_a_schema_that_fails_its_meta_schema_is_refused(self):
        release = key("1.4.2")
        broken = json.dumps({
            "$schema": DIALECT, "$id": release.url(BASE_URL, "planr.schema.json"),
            "type": "not-a-type",
        }).encode()
        self.registry.publish(release, bodies={"planr.schema.json": broken})
        with self.assertRaisesRegex(ValidationError, "meta-schema"):
            self.registry.apply(config(self.linters()))

    def test_a_refused_admission_publishes_nothing(self):
        release = key("1.4.2")
        self.registry.publish(release, bodies={"planr.schema.json": b"not json"})
        with self.assertRaises(ValidationError):
            self.registry.apply(config(self.linters()))
        self.assertFalse((self.registry.site / release.path).exists())

    def test_lint_does_not_re_run_on_what_is_already_published(self):
        """A rule added later must not fail a run over bytes that are already frozen."""
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply(config())                       # admitted with no linter
        refuse = {".schema.json": [sys.executable, "-c", "import sys; sys.exit(1)"]}
        result = self.registry.apply(config(refuse))        # now everything would fail
        self.assertEqual(result["admitted"], 0)

    def test_a_suffix_with_no_linter_is_reported_rather_than_implied_to_pass(self):
        documents = fetch.verify(
            key("1.4.2"),
            ArtifactLock(self.registry.url(key("1.4.2")),
                         hashlib.sha256(make_artifact(key("1.4.2"))).hexdigest()),
            make_artifact(key("1.4.2")),
        )
        self.assertEqual(lint.unlinted({}, documents), ["planr.schema.json"])
        self.assertEqual(lint.unlinted(self.linters(), documents), [])

    def test_an_unknown_suffix_cannot_be_configured(self):
        with self.assertRaisesRegex(ValidationError, "not a known suffix"):
            lint.load({".cddl": ["cddl", "compile"]})


class RedactionTests(TempCase):
    """Break-glass removal is a human action in both trees; the order is load-bearing."""

    def test_removing_from_gh_pages_first_is_undone_by_the_next_run(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        import shutil
        shutil.rmtree(self.registry.site / release.path)
        self.registry.apply()
        self.assertTrue((self.registry.site / release.path / "planr.schema.json").is_file())

    def test_removing_from_the_manifest_first_halts_until_the_tree_follows(self):
        release = key("1.4.2")
        self.registry.publish(release)
        self.registry.apply()
        self.registry.undeclare(release)
        with self.assertRaisesRegex(ValidationError, "published but not declared"):
            self.registry.apply()
        import shutil
        shutil.rmtree(self.registry.site / release.path)
        result = self.registry.apply()
        self.assertEqual(result["releases"], 0)

    def test_a_half_finished_redaction_blocks_unrelated_publishing(self):
        self.registry.publish(key("1.4.2"))
        self.registry.apply()
        self.registry.undeclare(key("1.4.2"))
        self.registry.publish(key("2.0.0"))
        with self.assertRaises(ValidationError):
            self.registry.apply()
        self.assertFalse((self.registry.site / "planr/v2").exists())


if __name__ == "__main__":
    unittest.main()
