#!/usr/bin/env bash
#
# Rebuild the production image and redeploy the Portainer stack.
#
# Production runs a **locally built** image, not one pulled from a registry.
# That is the whole reason this script exists: Portainer's "Update stack"
# defaults to re-pulling, which fails outright for an image Docker Hub has
# never heard of. The redeploy below sends pullImage:false so Portainer
# recreates the container against whatever `plexlection:latest` currently is
# on this host.
#
# Once a CI pipeline publishes the image, this becomes a pull-based redeploy
# and the flag flips.
#
#   ./scripts/redeploy-prod.sh            rebuild, then redeploy
#   ./scripts/redeploy-prod.sh --no-build  redeploy the existing image
#
set -euo pipefail

STACK_NAME=plexlection
PORTAINER=http://192.168.1.168:9000
ENDPOINT=2
# Homepage's config is the one place this key already lives. Reading it here
# keeps it out of the repo and out of shell history.
KEY_FILE=/config/homepage/services.yaml
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" != "--no-build" ]]; then
  echo "==> Building plexlection:latest"
  docker build \
    --build-arg DEV_MODE=false \
    --build-arg FFMPEG_SOURCE=static \
    -t plexlection:latest "$REPO"
fi

KEY="$(grep -oE 'ptr_[A-Za-z0-9+/=]+' "$KEY_FILE" | head -1)"
[[ -n "$KEY" ]] || { echo "No Portainer API key found in $KEY_FILE" >&2; exit 1; }

echo "==> Redeploying stack '$STACK_NAME'"
python3 - "$PORTAINER" "$ENDPOINT" "$STACK_NAME" "$KEY" <<'PY'
import json, sys, urllib.request

base, endpoint, name, key = sys.argv[1:5]
hdr = {"X-API-Key": key, "Content-Type": "application/json"}


def call(path, method="GET", body=None):
    req = urllib.request.Request(
        f"{base}/api/{path}", method=method, headers=hdr,
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r) if r.headers.get("Content-Type", "").startswith("application/json") else None


# Resolve by name — stack ids are not stable across environments.
stack = next((s for s in call("stacks") if s["Name"] == name), None)
if stack is None:
    sys.exit(f"No Portainer stack named {name!r}")

sid = stack["Id"]
content = call(f"stacks/{sid}/file")["StackFileContent"]
call(f"stacks/{sid}?endpointId={endpoint}", "PUT", {
    "stackFileContent": content,
    "env": stack.get("Env") or [],
    "prune": False,
    # Critical: the image is local. Pulling would fail.
    "pullImage": False,
})
print(f"    stack {sid} redeployed")
PY

echo "==> Waiting for health"
for _ in $(seq 1 30); do
  status=$(docker inspect plexlection --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  [[ "$status" == "healthy" ]] && { curl -fsS http://localhost:5183/api/health; echo; exit 0; }
  sleep 3
done
echo "Container did not become healthy; check: docker logs plexlection" >&2
exit 1
