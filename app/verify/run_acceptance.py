#!/usr/bin/env python3
"""End-to-end acceptance checks run by the one-shot `verify` Compose service.

Checks, in order:
  1. the health page, console page and release API answer over HTTP;
  2. a normal release completes with identical SHA-256 on both registries;
  3. invalid Base64 / oversized artifacts / same-id-different-artifact return
     specific feedback and leave the prior good release state intact;
  4. replay (same id, same artifact) does not activate a second time;
  5. the headline scenario: registry B commits activation but drops the
     response; the controller is restarted; after recovery, both registries
     must show the same final digest, the activation count stays at 1 and the
     prepare/activation evidence is present;
  6. a foreign digest locks the release REJECTED without moving the pointer.

Exits 0 only if every check passes.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

CTRL = os.environ.get("CONTROLLER_URL", "http://controller:8000")
REGA = os.environ.get("REGISTRY_A_URL", "http://registry-a:8080")
REGB = os.environ.get("REGISTRY_B_URL", "http://registry-b:8080")
SECRET = os.environ.get("OP_KEY_SECRET", "probe-calibration-master-secret")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "verify-admin-token")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(name)


def http(method: str, url: str, payload=None, headers=None, timeout=5.0):
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def wait_health(url: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(1)
    return False


def submit(rid: str, raw: bytes):
    return http("POST", CTRL + "/api/releases",
                {"release_id": rid, "artifact": base64.b64encode(raw).decode()})


def status(rid: str):
    return http("GET", f"{CTRL}/api/releases/{rid}")


def raw_get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def main() -> int:
    run_id = f"acc-{int(time.time())}"
    print(f"== acceptance run {run_id} ==", flush=True)

    check("controller health reachable", wait_health(CTRL))
    check("registry-a health reachable", wait_health(REGA))
    check("registry-b health reachable", wait_health(REGB))

    code, body = raw_get(CTRL + "/")
    check("console page served", code == 200 and b"calib" in body)
    code, body = raw_get(CTRL + "/app.js")
    check("console script served", code == 200)

    # --- happy path -----------------------------------------------------
    good_raw = f"acceptance-calibration-package-{run_id}".encode()
    good_digest = __import__("hashlib").sha256(good_raw).hexdigest()
    rid_good = run_id + "-good"
    code, body = submit(rid_good, good_raw)
    check("good release returns 200", code == 200, f"got {code}")
    check("good release COMPLETED",
          body.get("release", {}).get("state") == "COMPLETED",
          body.get("release", {}).get("state"))
    check("good digest matches SHA-256",
          body["release"]["sha256"] == good_digest)

    code, act = http("GET", CTRL + "/api/active")
    check("active pointer moved to good digest",
          act.get("active_digest") == good_digest,
          f"pointer={act.get('active_digest')}")
    code, ra = http("GET", REGA + "/v1/active")
    code, rb = http("GET", REGB + "/v1/active")
    check("both registries active on good digest",
          ra["active_digest"] == good_digest == rb["active_digest"],
          f"a={ra['active_digest']} b={rb['active_digest']}")

    # prepare + activate evidence on both registries
    ev = body["release"]
    check("prepare evidence present for both registries",
          all(ev["prepare_evidence"].values()))
    check("activation evidence present for both registries",
          all(ev["activate_evidence"].values()))

    # --- input validation & preservation --------------------------------
    code, body = http("POST", CTRL + "/api/releases",
                      {"release_id": rid_good, "artifact": "@@not-base64@@"})
    check("illegal Base64 -> 400 with feedback",
          code == 400 and "Base64" in body.get("error", ""), str(body))

    big = base64.b64encode(b"x" * (64 * 1024 + 1)).decode()
    code, body = http("POST", CTRL + "/api/releases",
                      {"release_id": run_id + "-big", "artifact": big})
    check("oversized artifact -> 400 with feedback",
          code == 400 and "64 KiB" in body.get("error", ""), str(body)[:120])

    code, body = submit(rid_good, b"a-totally-different-artifact")
    check("used id with different artifact -> 409", code == 409, str(body)[:100])
    code, body = status(rid_good)
    check("prior good release state preserved",
          body["release"]["state"] == "COMPLETED"
          and body["release"]["sha256"] == good_digest)
    code, act = http("GET", CTRL + "/api/active")
    check("active pointer still the good digest", act["active_digest"] == good_digest)

    # --- replay must not activate a second time -------------------------
    code, body = submit(rid_good, good_raw)
    check("same id+artifact replays", code == 200 and body.get("replay") is True)
    counts = {n: e["activation_count"]
              for n, e in body["release"]["registries"].items()}
    check("no second activation anywhere", counts == {"registry-a": 1,
                                                       "registry-b": 1}, str(counts))

    # --- headline: activate, drop response, restart, converge -----------
    rid_drop = run_id + "-drop"
    drop_raw = f"acceptance-calibration-package-v2-{run_id}".encode()
    drop_digest = __import__("hashlib").sha256(drop_raw).hexdigest()
    code, _ = http("POST", REGB + "/fault", {"mode": "drop"})
    check("armed drop fault on registry-b", code == 200)
    code, body = submit(rid_drop, drop_raw)
    check("release accepted while B dropped response", code == 202
          and body.get("pending"), f"code={code}")
    time.sleep(0.5)
    code, rb = http("GET", REGB + "/v1/active")
    check("registry-b durably committed activation despite dropped response",
          rb["active_digest"] == drop_digest, rb.get("active_digest"))
    http("POST", REGB + "/fault", {"mode": "none"})

    code, _ = http("POST", CTRL + "/admin/restart", headers={"X-Admin-Token": ADMIN_TOKEN})
    check("restart requested", code == 200)
    check("controller comes back healthy", wait_health(CTRL, attempts=40))

    # Give startup reconciliation a moment, then query (query also reconciles).
    deadline, final = time.time() + 20, None
    while time.time() < deadline:
        code, body = status(rid_drop)
        st = body.get("release", {}).get("state")
        if st == "COMPLETED":
            final = body["release"]
            break
        time.sleep(1)
    check("dropped release CONVERGED to COMPLETED after restart",
          final is not None, str(final and final["state"]))
    if final:
        counts = {n: e["activation_count"] for n, e in final["registries"].items()}
        check("convergence used registry receipts, no second activation",
              counts == {"registry-a": 1, "registry-b": 1}, str(counts))
        check("final current digest is the release SHA-256",
              final["current_digest"] == drop_digest)
        check("post-restart prepare evidence intact",
              all(final["prepare_evidence"].values()))
        check("post-restart activation evidence intact",
              all(final["activate_evidence"].values()))
    code, ra = http("GET", REGA + "/v1/active")
    code, rb = http("GET", REGB + "/v1/active")
    check("both registries finish on the SAME digest",
          ra["active_digest"] == rb["active_digest"] == drop_digest,
          f"a={ra['active_digest']} b={rb['active_digest']}")
    code, act = http("GET", CTRL + "/api/active")
    check("controller active pointer reflects converged release",
          act["active_digest"] == drop_digest
          and act["active_release"] == rid_drop, str(act))

    # --- foreign digest locks the release, pointer untouched -------------
    http("POST", REGA + "/fault", {"mode": "foreign"})
    rid_evil = run_id + "-foreign"
    code, body = submit(rid_evil, b"foreign-attempt-payload")
    http("POST", REGA + "/fault", {"mode": "none"})
    check("foreign digest -> 409", code == 409, f"code={code}")
    check("foreign release locked REJECTED",
          body.get("release", {}).get("state") == "REJECTED")
    code, act2 = http("GET", CTRL + "/api/active")
    check("active pointer NOT rewritten by foreign release",
          act2["active_digest"] == drop_digest, act2.get("active_digest"))

    print("=" * 60)
    if FAILURES:
        print(f"ACCEPTANCE FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ACCEPTANCE PASSED: all checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
