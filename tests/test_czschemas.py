import hashlib
import importlib.util
import json
import sys
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src" / "czschemas.py"
spec = importlib.util.spec_from_file_location("czschemas", MODULE_PATH)
czschemas = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = czschemas
spec.loader.exec_module(czschemas)


BASE_URL = "https://schemas.columnzero.com"


def schema(version: str, name: str = "planr.schema.json") -> bytes:
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE_URL}/planr/rel/{version}/{name}",
            "type": "object",
        }
    ).encode()


def artifact(version: str, compat: str = "1", body: bytes | None = None) -> bytes:
    index = {
        "schema_index": 1,
        "project": "planr",
        "release": version,
        "schemas": [
            {
                "path": "planr.schema.json",
                "compat": compat,
                "dialect": "https://json-schema.org/draft/2020-12/schema",
            }
        ],
    }
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in [
            ("index.json", json.dumps(index).encode()),
            ("planr.schema.json", body or schema(version)),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, BytesIO(content))
    return output.getvalue()


def artifact_with_index(index: dict, *name_bodies: tuple[str, bytes]) -> bytes:
    """Build an artifact tar with an explicit index and named schema members."""
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in [("index.json", json.dumps(index).encode()), *name_bodies]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, BytesIO(content))
    return output.getvalue()


def locked_artifact(directory: Path, version: str, compat: str = "1", body: bytes | None = None):
    payload = artifact(version, compat, body)
    path = directory / f"{version}.tar.gz"
    path.write_bytes(payload)
    return czschemas.LockedArtifact(
        project="planr",
        repo="unprofessor/planr-rs",
        tag=f"v{version}",
        version=version,
        asset="schemas.tar.gz",
        url=path.as_uri(),
        sha256=hashlib.sha256(payload).hexdigest(),
        published_at="2026-08-01T00:00:00Z",
    )


def locked_github_artifact(directory: Path, version: str, payload_bytes: bytes) -> czschemas.LockedArtifact:
    """Locked artifact whose download URL looks like a GitHub release asset."""
    path = directory / f"{version}.tar.gz"
    path.write_bytes(payload_bytes)
    return czschemas.LockedArtifact(
        project="planr",
        repo="unprofessor/planr-rs",
        tag=f"v{version}",
        version=version,
        asset="schemas.tar.gz",
        url=f"https://github.com/unprofessor/planr-rs/releases/download/v{version}/schemas.tar.gz",
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        published_at="2026-08-01T00:00:00Z",
    )


def file_download(artifact: czschemas.LockedArtifact) -> bytes:
    """Return the payload written next to a locked file:// artifact (test fixture)."""
    return Path(artifact.url.removeprefix("file://")).read_bytes()


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_download_rejects_non_github_urls(self):
        with self.assertRaisesRegex(czschemas.ValidationError, "unsafe artifact URL"):
            czschemas.download(locked_artifact(self.root, "1.4.2"))

    def test_download_enforces_artifact_size_limit(self):
        payload = b"x" * (czschemas.MAX_ARTIFACT_BYTES + 1)
        artifact = locked_github_artifact(self.root, "1.4.2", payload)
        with self.assertRaisesRegex(czschemas.ValidationError, "size limit"):
            czschemas.fetch_document_set(BASE_URL, [artifact], on_download=lambda a: payload)

    def test_build_publishes_canonical_schema_and_aliases(self):
        site = self.root / "site"
        result = czschemas.build_site(
            BASE_URL,
            [locked_artifact(self.root, "1.4.2")],
            site,
            on_download=file_download,
        )

        canonical = site / "planr/rel/1.4.2/planr.schema.json"
        compat = site / "planr/compat/1/planr.schema.json"
        latest = site / "planr/latest/planr.schema.json"
        self.assertEqual(canonical.read_bytes(), compat.read_bytes())
        self.assertEqual(canonical.read_bytes(), latest.read_bytes())
        self.assertEqual(result, {"published": 1, "aliases": 2})
        self.assertEqual(json.loads((site / "planr/compat/1/index.json").read_text())["version"], "1.4.2")
        self.assertEqual((site / "CNAME").read_text(), "schemas.columnzero.com\n")
        self.assertEqual(
            json.loads((site / "index.json").read_text())["schemas"][0]["url"],
            f"{BASE_URL}/planr/rel/1.4.2/planr.schema.json",
        )

    def test_build_rejects_schema_with_wrong_canonical_id(self):
        bad = json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://wrong.invalid/x"}).encode()
        with self.assertRaisesRegex(czschemas.ValidationError, r"\$id"):
            czschemas.build_site(
                BASE_URL,
                [locked_artifact(self.root, "1.4.2", body=bad)],
                self.root / "site",
                on_download=file_download,
            )

    def test_build_rejects_schema_that_fails_declared_meta_schema(self):
        invalid = json.loads(schema("1.4.2"))
        invalid["type"] = 7
        with self.assertRaisesRegex(czschemas.ValidationError, "meta-schema"):
            czschemas.build_site(
                BASE_URL,
                [locked_artifact(self.root, "1.4.2", body=json.dumps(invalid).encode())],
                self.root / "site",
                on_download=file_download,
            )

    def test_build_refuses_to_change_existing_canonical_schema(self):
        site = self.root / "site"
        czschemas.build_site(BASE_URL, [locked_artifact(self.root, "1.4.2")], site, on_download=file_download)
        changed_value = json.loads(schema("1.4.2"))
        changed_value["title"] = "changed but still identifies the same release"
        changed = json.dumps(changed_value).encode()
        with self.assertRaises(czschemas.ImmutabilityError):
            czschemas.build_site(
                BASE_URL,
                [locked_artifact(self.root, "1.4.2", body=changed)],
                site,
                on_download=file_download,
            )

    def test_build_retains_prior_catalog_entries_when_new_release_arrives(self):
        site = self.root / "site"
        czschemas.build_site(BASE_URL, [locked_artifact(self.root, "1.4.2")], site, on_download=file_download)
        czschemas.build_site(BASE_URL, [locked_artifact(self.root, "1.5.0")], site, on_download=file_download)
        versions = [entry["version"] for entry in json.loads((site / "index.json").read_text())["schemas"]]
        self.assertEqual(versions, ["1.4.2", "1.5.0"])
        self.assertTrue((site / "planr/rel/1.4.2/planr.schema.json").exists())

    def test_build_drops_stale_aliases_when_lockfile_changes(self):
        old_index = {
            "schema_index": 1,
            "project": "planr",
            "release": "1.4.2",
            "schemas": [
                {
                    "path": "planr.schema.json",
                    "compat": "1",
                    "dialect": "https://json-schema.org/draft/2020-12/schema",
                },
                {
                    "path": "task.schema.json",
                    "compat": "1",
                    "dialect": "https://json-schema.org/draft/2020-12/schema",
                },
            ],
        }
        site = self.root / "site"
        legacy = locked_artifact(self.root, "1.4.2")
        czschemas.build_site(BASE_URL, [legacy], site, on_download=file_download)

        old_archive_path = self.root / "old.tar.gz"
        old_archive_data = artifact_with_index(
            old_index,
            ("planr.schema.json", schema("1.4.2", "planr.schema.json")),
            ("task.schema.json", schema("1.4.2", "task.schema.json")),
        )
        old_archive_path.write_bytes(old_archive_data)
        old_artifact = czschemas.LockedArtifact(
            project="planr",
            repo="unprofessor/planr-rs",
            tag="v1.4.2",
            version="1.4.2",
            asset="schemas.tar.gz",
            url=old_archive_path.as_uri(),
            sha256=hashlib.sha256(old_archive_data).hexdigest(),
            published_at="2026-08-01T00:00:00Z",
        )
        czschemas.build_site(BASE_URL, [old_artifact], site, on_download=file_download)
        self.assertTrue((site / "planr/compat/1/task.schema.json").exists())

        newer = locked_artifact(self.root, "1.5.0")
        czschemas.build_site(BASE_URL, [legacy, newer], site, on_download=file_download)
        self.assertFalse((site / "planr/compat/1/task.schema.json").exists())
        self.assertFalse((site / "planr/latest/task.schema.json").exists())
        self.assertTrue((site / "planr/compat/1/planr.schema.json").exists())

    def test_extract_rejects_path_traversal(self):
        payload = BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            info = tarfile.TarInfo("../index.json")
            content = b"{}"
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
        archive_path = self.root / "unsafe.tar.gz"
        archive_path.write_bytes(payload.getvalue())
        with self.assertRaisesRegex(czschemas.ValidationError, "unsafe archive path"):
            czschemas.read_artifact(archive_path)


if __name__ == "__main__":
    unittest.main()
