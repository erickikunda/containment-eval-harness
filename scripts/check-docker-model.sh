#!/bin/sh
# Run after check-offline-image.sh has built containment-preflight:check.
set -eu
model_report_dir=$(mktemp -d "$PWD/.harness/model-lab.XXXXXX")
model_volume="containment-$(basename "$model_report_dir")"
docker volume create "$model_volume" > /dev/null
model_passed=false
finish() {
  if [ "$model_passed" = true ]; then
    docker volume rm "$model_volume" > /dev/null
  else
    printf 'Check failed; retained Docker volume: %s\n' "$model_volume" >&2
  fi
  printf 'Model adapter rehearsal reports: %s\n' "$model_report_dir"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
model_run() {
  docker run --rm --init --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
    --mount "type=volume,src=$model_volume,dst=/var/lib/containment" \
    --mount "type=bind,src=$PWD/examples,dst=/examples,readonly" \
    --mount "type=bind,src=$PWD/tests/model_stub.py,dst=/model_stub.py,readonly" \
    --mount "type=bind,src=$PWD/scripts/model-lab-probe.py,dst=/model-probe.py,readonly" \
    --mount "type=bind,src=$PWD/scripts/docker-lab-probe.py,dst=/probe.py,readonly" \
    "$@"
}
model_run --entrypoint python containment-preflight:check /model-probe.py success \
  > "$model_report_dir/success.json"
model_run --entrypoint python containment-preflight:check /model-probe.py timeout \
  > "$model_report_dir/timeout.json"
model_status=0
model_run --entrypoint python containment-preflight:check /model-probe.py crash \
  > "$model_report_dir/crash.json" || model_status=$?
test "$model_status" -eq 137
model_run containment-preflight:check --state-dir /var/lib/containment reconcile \
  > "$model_report_dir/recovery.json"
model_run containment-preflight:check --state-dir /var/lib/containment reconcile \
  > "$model_report_dir/recovery-again.json"
model_run --entrypoint python containment-preflight:check /model-probe.py verify \
  > "$model_report_dir/verification.json"
uv run --no-sync python - "$model_report_dir" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
recovery = json.loads((root / "recovery.json").read_text())
assert len(recovery) == 1 and recovery[0]["outcome"] == "interrupted"
assert json.loads((root / "recovery-again.json").read_text()) == []
assert json.loads((root / "verification.json").read_text())["verified_trials"] == 3
print("Offline model adapter wire rehearsal passed: tool loop, timeout, SIGKILL recovery; no real model.")
PY
model_passed=true
