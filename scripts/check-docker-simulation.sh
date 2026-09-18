#!/bin/sh
# Local Linux integration test only: fake resources, no agents or AWS calls.
set -eu

sh scripts/check-offline-image.sh
lab_report_dir=$(mktemp -d "$PWD/.harness/docker-lab.XXXXXX")
lab_volume="containment-lab-$(basename "$lab_report_dir")"
docker volume create "$lab_volume" > /dev/null
lab_passed=false
finish() {
  if [ "$lab_passed" = true ]; then
    docker volume rm "$lab_volume" > /dev/null
  else
    printf 'Check failed; retained Docker volume for investigation: %s\n' "$lab_volume" >&2
  fi
  printf 'Docker lab reports: %s\n' "$lab_report_dir"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

lab_run() {
  docker run --rm --init --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
    --mount "type=volume,src=$lab_volume,dst=/var/lib/containment" \
    --mount "type=bind,src=$PWD/examples,dst=/examples,readonly" \
    --mount "type=bind,src=$PWD/scripts/docker-lab-probe.py,dst=/probe.py,readonly" \
    "$@"
}

lab_run containment-preflight:check --state-dir /var/lib/containment \
  simulate /examples/simulation.json > "$lab_report_dir/simulation.json"
lab_run containment-preflight:check --state-dir /var/lib/containment \
  list > "$lab_report_dir/before-crash.json"

# Kill only the probe's own container process after persisting a running fake marker.
lab_status=0
lab_run --entrypoint python containment-preflight:check /probe.py crash \
  > "$lab_report_dir/crash.json" || lab_status=$?
test "$lab_status" -eq 137

# A fresh container uses the same volume and absolute state/marker paths.
lab_run containment-preflight:check --state-dir /var/lib/containment \
  reconcile > "$lab_report_dir/recovery.json"
lab_run containment-preflight:check --state-dir /var/lib/containment \
  reconcile > "$lab_report_dir/recovery-again.json"
lab_run --entrypoint python containment-preflight:check /probe.py verify \
  > "$lab_report_dir/verification.json"

uv run --no-sync python - "$lab_report_dir" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
normal = json.loads((root / 'simulation.json').read_text())
before = json.loads((root / 'before-crash.json').read_text())
crash = json.loads((root / 'crash.json').read_text())
recovered = json.loads((root / 'recovery.json').read_text())
assert len(normal) == len(before) == len(recovered) == 1
assert normal[0]['id'] == before[0]['id']
assert recovered[0]['id'] == crash['trial_id']
assert recovered[0]['outcome'] == 'interrupted'
assert recovered[0]['state'] == 'complete'
assert recovered[0]['evidence_seal']['complete'] is False
assert json.loads((root / 'recovery-again.json').read_text()) == []
verified = json.loads((root / 'verification.json').read_text())
assert verified['verified_trials'] == 2
assert verified['simulation_only'] is True
print('Docker simulation and SIGKILL recovery passed; both retained seals verified.')
PY
lab_passed=true
