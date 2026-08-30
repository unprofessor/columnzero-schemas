"""The publisher's self-test: this repository publishes its own artifact contract.

`selftest/schema-index.schema.json` describes the root `index.json` that every upstream
artifact must carry, so the artifact built from it is the one artifact whose index can
be validated against its own payload.  These tests keep the source schema, the
reproducible archive, and the digest recorded in the manifest tree in agreement.
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
sys.path.insert(0, str(ROOT / "src"))

from czschemas import reconcile, registry                    # noqa: E402
from czschemas.config import Config                          # noqa: E402
from czschemas.model import ArtifactLock, ReleaseKey, Version  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pack = _load("selftest_pack", ROOT / "selftest" / "pack.py")
BASE_URL = "https://schemas.columnzero.com"
KEY = ReleaseKey(pack.PROJECT, Version.parse(pack.RELEASE))
LOCK_PATH = ROOT / reconcile.MANIFEST_ROOT / KEY.path / registry.LOCK_NAME


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
        try:
            from jsonschema.validators import validator_for
        except ImportError:
            self.skipTest("jsonschema is not installed (pip install '.[lint]')")
        contract = json.loads(self.members["schema-index.schema.json"])
        validator = validator_for(contract)
        validator.check_schema(contract)
        validator(contract).validate(json.loads(self.members["index.json"]))

    def test_the_declared_digest_matches_the_source_tree(self):
        """A changed schema must force a new release, not a silent republish."""
        if not LOCK_PATH.is_file():
            self.skipTest("the self-test artifact is not declared yet")
        lock = ArtifactLock.parse(LOCK_PATH.read_bytes(), str(LOCK_PATH))
        self.assertEqual(lock.sha256, hashlib.sha256(self.payload).hexdigest())

    def test_the_lock_lives_at_the_path_that_names_its_release(self):
        if not LOCK_PATH.is_file():
            self.skipTest("the self-test artifact is not declared yet")
        relative = LOCK_PATH.relative_to(ROOT / reconcile.MANIFEST_ROOT).parent.as_posix()
        key = ReleaseKey.from_path(relative)
        self.assertEqual(key.project, pack.PROJECT)
        self.assertEqual(str(key.version), pack.RELEASE)
        self.assertEqual(str(key.compat), pack.COMPAT)

    def test_the_artifact_reconciles_into_its_canonical_url(self):
        config = Config(BASE_URL, custom_domain=False, linters={})
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            result = reconcile.apply(
                ROOT, site, config, on_download=lambda lock: self.payload
            )
            self.assertEqual(result["admitted"], 1)
            self.assertEqual(result["published"], 1)
            published = site / KEY.path / "schema-index.schema.json"
            self.assertEqual(
                json.loads(published.read_text())["$id"], KEY.url(BASE_URL, published.name)
            )

            def refuse(lock):
                raise AssertionError("steady state fetched an artifact")

            self.assertEqual(
                reconcile.apply(ROOT, site, config, on_download=refuse)["admitted"], 0
            )


if __name__ == "__main__":
    unittest.main()
