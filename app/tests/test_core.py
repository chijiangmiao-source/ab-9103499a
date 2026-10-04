"""Unit tests for the control-service state machine and shared helpers."""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import (  # noqa: E402
    ArtifactError,
    MAX_ARTIFACT_BYTES,
    decode_artifact,
    derive_operation_key,
    sha256_hex,
)
from controller_server import (  # noqa: E402
    Controller,
    RegistryError,
    STATE_COMPLETED,
    STATE_REJECTED,
)

SECRET = "unit-test-secret"
FOREIGN = "f0eign" + "0" * 58


class FakeRegistry:
    """In-process stand-in exercising the same persistence/idempotency rules."""

    def __init__(self, name: str, fault: str = "none"):
        self.name = name
        self.fault = fault
        self.prepared: dict[str, str] = {}
        self.activated: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.active_digest: str | None = None

    def _key_ok(self, release_id, op_key):
        return op_key == derive_operation_key(SECRET, release_id)

    def _receipt(self, release_id, digest, op):
        return {"registry": self.name, "release": release_id,
                "op": op, "digest": digest, "nonce": f"{self.name}-{op}-nonce",
                "issued_at": "2026-10-04T00:00:00Z"}

    def prepare(self, release_id, op_key, digest):
        if not self._key_ok(release_id, op_key):
            raise RegistryError(f"{self.name}: bad key", kind="conflict")
        if release_id in self.prepared and self.prepared[release_id] != digest:
            raise RegistryError(f"{self.name}: same key other digest",
                                kind="conflict")
        self.prepared.setdefault(release_id, digest)
        return self._receipt(release_id, digest, "prepare")

    def activate(self, release_id, op_key, digest):
        if not self._key_ok(release_id, op_key):
            raise RegistryError(f"{self.name}: bad key", kind="conflict")
        if self.prepared.get(release_id) != digest:
            raise RegistryError(f"{self.name}: not prepared", kind="conflict")
        if self.fault == "foreign":
            fake = self._receipt(release_id, digest, "activate")
            fake["digest"] = FOREIGN
            return fake
        already = self.activated.get(release_id)
        self.activated[release_id] = digest
        self.counts[release_id] = self.counts.get(release_id, 0) + (
            0 if already else 1
        )
        self.active_digest = digest
        if self.fault == "drop":
            raise RegistryError(f"{self.name}: connection severed",
                                kind="unavailable")
        return self._receipt(release_id, digest, "activate")

    def status(self, release_id, op_key):
        if not self._key_ok(release_id, op_key):
            raise RegistryError(f"{self.name}: bad key", kind="conflict")
        activated = release_id in self.activated
        digest = self.prepared.get(release_id)
        return {
            "registry": self.name, "release": release_id,
            "prepared": release_id in self.prepared,
            "activated": activated,
            "activation_count": 1 if activated else 0,
            "digest": digest,
            "prepare": self._receipt(release_id, digest, "prepare")
            if digest else None,
            "activate": self._receipt(release_id, digest, "activate")
            if activated else None,
        }


def b64(text: bytes) -> str:
    return base64.b64encode(text).decode("ascii")


class HelperTests(unittest.TestCase):
    def test_decode_roundtrip(self):
        raw = b"calibration-package"
        self.assertEqual(decode_artifact(b64(raw)), raw)

    def test_decode_rejects_garbage(self):
        for bad in ["", "   ", "not*base64!", "aGVsbG8=\n"]:
            with self.assertRaises(ArtifactError):
                decode_artifact(bad)

    def test_decode_rejects_empty_and_oversize(self):
        with self.assertRaises(ArtifactError):
            decode_artifact(b64(b""))
        with self.assertRaises(ArtifactError):
            decode_artifact(b64(b"x" * (MAX_ARTIFACT_BYTES + 1)))
        # Exactly at the limit is accepted.
        self.assertEqual(len(decode_artifact(b64(b"x" * MAX_ARTIFACT_BYTES))),
                         MAX_ARTIFACT_BYTES)

    def test_operation_key_derivation(self):
        k1 = derive_operation_key(SECRET, "rel-1")
        self.assertEqual(k1, derive_operation_key(SECRET, "rel-1"))
        self.assertNotEqual(k1, derive_operation_key(SECRET, "rel-2"))
        self.assertNotEqual(k1, derive_operation_key("other-secret", "rel-1"))
        self.assertEqual(len(k1), 64)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = b"calibration-payload-v1"
        self.digest = sha256_hex(self.art)

    def tearDown(self):
        self.tmp.cleanup()

    def _controller(self, fa="none", fb="none"):
        a, b = FakeRegistry("registry-a", fa), FakeRegistry("registry-b", fb)
        ctl = Controller(self.tmp.name, SECRET, [a, b])
        return ctl, a, b

    def test_happy_path_completes_when_both_activate_same_digest(self):
        ctl, a, b = self._controller()
        code, body = ctl.submit("rel-1", b64(self.art))
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["release"]["state"], STATE_COMPLETED)
        self.assertEqual(ctl.active_digest(), self.digest)
        self.assertEqual(a.active_digest, self.digest)
        self.assertEqual(b.active_digest, self.digest)
        # Evidence: first receipts from both registries.
        self.assertTrue(body["release"]["prepare_evidence"]["registry-a"])
        self.assertTrue(body["release"]["activate_evidence"]["registry-b"])

    def test_replay_same_id_same_artifact_no_second_activation(self):
        ctl, a, b = self._controller()
        ctl.submit("rel-1", b64(self.art))
        code, body = ctl.submit("rel-1", b64(self.art))
        self.assertEqual(code, 200)
        self.assertTrue(body["replay"])
        self.assertEqual(a.counts["rel-1"], 1)
        self.assertEqual(b.counts["rel-1"], 1)

    def test_same_id_different_artifact_rejected_and_preserved(self):
        ctl, a, b = self._controller()
        ctl.submit("rel-1", b64(self.art))
        other = b64(b"totally-different-payload")
        code, body = ctl.submit("rel-1", other)
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        # Prior successful release keeps its true state and pointer.
        rec = ctl.store.get("rel-1")
        self.assertEqual(rec["state"], STATE_COMPLETED)
        self.assertEqual(rec["sha256"], self.digest)
        self.assertEqual(ctl.active_digest(), self.digest)
        self.assertEqual(rec["attempts"][-1]["reason"], "different_artifact")

    def test_invalid_inputs_are_400(self):
        ctl, _, _ = self._controller()
        code, body = ctl.submit("rel-x", "@@not-base64@@")
        self.assertEqual(code, 400)
        code, body = ctl.submit("bad id!", b64(self.art))
        self.assertEqual(code, 400)
        code, body = ctl.submit("rel-y", b64(b"x" * (MAX_ARTIFACT_BYTES + 1)))
        self.assertEqual(code, 400)
        self.assertIsNone(ctl.store.get("rel-x"))

    def test_drop_then_restart_converges_from_registry_receipts(self):
        ctl, a, b = self._controller(fa="none", fb="drop")
        code, body = ctl.submit("rel-2", b64(self.art))
        self.assertEqual(code, 202)  # B committed but severed the response
        self.assertTrue(body["warnings"])
        self.assertEqual(b.activated["rel-2"], self.digest)  # durable commit
        self.assertIsNone(ctl.active_digest())  # completion gate not yet passed

        # Simulate a controller RESTART: fresh controller over the same
        # persisted data dir; the registries (with their own durable state)
        # now answer status normally.
        b.fault = "none"
        ctl2 = Controller(self.tmp.name, SECRET, [a, b])
        code, body = ctl2.status("rel-2")
        self.assertEqual(code, 200)
        self.assertEqual(body["release"]["state"], STATE_COMPLETED)
        self.assertEqual(ctl2.active_digest(), self.digest)
        # B must not have been activated a second time during convergence.
        self.assertEqual(b.counts["rel-2"], 1)

    def test_foreign_digest_locks_rejected_and_keeps_pointer(self):
        ctl, a, b = self._controller()
        ctl.submit("good", b64(self.art))
        a.fault = "foreign"
        ctl2 = Controller(self.tmp.name, SECRET, [a, b])
        code, body = ctl2.submit("evil", b64(b"another-payload"))
        self.assertEqual(code, 409)
        self.assertEqual(body["release"]["state"], STATE_REJECTED)
        # Active pointer is not rewritten.
        self.assertEqual(ctl2.active_digest(), self.digest)
        self.assertEqual(a.active_digest, self.digest)
        self.assertNotIn("evil", a.activated)
        # Terminal lock is sticky: further status checks do not re-drive it.
        code, body = ctl2.status("evil")
        self.assertEqual(body["release"]["state"], STATE_REJECTED)
        self.assertEqual(ctl2.active_digest(), self.digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
