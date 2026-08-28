"""The publisher's self-test: this repository publishes its own artifact contract.

`selftest/schema-index.schema.json` describes the root `index.json` that every upstream
artifact must carry, so the artifact we build from it is the one artifact whose index
can be validated against its own payload.  These tests keep the source schema, the
reproducible archive, and the SHA-256 recorded in `manifest.lock` in agreement.
"""

import hashlib
import importlib.util
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


czschemas = _load("czschemas", ROOT / "src" / "czschemas.py")
pack = _load("selftest_pack", ROOT / "selftest" / "pack.py")

BASE_URL = "https://schemas.columnzero.com"


def build_payload() -> bytes:
    members = [("index.json", pack.build_index())]
    for name in pack.MEMBERS:
        members.append((name, (ROOT / "selftest" / name).read_bytes()))
    return pack.pack(members)


class SelfTestArtifactTests(unittest.TestCase):
    def setUp(self):
        self.payload = build_payload()
        self.members = {}
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as handle:
            handle.write(self.payload)
            handle.flush()
            with tarfile.open(handle.name) as archive:
                for member in archive.getmembers():
                    self.members[member.name] = archive.extractfile(member).read()

    def test_archive_is_byte_reproducible(self):
        self.assertEqual(build_payload(), self.payload)

    def test_index_validates_against_the_schema_it_ships(self):
        contract = json.loads(self.members["schema-index.schema.json"])
        validator = czschemas.validator_for(contract)
        validator.check_schema(contract)
        validator(contract).validate(json.loads(self.members["index.json"]))

    def test_locked_sha256_matches_the_source_tree(self):
        """A changed schema must force a new release, not a silent republish."""
        locked = [
            artifact
            for artifact in czschemas.read_lock(ROOT / "manifest.lock")
            if artifact.project == pack.PROJECT
        ]
        if not locked:
            self.skipTest("self-test artifact is not in manifest.lock yet")
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0].version, pack.RELEASE)
        self.assertEqual(locked[0].sha256, hashlib.sha256(self.payload).hexdigest())

    def test_artifact_publishes_to_its_canonical_url(self):
        artifact = czschemas.LockedArtifact(
            project=pack.PROJECT,
            repo="unprofessor/columnzero-schemas",
            tag=f"schemas-v{pack.RELEASE}",
            version=pack.RELEASE,
            asset="schemas.tar.gz",
            url=f"https://github.com/unprofessor/columnzero-schemas/releases/download/schemas-v{pack.RELEASE}/schemas.tar.gz",
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            result = czschemas.build_site(BASE_URL, [artifact], site, on_download=lambda a: self.payload)
            self.assertEqual(result, {"published": 1, "aliases": 2})
            published = site / f"{pack.PROJECT}/v{pack.COMPAT}/{pack.RELEASE}/schema-index.schema.json"
            self.assertEqual(
                json.loads(published.read_text())["$id"],
                f"{BASE_URL}/{pack.PROJECT}/v{pack.COMPAT}/{pack.RELEASE}/schema-index.schema.json",
            )
            # Rebuilding the same lockfile republishes nothing and passes the audit.
            self.assertEqual(
                czschemas.build_site(BASE_URL, [artifact], site, on_download=lambda a: self.payload),
                {"published": 0, "aliases": 2},
            )


if __name__ == "__main__":
    unittest.main()
