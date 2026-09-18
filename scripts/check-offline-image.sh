#!/bin/sh
# Run from the repository root. Uses local Docker only; never pushes or deploys.
set -eu
mkdir -p .harness
context_dir=$(mktemp -d "$PWD/.harness/image-build.XXXXXX")
trap 'rm -rf "$context_dir"' EXIT HUP INT TERM
uv build --wheel --out-dir "$context_dir/dist"
uv export --locked --no-dev --no-emit-project --format requirements-txt \
  --output-file "$context_dir/requirements-runtime.txt" > /dev/null
cp -R deployment/examples/assets "$context_dir/assets"
docker build --tag containment-preflight:check --file deployment/Dockerfile "$context_dir"

status=0
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
  --mount "type=bind,src=$PWD/deployment/examples,dst=/config,readonly" \
  containment-preflight:check preflight /config/eks-fargate-app.json /config/assets.json \
  --asset-root /opt/containment/assets > .harness/last-image-preflight.json || status=$?
# Exit 1 is the deliberate readiness block; parse the report to distinguish a real error.
test "$status" -eq 1
uv run --no-sync python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path('.harness/last-image-preflight.json').read_text())
assert report['readiness'] == 'blocked'
assert report['execution_authorized'] is False
assert not [check for check in report['checks'] if check['status'] == 'fail']
assert any(check['id'].startswith('asset:') and check['status'] == 'pass'
           for check in report['checks'])
assert any(check['status'] == 'unverified' for check in report['checks'])
print('Offline image smoke test passed; live AWS readiness remains blocked.')
PY
