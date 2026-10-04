"""Control service for high-altitude probe calibration package swaps.

Guarantees implemented here:
  * SHA-256 and the immutable release intent are persisted *before* any
    registry operation;
  * registry operations use an operation key derived from the release id;
  * same key + same digest replays the first receipt (activation happens at
    most once); same key + different digest is an explicit rejection;
  * a release completes only when BOTH registries have activated the same
    SHA-256; if one registry commits activation but drops the response, a
    restarted controller converges to COMPLETED from registry receipts;
  * a receipt whose digest does not belong to the release locks the release as
    REJECTED and the active pointer is never rewritten;
  * re-submitting the same id + artifact never triggers a second activation;
    reusing an id with a different artifact, illegal Base64 or oversized
    payloads return specific feedback without touching a prior good release.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from common import (
    ArtifactError,
    decode_artifact,
    derive_operation_key,
    sha256_hex,
    validate_release_id,
)

STATE_RECEIVED = "RECEIVED"
STATE_PREPARED = "PREPARED"
STATE_ACTIVATING = "ACTIVATING"
STATE_COMPLETED = "COMPLETED"
STATE_REJECTED = "REJECTED"

TERMINAL_STATES = {STATE_COMPLETED, STATE_REJECTED}


class RegistryError(Exception):
    def __init__(self, message: str, kind: str = "unavailable", status: int = 502):
        super().__init__(message)
        self.kind = kind  # "unavailable" | "conflict" | "foreign"
        self.status = status


class RegistryClient:
    def __init__(self, name: str, base_url: str, timeout: float = 4.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = detail.get("error", f"registry HTTP {exc.code}")
            except (ValueError, UnicodeDecodeError):
                message = f"registry HTTP {exc.code}"
            kind = "conflict" if exc.code in (403, 409) else "unavailable"
            raise RegistryError(f"{self.name}: {message}", kind=kind, status=502)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise RegistryError(
                f"{self.name}: 连接中断或无响应 ({exc.__class__.__name__})",
                kind="unavailable",
            )

    def prepare(self, release_id: str, op_key: str, digest: str) -> dict:
        body = self._request(
            "POST", f"/v1/releases/{release_id}/prepare",
            {"op_key": op_key, "sha256": digest},
        )
        return body["receipt"]

    def activate(self, release_id: str, op_key: str, digest: str) -> dict:
        body = self._request(
            "POST", f"/v1/releases/{release_id}/activate",
            {"op_key": op_key, "sha256": digest},
        )
        return body["receipt"]

    def status(self, release_id: str, op_key: str) -> dict:
        return self._request(
            "GET", f"/v1/releases/{release_id}/status?op_key={op_key}", None
        )


class Store:
    """JSON-file persistence with an append-only immutable intent journal."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.releases_path = os.path.join(data_dir, "releases.json")
        self.pointer_path = os.path.join(data_dir, "active-pointer.json")
        self.journal_path = os.path.join(data_dir, "intent-journal.log")
        self.lock = threading.RLock()
        if os.path.exists(self.releases_path):
            with open(self.releases_path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        else:
            self.data = {"releases": {}}
            self._flush_locked()
        if not os.path.exists(self.pointer_path):
            self._write_pointer_locked(None)

    def _flush_locked(self) -> None:
        tmp = self.releases_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.releases_path)

    def _write_pointer_locked(self, digest: str | None) -> None:
        tmp = self.pointer_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"active_digest": digest}, fh, ensure_ascii=False)
        os.replace(tmp, self.pointer_path)

    def get(self, release_id: str) -> dict | None:
        with self.lock:
            return self.data["releases"].get(release_id)

    def list_ids(self) -> list[str]:
        with self.lock:
            return list(self.data["releases"].keys())

    def persist_intent(self, release_id: str, digest: str) -> dict:
        """Step 1: persist SHA-256 and the immutable release intent."""
        with self.lock:
            record = {
                "release": release_id,
                "sha256": digest,
                "state": STATE_RECEIVED,
                "created_at": _now(),
                "updated_at": _now(),
                "registries": {},
                "attempts": [],
                "error": None,
            }
            self.data["releases"][release_id] = record
            self._flush_locked()
            with open(self.journal_path, "a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(
                        {
                            "ts": _now(),
                            "event": "intent_persisted",
                            "release": release_id,
                            "sha256": digest,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            return record

    def update(self, release_id: str, **changes) -> None:
        with self.lock:
            record = self.data["releases"][release_id]
            # The intent (id -> digest) is immutable and never rewritten.
            changes.pop("sha256", None)
            record.update(changes)
            record["updated_at"] = _now()
            self._flush_locked()

    def update_registry(self, release_id: str, registry: str, **changes) -> None:
        with self.lock:
            record = self.data["releases"][release_id]
            entry = record["registries"].setdefault(
                registry,
                {
                    "prepare_receipt": None,
                    "activate_receipt": None,
                    "activation_count": 0,
                    "activation_attempted": False,
                },
            )
            entry.update(changes)
            record["updated_at"] = _now()
            self._flush_locked()

    def record_attempt(self, release_id: str, digest: str, reason: str) -> None:
        with self.lock:
            record = self.data["releases"][release_id]
            record["attempts"].append(
                {"sha256": digest, "reason": reason, "at": _now()}
            )
            self._flush_locked()

    def lock_rejected(self, release_id: str, reason: str) -> None:
        """Terminal REJECTED lock; the active pointer is never touched here."""
        with self.lock:
            record = self.data["releases"][release_id]
            if record["state"] == STATE_COMPLETED:
                return  # a completed release cannot be retroactively rejected
            record["state"] = STATE_REJECTED
            record["error"] = reason
            record["updated_at"] = _now()
            self._flush_locked()

    def complete(self, release_id: str, digest: str) -> None:
        with self.lock:
            record = self.data["releases"][release_id]
            record["state"] = STATE_COMPLETED
            record["error"] = None
            record["updated_at"] = _now()
            self._flush_locked()
            # Active pointer moves only after both registries agree.
            self._write_pointer_locked(digest)
            with open(self.journal_path, "a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(
                        {
                            "ts": _now(),
                            "event": "release_completed",
                            "release": release_id,
                            "sha256": digest,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def active_digest(self) -> str | None:
        with self.lock:
            with open(self.pointer_path, "r", encoding="utf-8") as fh:
                return json.load(fh)["active_digest"]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Controller:
    def __init__(self, data_dir: str, secret: str, registries: list[RegistryClient]):
        self.store = Store(data_dir)
        self.secret = secret
        self.registries = registries
        # Serialize driving a single release; durable persistence means other
        # requests can still reconcile from receipts.
        self.drive_locks: dict[str, threading.Lock] = {}
        self.drive_locks_guard = threading.Lock()

    def _lock_for(self, release_id: str) -> threading.Lock:
        with self.drive_locks_guard:
            return self.drive_locks.setdefault(release_id, threading.Lock())

    def _op_key(self, release_id: str) -> str:
        return derive_operation_key(self.secret, release_id)

    # ---- public API -----------------------------------------------------

    def submit(self, release_id: str, b64_artifact: str) -> tuple[int, dict]:
        release_id = release_id.strip()
        try:
            validate_release_id(release_id)
            raw = decode_artifact(b64_artifact)
        except ArtifactError as exc:
            return 400, {"ok": False, "error": str(exc)}
        digest = sha256_hex(raw)

        existing = self.store.get(release_id)
        if existing is not None:
            if existing["sha256"] == digest:
                # Same id + same artifact: reconcile if needed and replay the
                # first result. Never triggers another activation.
                if existing["state"] not in TERMINAL_STATES:
                    self._drive(release_id, digest)
                record = self.store.get(release_id)
                return 200, {
                    "ok": record["state"] == STATE_COMPLETED,
                    "replay": True,
                    "release": self._view(record),
                }
            # Used id with a DIFFERENT artifact: explicit refusal; the prior
            # release (and its true status) is preserved untouched.
            self.store.record_attempt(release_id, digest, "different_artifact")
            return 409, {
                "ok": False,
                "error": (
                    f"发布标识 {release_id} 已绑定摘要 {existing['sha256']}，"
                    f"拒绝以不同摘要 {digest} 重复提交（同键异摘要）"
                ),
                "existing_release": self._view(existing),
            }

        # Persist immutable intent BEFORE contacting either registry.
        self.store.persist_intent(release_id, digest)
        return self._drive(release_id, digest)

    def status(self, release_id: str) -> tuple[int, dict]:
        record = self.store.get(release_id)
        if record is None:
            return 404, {"ok": False, "error": f"未知发布标识：{release_id}"}
        if record["state"] not in TERMINAL_STATES:
            self._drive(release_id, record["sha256"])
            record = self.store.get(release_id)
        return 200, {"ok": True, "release": self._view(record)}

    def active_digest(self) -> str | None:
        return self.store.active_digest()

    def active(self) -> dict:
        digest = self.store.active_digest()
        release_id = None
        if digest is not None:
            for rid in self.store.list_ids():
                record = self.store.get(rid)
                if record["state"] == STATE_COMPLETED and record["sha256"] == digest:
                    release_id = rid
                    break
        return {"active_digest": digest, "active_release": release_id}

    # ---- state machine --------------------------------------------------

    def _drive(self, release_id: str, digest: str) -> tuple[int, dict]:
        """Drive a release toward completion; safe to re-run after restart."""
        with self._lock_for(release_id):
            op_key = self._op_key(release_id)
            warnings: list[str] = []
            record = self.store.get(release_id)
            if record is None or record["state"] in TERMINAL_STATES:
                record = self.store.get(release_id)
                return 200, {"ok": record["state"] == STATE_COMPLETED,
                             "release": self._view(record)}

            # --- Phase 1/3: reconcile from registry receipts --------------
            # This is what makes a controller restart converge: adopt whatever
            # the registries durably committed, using their signed receipts.
            for reg in self.registries:
                entry = record["registries"].get(reg.name, {})
                if entry.get("activate_receipt"):
                    continue  # evidence already held
                try:
                    remote = reg.status(release_id, op_key)
                except RegistryError as exc:
                    warnings.append(str(exc))
                    continue
                remote_digest = remote.get("digest") if remote.get("activated") else None
                if remote_digest is not None and remote_digest != digest:
                    reason = (
                        f"仓 {reg.name} 回报的摘要 {remote_digest} 不属于发布 "
                        f"{release_id}（期望 {digest}）；发布锁定为拒绝，活动指针不变"
                    )
                    self.store.lock_rejected(release_id, reason)
                    record = self.store.get(release_id)
                    return 409, {"ok": False, "error": reason,
                                 "release": self._view(record)}
                if remote.get("prepared") and not entry.get("prepare_receipt"):
                    self.store.update_registry(
                        release_id, reg.name, prepare_receipt=remote["prepare"]
                    )
                if remote.get("activated"):
                    self.store.update_registry(
                        release_id, reg.name,
                        activate_receipt=remote["activate"],
                        activation_count=max(
                            1, int(remote.get("activation_count", 1))
                        ),
                        activation_attempted=True,
                    )
            record = self.store.get(release_id)

            prepared_all = all(
                record["registries"].get(r.name, {}).get("prepare_receipt")
                for r in self.registries
            )

            # --- Phase 2: prepare both registries --------------------------
            if not prepared_all:
                for reg in self.registries:
                    entry = record["registries"].get(reg.name, {})
                    if entry.get("prepare_receipt"):
                        continue
                    try:
                        receipt = reg.prepare(release_id, op_key, digest)
                    except RegistryError as exc:
                        if exc.kind == "conflict":
                            reason = f"准备阶段被仓拒绝：{exc}"
                            self.store.lock_rejected(release_id, reason)
                            record = self.store.get(release_id)
                            return 409, {"ok": False, "error": reason,
                                         "release": self._view(record)}
                        warnings.append(str(exc))
                        continue
                    if receipt.get("digest") != digest:
                        reason = (
                            f"仓 {reg.name} 准备回执摘要 {receipt.get('digest')} "
                            f"不属于该发布；锁定为拒绝"
                        )
                        self.store.lock_rejected(release_id, reason)
                        record = self.store.get(release_id)
                        return 409, {"ok": False, "error": reason,
                                     "release": self._view(record)}
                    self.store.update_registry(
                        release_id, reg.name, prepare_receipt=receipt
                    )
                record = self.store.get(release_id)
                prepared_all = all(
                    record["registries"].get(r.name, {}).get("prepare_receipt")
                    for r in self.registries
                )
                if prepared_all and record["state"] == STATE_RECEIVED:
                    self.store.update(release_id, state=STATE_PREPARED)
                    record = self.store.get(release_id)

            # --- Phase 3: activate both registries -------------------------
            if prepared_all:
                if record["state"] != STATE_ACTIVATING:
                    self.store.update(release_id, state=STATE_ACTIVATING)
                for reg in self.registries:
                    entry = record["registries"].get(reg.name, {})
                    if entry.get("activate_receipt"):
                        continue  # already durably evidenced -> never re-activate
                    self.store.update_registry(
                        release_id, reg.name, activation_attempted=True
                    )
                    try:
                        receipt = reg.activate(release_id, op_key, digest)
                    except RegistryError as exc:
                        # Connection dropped after durable commit is the key
                        # case: convergence happens on restart / next status.
                        warnings.append(
                            f"激活 {reg.name} 后未获响应，将在重启或下次查询时"
                            f"依据仓端回执收敛：{exc}"
                        )
                        continue
                    if receipt.get("digest") != digest:
                        reason = (
                            f"仓 {reg.name} 激活回执摘要 {receipt.get('digest')} "
                            f"不属于发布 {release_id}（期望 {digest}）；"
                            f"发布锁定为拒绝，活动指针不被改写"
                        )
                        self.store.lock_rejected(release_id, reason)
                        record = self.store.get(release_id)
                        return 409, {"ok": False, "error": reason,
                                     "release": self._view(record)}
                    self.store.update_registry(
                        release_id, reg.name,
                        activate_receipt=receipt,
                        activation_count=1,
                    )
            record = self.store.get(release_id)
            activated = [
                r.name for r in self.registries
                if record["registries"].get(r.name, {}).get("activate_receipt")
            ]
            all_activated = len(activated) == len(self.registries)

            if all_activated:
                # Completion gate: both registries activated the SAME digest.
                digests = {
                    record["registries"][r.name]["activate_receipt"]["digest"]
                    for r in self.registries
                }
                if digests != {digest}:
                    reason = f"双仓激活摘要不一致：{sorted(digests)}"
                    self.store.lock_rejected(release_id, reason)
                    record = self.store.get(release_id)
                    return 409, {"ok": False, "error": reason,
                                 "release": self._view(record)}
                self.store.complete(release_id, digest)
                record = self.store.get(release_id)
                return 200, {"ok": True, "release": self._view(record)}

            message = (
                "发布意图已持久化，正在推进双仓激活；"
                f"已激活仓：{activated or '无'}。"
                + (" 警告：" + "；".join(warnings) if warnings else "")
            )
            return 202, {"ok": False, "pending": True, "message": message,
                         "warnings": warnings, "release": self._view(record)}

    def _view(self, record: dict) -> dict:
        current_digests = set()
        for entry in record["registries"].values():
            receipt = entry.get("activate_receipt")
            if receipt:
                current_digests.add(receipt["digest"])
        return {
            "release": record["release"],
            "sha256": record["sha256"],
            "state": record["state"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "progress": _progress(record),
            "current_digest": (
                record["sha256"]
                if record["state"] == STATE_COMPLETED
                else (next(iter(current_digests)) if len(current_digests) == 1 else None)
            ),
            "registries": record["registries"],
            "prepare_evidence": {
                name: entry.get("prepare_receipt")
                for name, entry in record["registries"].items()
            },
            "activate_evidence": {
                name: entry.get("activate_receipt")
                for name, entry in record["registries"].items()
            },
            "rejected_attempts": record.get("attempts", []),
            "error": record.get("error"),
        }


def _progress(record: dict) -> dict:
    regs = record["registries"]
    return {
        "intent_persisted": True,
        "prepared": [n for n, e in regs.items() if e.get("prepare_receipt")],
        "activated": [n for n, e in regs.items() if e.get("activate_receipt")],
        "state": record["state"],
    }


# ---- HTTP layer ----------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")


def build_handler(controller: Controller):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: str, content_type: str) -> None:
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._json(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self._json(200, {"status": "ok", "service": "controller"})
            if path == "/":
                return self._file(
                    os.path.join(STATIC_DIR, "index.html"),
                    "text/html; charset=utf-8",
                )
            if path == "/app.js":
                return self._file(
                    os.path.join(STATIC_DIR, "app.js"),
                    "application/javascript; charset=utf-8",
                )
            if path == "/api/active":
                return self._json(200, controller.active())
            if path.startswith("/api/releases/"):
                release_id = path.rsplit("/", 1)[-1]
                code, payload = controller.status(release_id)
                return self._json(code, payload)
            return self._json(404, {"error": "unknown path"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/admin/restart":
                token = os.environ.get("ADMIN_TOKEN", "")
                if not token or self.headers.get("X-Admin-Token") != token:
                    return self._json(403, {"error": "forbidden"})
                self._json(200, {"restarting": True})

                def _exit():
                    time.sleep(0.3)
                    os._exit(0)  # compose restart policy brings it back

                threading.Thread(target=_exit, daemon=True).start()
                return
            if path != "/api/releases":
                return self._json(404, {"error": "unknown path"})
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            code, result = controller.submit(
                str(payload.get("release_id", "")),
                str(payload.get("artifact", "")),
            )
            self._json(code, result)

    return Handler


def main() -> None:
    port = int(os.environ.get("CONTROL_PORT", "8000"))
    data_dir = os.environ.get("CONTROLLER_DATA_DIR", "/data/controller")
    secret = os.environ.get("OP_KEY_SECRET", "probe-calibration-master-secret")
    registries = [
        RegistryClient(
            "registry-a",
            os.environ.get("REGISTRY_A_URL", "http://registry-a:8080"),
        ),
        RegistryClient(
            "registry-b",
            os.environ.get("REGISTRY_B_URL", "http://registry-b:8080"),
        ),
    ]
    controller = Controller(data_dir, secret, registries)

    # Reconcile any in-flight releases left by a previous (crashed/restarted)
    # process using registry-side receipts before accepting traffic.
    for rid in controller.store.list_ids():
        record = controller.store.get(rid)
        if record["state"] not in TERMINAL_STATES:
            try:
                controller._drive(rid, record["sha256"])
            except Exception as exc:  # never block startup on reconciliation
                print(f"startup reconcile {rid} deferred: {exc}", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(controller))
    print(f"control service listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
