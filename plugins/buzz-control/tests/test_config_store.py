from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONFIG_STORE_PATH = PLUGIN_ROOT / "dashboard" / "config_store.py"


def load_config_store():
    spec = importlib.util.spec_from_file_location(
        "buzz_control_config_store", CONFIG_STORE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load dashboard/config_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config_store()

    def make_store(
        self,
        root: Path,
        desired: bytes,
        *,
        applied: bytes | None = None,
    ):
        config_dir = root / "config"
        state_dir = root / "state"
        config_dir.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        desired_path = config_dir / "prod.env"
        desired_path.write_bytes(desired)
        desired_path.chmod(0o600)
        paths = self.config.ConfigPaths.for_desired(desired_path, state_dir)
        if applied is not None:
            paths.applied.write_bytes(applied)
            paths.applied.chmod(0o600)
        return self.config.ConfigStore(paths), paths

    def test_document_round_trips_supported_bytes_exactly(self):
        for raw in (
            b"# heading\n\nBUZZ_DOMAIN=example.test\nUNKNOWN_KEY=caf\xc3\xa9\n",
            b"# heading\r\nBUZZ_DOMAIN=example.test\r\nUNKNOWN_KEY=value",
        ):
            with self.subTest(raw=raw):
                document = self.config.DotenvDocument.parse(raw)
                self.assertEqual(document.render(), raw)

    def test_document_changes_only_the_explicit_assignment(self):
        raw = b"# keep\r\nBUZZ_DOMAIN=old.test\r\nUNKNOWN_KEY=opaque\r\n"
        document = self.config.DotenvDocument.parse(raw)

        updated, changed = document.apply({"BUZZ_DOMAIN": "new.test"})

        self.assertEqual(changed, ("BUZZ_DOMAIN",))
        self.assertEqual(
            updated.render(),
            b"# keep\r\nBUZZ_DOMAIN='new.test'\r\nUNKNOWN_KEY=opaque\r\n",
        )

    def test_document_rejects_ambiguous_or_unsafe_input_without_echoing_it(self):
        cases = (
            b"A=1\nA=secret-canary\n",
            b"A=continued\\\nnext\n",
            b"A=\x00secret-canary\n",
            b"A=\xffsecret-canary\n",
            b"export A=secret-canary\n",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    self.config.DotenvDocument.parse(raw)
                self.assertNotIn("secret-canary", str(raised.exception))

        with self.assertRaises(self.config.ConfigStoreError) as raised:
            self.config.DotenvDocument.parse(
                b"A=" + b"x" * self.config.MAX_CONFIG_BYTES
            )
        self.assertEqual(raised.exception.code, "document_too_large")

    def test_catalog_is_limited_to_stable_operator_settings(self):
        catalog = self.config.FIELD_CATALOG
        expected = {
            "BUZZ_ALLOW_NIP_OA_AUTH",
            "BUZZ_CORS_ORIGINS",
            "BUZZ_DOMAIN",
            "BUZZ_MEDIA_BASE_URL",
            "BUZZ_MEDIA_SERVER_DOMAIN",
            "BUZZ_REQUIRE_AUTH_TOKEN",
            "BUZZ_REQUIRE_RELAY_MEMBERSHIP",
            "RELAY_OWNER_PUBKEY",
            "RELAY_URL",
        }
        self.assertEqual(set(catalog), expected)
        self.assertEqual(
            catalog["BUZZ_REQUIRE_RELAY_MEMBERSHIP"].impact,
            self.config.ImpactClass.RELAY_HIGH_IMPACT,
        )
        self.assertEqual(
            catalog["BUZZ_REQUIRE_RELAY_MEMBERSHIP"].group,
            "access_policy",
        )
        self.assertEqual(catalog["BUZZ_DOMAIN"].group, "public_address")
        self.assertEqual(catalog["RELAY_OWNER_PUBKEY"].group, "owner_identity")
        self.assertFalse(catalog["RELAY_OWNER_PUBKEY"].editable)

    def test_projection_excludes_secrets_and_unmanaged_assignments(self):
        canary = "secret-canary-with-distinct-length"
        document = self.config.DotenvDocument.parse(
            (
                "BUZZ_DOMAIN=example.test\n"
                f"POSTGRES_PASSWORD={canary}\n"
                "UNKNOWN_LOCAL=unknown-canary\n"
            ).encode()
        )

        serialized = json.dumps(document.project(), default=lambda value: value.value)

        self.assertIn("example.test", serialized)
        self.assertNotIn(canary, serialized)
        self.assertNotIn("unknown-canary", serialized)
        by_name = {field["name"]: field for field in document.project()}
        self.assertEqual(by_name["BUZZ_DOMAIN"]["kind"], "host")
        self.assertEqual(by_name["BUZZ_DOMAIN"]["group"], "public_address")
        self.assertNotIn("known", by_name["BUZZ_DOMAIN"])
        self.assertNotIn("POSTGRES_PASSWORD", by_name)
        self.assertNotIn("UNKNOWN_LOCAL", by_name)

    def test_unmanaged_secret_replacements_are_rejected_without_mutation(self):
        canary = b"secret-canary"
        raw = b"POSTGRES_PASSWORD=" + canary + b"\nBUZZ_DOMAIN=old.test\n"
        document = self.config.DotenvDocument.parse(raw)

        omitted, omitted_changed = document.apply({"BUZZ_DOMAIN": "new.test"})

        self.assertIn(canary, omitted.render())
        self.assertEqual(omitted_changed, ("BUZZ_DOMAIN",))
        with self.assertRaises(self.config.ConfigStoreError) as raised:
            document.apply({"POSTGRES_PASSWORD": "new-secret"})
        self.assertEqual(raised.exception.code, "field_not_editable")
        self.assertEqual(document.render(), raw)

    def test_plain_optional_values_can_be_cleared(self):
        document = self.config.DotenvDocument.parse(
            b"BUZZ_MEDIA_BASE_URL=https://media.example.test\n"
        )

        updated, changed = document.apply({"BUZZ_MEDIA_BASE_URL": ""})

        self.assertEqual(changed, ("BUZZ_MEDIA_BASE_URL",))
        self.assertEqual(updated.render(), b"BUZZ_MEDIA_BASE_URL=''\n")

    def test_replacements_use_literal_compose_encoding(self):
        document = self.config.DotenvDocument.parse(b"BUZZ_CORS_ORIGINS=https://old.test\n")
        logical = "buzz=$value # don't interpolate \\ path"

        updated, changed = document.apply({"BUZZ_CORS_ORIGINS": logical})

        self.assertEqual(changed, ("BUZZ_CORS_ORIGINS",))
        self.assertIn(b"BUZZ_CORS_ORIGINS='buzz=$value # don\\'t interpolate \\ path'", updated.render())
        projected = {field["name"]: field for field in updated.project()}
        self.assertEqual(projected["BUZZ_CORS_ORIGINS"]["value"], logical)

        trailing, _changed = document.apply({"BUZZ_CORS_ORIGINS": "trailing\\"})
        self.assertEqual(trailing.value("BUZZ_CORS_ORIGINS"), "trailing\\")
        self.assertIn(b'BUZZ_CORS_ORIGINS="trailing\\\\"', trailing.render())

    def test_compose_inline_comments_decode_without_changing_raw_bytes(self):
        raw = (
            b"BUZZ_HTTP_PORT=4400 # local bind\n"
            b"RELAY_URL=wss://relay.example.test/#events # public endpoint\n"
            b"BUZZ_CORS_ORIGINS='alpha # beta' # operator's note\n"
        )

        document = self.config.DotenvDocument.parse(raw)

        self.assertEqual(document.value("BUZZ_HTTP_PORT"), "4400")
        self.assertEqual(
            document.value("RELAY_URL"),
            "wss://relay.example.test/#events",
        )
        self.assertEqual(document.value("BUZZ_CORS_ORIGINS"), "alpha # beta")
        self.assertEqual(document.render(), raw)

    def test_ambiguous_trailing_quoted_content_is_rejected(self):
        with self.assertRaises(self.config.ConfigStoreError) as raised:
            self.config.DotenvDocument.parse(
                b'BUZZ_CORS_ORIGINS="https://one.test"#ambiguous\n'
            )

        self.assertEqual(raised.exception.code, "invalid_document")

    def test_secret_bearing_urls_are_write_only_and_cannot_be_saved(self):
        for value in (
            "wss://operator:secret-canary@relay.example.test",
            "wss://relay.example.test/events?token=secret-canary",
            "wss://relay.example.test/events#secret-canary",
        ):
            with self.subTest(value=value):
                document = self.config.DotenvDocument.parse(
                    f"RELAY_URL={value}\n".encode()
                )

                projected = {field["name"]: field for field in document.project()}

                self.assertEqual(
                    projected["RELAY_URL"]["disclosure"], "write_only"
                )
                self.assertTrue(projected["RELAY_URL"]["configured"])
                self.assertFalse(projected["RELAY_URL"]["editable"])
                self.assertNotIn("value", projected["RELAY_URL"])
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    document.apply({"RELAY_URL": "wss://relay.example.test"})
                self.assertEqual(raised.exception.code, "field_not_editable")

    def test_runtime_value_uses_applied_snapshot_while_desired_is_pending(self):
        with tempfile.TemporaryDirectory() as td:
            desired = b"BUZZ_HTTP_PORT=4400\n"
            applied = b"BUZZ_HTTP_PORT=3300\n"
            store, _paths = self.make_store(
                Path(td), desired, applied=applied
            )

            self.assertEqual(store.runtime_value("BUZZ_HTTP_PORT"), "3300")
            self.assertEqual(
                store.runtime_value("BUZZ_HTTP_PORT", desired=True), "4400"
            )

    def test_runtime_value_does_not_parse_invalid_pending_desired(self):
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td),
                b"export BUZZ_HTTP_PORT=secret-canary\n",
                applied=b"BUZZ_HTTP_PORT=3300\n",
            )

            self.assertEqual(store.runtime_value("BUZZ_HTTP_PORT"), "3300")
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.runtime_value("BUZZ_HTTP_PORT", desired=True)
            self.assertEqual(raised.exception.code, "invalid_document")

    def test_known_values_are_validated_without_echoing_rejected_values(self):
        document = self.config.DotenvDocument.parse(
            b"BUZZ_DOMAIN=example.test\nBUZZ_REQUIRE_AUTH_TOKEN=true\n"
        )
        for key, value in (
            ("BUZZ_DOMAIN", "invalid host secret-canary"),
            ("BUZZ_REQUIRE_AUTH_TOKEN", "secret-canary"),
            ("RELAY_URL", "file://secret-canary"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    document.apply({key: value})
                self.assertEqual(raised.exception.code, "invalid_value")
                self.assertNotIn(value, str(raised.exception))

    def test_read_only_and_unmanaged_keys_cannot_be_written(self):
        document = self.config.DotenvDocument.parse(
            b"RELAY_OWNER_PUBKEY=owner\nBUZZ_IMAGE=image:old\n"
        )
        for patch in (
            {"RELAY_OWNER_PUBKEY": "different-owner"},
            {"BUZZ_IMAGE": "image:new"},
            {"ARBITRARY_NEW_KEY": "value"},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    document.apply(patch)
                self.assertEqual(raised.exception.code, "field_not_editable")

    def test_read_rejects_unsafe_parent_and_file_types(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(root, b"BUZZ_DOMAIN=example.test\n")
            paths.desired.chmod(0o644)
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

        metadata = MagicMock()
        metadata.st_mode = stat.S_IFREG | 0o600
        metadata.st_uid = os.getuid() + 1
        metadata.st_nlink = 1
        with self.assertRaises(self.config.ConfigStoreError) as raised:
            self.config._validate_file_metadata(metadata, os.getuid())
        self.assertEqual(raised.exception.code, "unsafe_storage")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(root, b"BUZZ_DOMAIN=example.test\n")
            paths.desired.unlink()
            paths.desired.symlink_to(root / "missing")
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(root, b"BUZZ_DOMAIN=example.test\n")
            os.link(paths.desired, root / "second-link")
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(root, b"BUZZ_DOMAIN=example.test\n")
            paths.desired.unlink()
            os.mkfifo(paths.desired, 0o600)
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(root, b"BUZZ_DOMAIN=example.test\n")
            paths.desired.parent.chmod(0o777)
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

    def test_describe_creates_opaque_revision_and_does_not_adopt_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(
                Path(td), b"BUZZ_DOMAIN=example.test\n"
            )

            view = store.describe()

            self.assertEqual(view.baseline_state, "baseline_missing")
            self.assertFalse(paths.applied.exists())
            self.assertRegex(view.revision, r"^[A-Za-z0-9_-]{20,}$")
            record = json.loads(paths.revision.read_text())
            self.assertNotEqual(record["revision"], record["fingerprint"])
            self.assertNotIn(record["fingerprint"], repr(view))
            self.assertEqual(stat.S_IMODE(paths.revision.stat().st_mode), 0o600)

    def test_external_edit_rotates_revision_and_invalidates_stale_save(self):
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(
                Path(td),
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            first = store.describe()
            paths.desired.write_bytes(b"BUZZ_DOMAIN=external.test\n")
            paths.desired.chmod(0o600)
            second = store.describe()

            self.assertNotEqual(first.revision, second.revision)
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.save(first.revision, {"BUZZ_DOMAIN": "third.test"})
            self.assertEqual(raised.exception.code, "stale_revision")

    def test_save_is_atomic_preserves_backup_and_writes_non_secret_journal(self):
        canary = "secret-canary"
        initial = (
            f"POSTGRES_PASSWORD={canary}\nBUZZ_DOMAIN=first.test\n"
        ).encode()
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(Path(td), initial, applied=initial)
            before = store.describe()

            result = store.save(before.revision, {"BUZZ_DOMAIN": "second.test"})

            self.assertTrue(result.wrote)
            self.assertEqual(paths.recovery.read_bytes(), initial)
            self.assertEqual(stat.S_IMODE(paths.recovery.stat().st_mode), 0o600)
            self.assertIn(b"BUZZ_DOMAIN='second.test'", paths.desired.read_bytes())
            journal = paths.journal.read_text()
            self.assertIn("BUZZ_DOMAIN", journal)
            self.assertNotIn(canary, journal)
            self.assertNotEqual(result.revision, before.revision)

    def test_save_rejects_external_replacement_at_write_boundary(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        external = b"BUZZ_DOMAIN=external.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(Path(td), initial, applied=initial)
            revision = store.describe().revision
            real_atomic_write = self.config._atomic_write
            replaced = False

            def racing_write(path, data, uid, **kwargs):
                nonlocal replaced
                if path == paths.desired and kwargs.get("expected") and not replaced:
                    replaced = True
                    replacement = paths.desired.with_name("external.env")
                    replacement.write_bytes(external)
                    replacement.chmod(0o600)
                    os.replace(replacement, paths.desired)
                return real_atomic_write(path, data, uid, **kwargs)

            with patch.object(
                self.config, "_atomic_write", side_effect=racing_write
            ):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    store.save(revision, {"BUZZ_DOMAIN": "second.test"})

            self.assertEqual(raised.exception.code, "stale_revision")
            self.assertEqual(paths.desired.read_bytes(), external)
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(paths.journal.exists())

    def test_oversized_save_rejects_before_any_protected_mutation(self):
        prefix = b"BUZZ_CORS_ORIGINS=x\nOPAQUE="
        suffix = b"\n"
        filler = b"y" * (
            self.config.MAX_CONFIG_BYTES - len(prefix) - len(suffix) - 128
        )
        initial = prefix + filler + suffix
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(Path(td), initial, applied=initial)
            revision = store.describe().revision
            revision_record = paths.revision.read_bytes()

            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.save(
                    revision,
                    {"BUZZ_CORS_ORIGINS": "z" * self.config.MAX_FIELD_BYTES},
                )

            self.assertEqual(raised.exception.code, "document_too_large")
            self.assertEqual(paths.desired.read_bytes(), initial)
            self.assertEqual(paths.revision.read_bytes(), revision_record)
            self.assertFalse(paths.recovery.exists())
            self.assertFalse(paths.journal.exists())

    def test_atomic_replace_failure_preserves_original_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o700)
            target = root / "protected.env"
            target.write_bytes(b"original-secret\n")
            target.chmod(0o600)

            with patch.object(
                self.config.os, "replace", side_effect=OSError("injected")
            ):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    self.config._atomic_write(
                        target, b"replacement-secret\n", os.getuid()
                    )

            self.assertEqual(raised.exception.code, "unsafe_storage")
            self.assertEqual(target.read_bytes(), b"original-secret\n")
            self.assertEqual(list(root.glob(".buzz-control.tmp-*")), [])

    def test_directory_fsync_failure_leaves_a_complete_protected_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o700)
            target = root / "protected.env"
            target.write_bytes(b"original-secret\n")
            target.chmod(0o600)
            real_fsync = self.config.os.fsync

            def fail_directory_fsync(descriptor: int):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("injected")
                return real_fsync(descriptor)

            with patch.object(
                self.config.os, "fsync", side_effect=fail_directory_fsync
            ):
                with self.assertRaises(self.config.ConfigStoreError):
                    self.config._atomic_write(
                        target, b"replacement-secret\n", os.getuid()
                    )

            self.assertEqual(target.read_bytes(), b"replacement-secret\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list(root.glob(".buzz-control.tmp-*")), [])

    def test_no_op_save_does_not_rewrite_or_rotate_revision(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(Path(td), initial, applied=initial)
            before = store.describe()
            inode_before = paths.desired.stat().st_ino

            result = store.save(before.revision, {"BUZZ_DOMAIN": "first.test"})

            self.assertFalse(result.wrote)
            self.assertEqual(result.revision, before.revision)
            self.assertEqual(paths.desired.stat().st_ino, inode_before)
            self.assertFalse(paths.recovery.exists())

    def test_save_requires_an_applied_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td), b"BUZZ_DOMAIN=first.test\n"
            )
            view = store.describe()
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.save(view.revision, {"BUZZ_DOMAIN": "second.test"})
            self.assertEqual(raised.exception.code, "baseline_missing")

    def test_lock_contention_is_distinct_from_stale_revision(self):
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td),
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            view = store.describe()
            with store.lock():
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    store.save(view.revision, {"BUZZ_DOMAIN": "second.test"})
            self.assertEqual(raised.exception.code, "busy")

    def test_existing_unsafe_lock_file_is_rejected_not_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(
                Path(td),
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            paths.lock.write_text("do-not-trust")
            paths.lock.chmod(0o644)

            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()

            self.assertEqual(raised.exception.code, "unsafe_storage")
            self.assertEqual(stat.S_IMODE(paths.lock.stat().st_mode), 0o644)

    def test_snapshot_symlink_is_rejected_and_never_followed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(
                root,
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            paths.applied.unlink()
            paths.applied.symlink_to(paths.desired)
            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()
            self.assertEqual(raised.exception.code, "unsafe_storage")

    def test_stale_protected_temp_files_are_scavenged_under_the_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(
                root,
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            remnant = paths.desired.parent / ".buzz-control.tmp-abandoned"
            remnant.write_bytes(b"secret-canary")
            remnant.chmod(0o600)
            old = time.time() - 3600
            os.utime(remnant, (old, old))

            store.describe()

            self.assertFalse(remnant.exists())

    def test_unsafe_temp_remnant_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, paths = self.make_store(
                root,
                b"BUZZ_DOMAIN=first.test\n",
                applied=b"BUZZ_DOMAIN=first.test\n",
            )
            outside = root / "outside"
            outside.write_bytes(b"secret-canary")
            remnant = paths.desired.parent / ".buzz-control.tmp-hostile"
            remnant.symlink_to(outside)

            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.describe()

            self.assertEqual(raised.exception.code, "unsafe_storage")
            self.assertEqual(outside.read_bytes(), b"secret-canary")

    def test_adopt_promotes_exact_revision_and_restore_keeps_desired_recoverable(self):
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(
                Path(td), b"BUZZ_DOMAIN=first.test\n"
            )
            initial = store.describe()
            adopted = store.promote(initial.revision)
            self.assertEqual(paths.applied.read_bytes(), paths.desired.read_bytes())
            self.assertFalse(adopted.pending)

            saved = store.save(adopted.revision, {"BUZZ_DOMAIN": "second.test"})
            self.assertTrue(saved.pending)
            restored = store.restore(saved.revision)

            self.assertFalse(restored.pending)
            self.assertIn(b"BUZZ_DOMAIN=first.test", paths.desired.read_bytes())
            self.assertIn(b"BUZZ_DOMAIN='second.test'", paths.recovery.read_bytes())

    def test_failed_first_promotion_does_not_establish_an_applied_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(
                Path(td), b"BUZZ_DOMAIN=first.test\n"
            )
            initial = store.describe()

            with patch.object(
                store,
                "_write_journal_locked",
                side_effect=self.config.ConfigStoreError("unsafe_storage"),
            ):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    store.promote(initial.revision)

            self.assertEqual(raised.exception.code, "unsafe_storage")
            self.assertFalse(paths.applied.exists())

    def test_restore_rejects_external_replacement_at_write_boundary(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        external = b"BUZZ_DOMAIN=external.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, paths = self.make_store(Path(td), initial, applied=initial)
            saved = store.save(
                store.describe().revision, {"BUZZ_DOMAIN": "second.test"}
            )
            real_atomic_write = self.config._atomic_write
            replaced = False

            def racing_write(path, data, uid, **kwargs):
                nonlocal replaced
                if path == paths.desired and kwargs.get("expected") and not replaced:
                    replaced = True
                    replacement = paths.desired.with_name("external.env")
                    replacement.write_bytes(external)
                    replacement.chmod(0o600)
                    os.replace(replacement, paths.desired)
                return real_atomic_write(path, data, uid, **kwargs)

            with patch.object(
                self.config, "_atomic_write", side_effect=racing_write
            ):
                with self.assertRaises(self.config.ConfigStoreError) as raised:
                    store.restore(saved.revision)

            self.assertEqual(raised.exception.code, "stale_revision")
            self.assertEqual(paths.desired.read_bytes(), external)

    def test_journal_rejects_values_and_secret_derived_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td),
                b"POSTGRES_PASSWORD=secret-canary\n",
                applied=b"POSTGRES_PASSWORD=secret-canary\n",
            )
            for entry in (
                {"phase": "saved", "changed_keys": ["POSTGRES_PASSWORD"], "value": "x"},
                {"phase": "saved", "changed_keys": ["POSTGRES_PASSWORD"], "length": 13},
            ):
                with self.subTest(entry=entry):
                    with self.assertRaises(self.config.ConfigStoreError) as raised:
                        store.write_journal(entry)
                    self.assertEqual(raised.exception.code, "invalid_journal")

    def test_interrupted_mutation_phase_fails_closed(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td), initial, applied=initial
            )
            view = store.describe()
            store.write_journal(
                {
                    "phase": "applying",
                    "action": "apply",
                    "revision": view.revision,
                    "started_at": "2026-08-04T01:00:00Z",
                }
            )

            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.save(view.revision, {"BUZZ_DOMAIN": "second.test"})

            self.assertEqual(raised.exception.code, "recovery_required")

    def test_recovery_adoption_intent_requires_a_blocking_recovery_phase(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_store(
                Path(td), initial, applied=initial
            )
            revision = store.describe().revision

            with self.assertRaises(self.config.ConfigStoreError) as raised:
                store.record_operation_intent(
                    digest="a" * 64,
                    action="recover_adopt",
                    revision=revision,
                    expires_at=time.time() + 60,
                )

            self.assertEqual(raised.exception.code, "recovery_required")


if __name__ == "__main__":
    unittest.main()
