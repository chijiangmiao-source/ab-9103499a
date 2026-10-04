"""Offline mirror-registry stub.

Each registry persists prepared/activated release state to disk so that a
dropped activation response (the work itself was durably committed) can be
recovered from the registry receipt after a controller restart.

Rules:
  * prepare/activate require the operation key derived from the release id;
  * same key + same digest replays the *first* receipt byte-for-byte;
  * same key + different digest is an explicit rejection;
  * activation is performed at most once per release (counter never grows);
  * fault injection: "drop" (commit then sever the connection), "foreign"
    (report a digest that does not belong to the release, without moving the
    active pointer).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from common import derive_operation_key

FOREIGN_DIGEST = "f0eign" + "0" * 58  # clearly does not belong to the release


class RegistryUnavailable(Exception):
    pass


class RegistryAuth(Exception):
    pass


class RegistryConflict(Exception):
    pass


class RegistryState:
    def __init__(self, name: str, data_dir: str, secret: str):
        self.name = name
        self.data_dir = data_dir
        self.secret = secret
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
            # Fault injection is test scaffolding, never durable intent:
            # always boot answering normally.
            self.data["fault"] = "none"
            self._flush()
        else:
            self.data = {"fault": "none", "active_digest": None, "releases": {}}
            self._flush()

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def set_fault(self, mode: str) -> None:
        with self.lock:
            self.data["fault"] = mode
            self._flush()

    def _check_key(self, release_id: str, op_key: str) -> bool:
        expected = derive_operation_key(self.secret, release_id)
        return bool(op_key) and _const_eq(expected, op_key)

    def _receipt(self, release_id: str, digest: str, op: str) -> dict:
        return {
            "registry": self.name,
            "release": release_id,
            "op": op,
            "digest": digest,
            "nonce": uuid.uuid4().hex,
            "issued_at": _now(),
        }

    def prepare(self, release_id: str, op_key: str, digest: str) -> dict:
        if not self._check_key(release_id, op_key):
            raise RegistryAuth("operation key rejected by registry")
        with self.lock:
            rel = self.data["releases"].get(release_id)
            if rel is not None and rel["digest"] != digest:
                raise RegistryConflict(
                    f"同键异摘要：仓 {self.name} 已绑定另一摘要 {rel['digest']}"
                )
            if rel is None:
                rel = {
                    "digest": digest,
                    "activation_count": 0,
                    "prepare": self._receipt(release_id, digest, "prepare"),
                    "activate": None,
                }
                self.data["releases"][release_id] = rel
                self._flush()
            # Replay the exact first prepare receipt.
            return dict(rel["prepare"])

    def activate(self, release_id: str, op_key: str, digest: str) -> dict:
        if not self._check_key(release_id, op_key):
            raise RegistryAuth("operation key rejected by registry")
        with self.lock:
            rel = self.data["releases"].get(release_id)
            if rel is None:
                raise RegistryConflict(f"仓 {self.name} 尚无该发布的准备记录")
            if rel["digest"] != digest:
                raise RegistryConflict(
                    f"同键异摘要：仓 {self.name} 已绑定另一摘要 {rel['digest']}"
                )
            # "foreign" fault: fabricate a receipt carrying a digest that does
            # not belong to this release. Nothing is committed, the activation
            # counter does not grow and the active pointer is never rewritten.
            if self.data["fault"] == "foreign":
                fake = self._receipt(release_id, digest, "activate")
                fake["digest"] = FOREIGN_DIGEST
                fake["fabricated"] = True
                return fake
            first_time = rel["activate"] is None
            if first_time:
                rel["activate"] = self._receipt(release_id, digest, "activate")
                rel["activation_count"] = 1
                self.data["active_digest"] = digest  # durable pointer move
                self._flush()
            receipt = dict(rel["activate"])
        return receipt

    def status(self, release_id: str, op_key: str) -> dict:
        if not self._check_key(release_id, op_key):
            raise RegistryAuth("operation key rejected by registry")
        with self.lock:
            rel = self.data["releases"].get(release_id)
            return {
                "registry": self.name,
                "release": release_id,
                "prepared": rel is not None,
                "activated": bool(rel and rel["activate"]),
                "activation_count": rel["activation_count"] if rel else 0,
                "digest": rel["digest"] if rel else None,
                "prepare": dict(rel["prepare"]) if rel else None,
                "activate": dict(rel["activate"]) if rel and rel["activate"] else None,
            }

    def active(self) -> dict:
        with self.lock:
            return {"registry": self.name, "active_digest": self.data["active_digest"]}


def _const_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_handler(state: RegistryState):
    class RegistryHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # silence default logging
            pass

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _drop(self) -> bool:
            """Commit-then-disconnect fault: close the socket without a reply."""
            if state.data["fault"] != "drop":
                return False
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return True

        def do_GET(self):
            path = urlparse(self.path).path
            qs = urlparse(self.path).query
            if path == "/health":
                return self._send_json(200, {"status": "ok", "registry": state.name})
            if path == "/v1/active":
                return self._send_json(200, state.active())
            if path.startswith("/v1/releases/"):
                tail = path[len("/v1/releases/"):]
                if tail.endswith("/status"):
                    release_id = tail[: -len("/status")]
                else:
                    release_id = tail
                op_key = _query(qs, "op_key")
                try:
                    body = state.status(release_id, op_key)
                except RegistryAuth as exc:
                    return self._send_json(403, {"error": str(exc)})
                except RegistryConflict as exc:
                    return self._send_json(409, {"error": str(exc)})
                # Note: the "drop" fault only severs activate responses, so a
                # restarted controller can still converge from this status.
                return self._send_json(200, body)
            return self._send_json(404, {"error": "unknown path"})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                payload = self._read_json()
            except (ValueError, UnicodeDecodeError):
                return self._send_json(400, {"error": "invalid JSON body"})
            if path == "/fault":
                mode = payload.get("mode", "none")
                if mode not in ("none", "drop", "foreign"):
                    return self._send_json(400, {"error": "unknown fault mode"})
                state.set_fault(mode)
                return self._send_json(200, {"fault": mode})
            prefix = "/v1/releases/"
            if path.startswith(prefix) and path.endswith(("/prepare", "/activate")):
                release_id = path[len(prefix):].rsplit("/", 1)[0]
                op_key = payload.get("op_key", "")
                digest = payload.get("sha256", "")
                op = path.rsplit("/", 1)[-1]
                try:
                    if op == "prepare":
                        receipt = state.prepare(release_id, op_key, digest)
                    else:
                        receipt = state.activate(release_id, op_key, digest)
                except RegistryAuth as exc:
                    return self._send_json(403, {"error": str(exc)})
                except RegistryConflict as exc:
                    return self._send_json(409, {"error": str(exc)})
                # The "drop" fault severs only the activation response, after
                # the registry has durably committed it.
                if op == "activate" and self._drop():
                    return
                return self._send_json(200, {"receipt": receipt})
            return self._send_json(404, {"error": "unknown path"})

    return RegistryHandler


def _query(qs: str, key: str) -> str:
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k == key:
                return v
    return ""


def serve(name: str, port: int, data_dir: str, secret: str) -> None:
    state = RegistryState(name, data_dir, secret)
    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(state))
    print(f"registry {name} listening on :{port}", flush=True)
    server.serve_forever()


def main() -> None:
    name = os.environ.get("REGISTRY_NAME", "registry")
    port = int(os.environ.get("REGISTRY_PORT", "8080"))
    data_dir = os.environ.get("REGISTRY_DATA_DIR", f"/data/{name}")
    secret = os.environ.get("OP_KEY_SECRET", "probe-calibration-master-secret")
    serve(name, port, data_dir, secret)


if __name__ == "__main__":
    main()
