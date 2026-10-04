#!/bin/sh
# One-shot acceptance entrypoint for the Compose `verify` service.
# Runs code tests, a build/compile check, then the live HTTP acceptance
# scenario (drop-after-activate + controller restart convergence).
# Exits non-zero if any stage fails.
set -eu

APP_DIR="${APP_DIR:-/app}"

echo "==================== [1/3] code tests ===================="
cd "$APP_DIR"
PYTHONPATH="$APP_DIR/lib" python3 -m unittest discover -s "$APP_DIR/tests" -v

echo "==================== [2/3] build check ===================="
python3 -m py_compile "$APP_DIR/lib/common.py" "$APP_DIR/lib/registry_server.py" \
    "$APP_DIR/lib/controller_server.py" "$APP_DIR/verify/run_acceptance.py"
echo "py_compile OK"

echo "==================== [3/3] HTTP + scenario acceptance ===================="
exec python3 "$APP_DIR/verify/run_acceptance.py"
