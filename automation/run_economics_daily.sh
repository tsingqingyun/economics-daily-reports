#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT="${ECONOMICS_PROJECT:-${SCRIPT_DIR:h}}"
PYTHON="/usr/bin/python3"
UPDATE_SCRIPT="$PROJECT/scripts/update_economics_flow.py"
STATE_DIR="$PROJECT/state"
AUTOMATION_DIR="$PROJECT/automations/economics"
ENV_FILE="$AUTOMATION_DIR/env.zsh"
RUN_LOG="$STATE_DIR/economics-daily.run.log"
MEMORY="$AUTOMATION_DIR/memory.md"
RUN_STATE="$STATE_DIR/economics_last_run.json"
LINK_CHECK="$PROJECT/scripts/check_vault_links.py"
GITHUB_SYNC="$PROJECT/scripts/sync_economics_github.sh"

mkdir -p "$STATE_DIR" "$AUTOMATION_DIR"

timestamp() {
  /bin/date "+%Y-%m-%d %H:%M:%S %Z"
}

log() {
  /bin/echo "[$(timestamp)] $*"
}

load_environment() {
  if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
    log "Loaded environment from $ENV_FILE"
  else
    log "No automation environment file found at $ENV_FILE"
  fi

  if [ -n "${https_proxy:-}" ]; then
    log "HTTPS proxy configured: $https_proxy"
  else
    log "HTTPS proxy is not configured"
  fi
}

check_proxy() {
  "$PYTHON" - <<'PY'
import os
import socket
import sys
from urllib.parse import urlparse

proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
if not proxy:
    print("Proxy preflight skipped: no https_proxy/HTTPS_PROXY")
    sys.exit(0)

parsed = urlparse(proxy)
host = parsed.hostname
port = parsed.port
if not host or not port:
    print(f"Proxy preflight failed: invalid proxy URL {proxy!r}")
    sys.exit(1)

try:
    with socket.create_connection((host, port), timeout=3):
        pass
except OSError as exc:
    print(f"Proxy preflight failed: cannot connect to {host}:{port}: {exc}")
    sys.exit(1)

print(f"Proxy preflight ok: {host}:{port}")
PY
}

check_dns() {
  "$PYTHON" - <<'PY'
import socket
import sys

hosts = [
    "www.federalreserve.gov",
    "www.bls.gov",
    "apps.bea.gov",
    "fredblog.stlouisfed.org",
]

failed = []
for host in hosts:
    try:
        socket.getaddrinfo(host, 443)
    except OSError as exc:
        failed.append(f"{host}: {exc}")

if failed:
    print("DNS preflight failed:")
    for failure in failed:
        print(f"- {failure}")
    sys.exit(1)

print("DNS preflight ok")
PY
}

run_update() {
  "$PYTHON" "$UPDATE_SCRIPT" \
    --vault "$PROJECT" \
    --timeout 45 \
    --retries 2 \
    --retry-delay 5
}

verify_update() {
  "$PYTHON" - "$PROJECT" "$RUN_STATE" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

project = Path(sys.argv[1])
run_state_path = Path(sys.argv[2])
if not run_state_path.exists():
    raise SystemExit(f"Run verification failed: missing {run_state_path}")

run = json.loads(run_state_path.read_text(encoding="utf-8"))
today = dt.date.today().isoformat()
if run.get("run_date") != today:
    raise SystemExit(f"Run verification failed: run_date={run.get('run_date')!r}, expected {today!r}")
if run.get("status") not in {"success", "degraded"}:
    raise SystemExit(f"Run verification failed: status={run.get('status')!r}")
if run.get("stage") != "complete" or run.get("validated") is not True:
    raise SystemExit(
        f"Run verification failed: stage={run.get('stage')!r}, validated={run.get('validated')!r}"
    )

output_path = Path(run.get("output_path") or "")
if not output_path.is_file() or output_path.stat().st_size == 0:
    raise SystemExit(f"Run verification failed: missing or empty report {output_path}")

seen_path = project / "state" / "seen_economics.json"
seen = json.loads(seen_path.read_text(encoding="utf-8"))
if seen.get("last_run_id") != run.get("run_id") or seen.get("last_validated") is not True:
    raise SystemExit("Run verification failed: seen state does not match the current run_id")

print(
    f"Run verification ok: status={run['status']}, candidates={run['candidate_count']}, "
    f"selected={run['selected_count']}, report={output_path}"
)
PY
}

{
  log "Starting economics daily update"
  log "Project: $PROJECT"

  cd "$PROJECT" || {
    log "Cannot cd into project"
    exit 1
  }

  load_environment

  attempt=1
  update_exit=1
  while [ "$attempt" -le 3 ]; do
    proxy_status=0
    check_proxy || proxy_status=$?
    if [ "$proxy_status" -ne 0 ]; then
      log "Proxy preflight failed on attempt $attempt; feed-level retries will capture the exact error"
    fi

    dns_status=0
    check_dns || dns_status=$?
    if [ "$dns_status" -ne 0 ]; then
      log "DNS preflight failed on attempt $attempt; feed-level retries will capture the exact error"
    fi

    log "Update attempt $attempt/3"
    run_update
    update_exit=$?
    if [ "$update_exit" -eq 0 ]; then
      verify_update
      update_exit=$?
    fi
    if [ "$update_exit" -eq 0 ]; then
      break
    fi
    log "Attempt $attempt failed with exit code $update_exit"
    if [ "$attempt" -lt 3 ]; then
      if [ "$attempt" -eq 1 ]; then
        retry_wait=30
      else
        retry_wait=120
      fi
      log "Waiting ${retry_wait}s before the next full attempt"
      /bin/sleep "$retry_wait"
    fi
    attempt=$((attempt + 1))
  done

  if [ "$update_exit" -eq 0 ] && [ -f "$LINK_CHECK" ]; then
    "$PYTHON" "$LINK_CHECK" --vault "$PROJECT"
    link_exit=$?
    if [ "$link_exit" -ne 0 ]; then
      log "Vault link validation failed with exit code $link_exit"
      update_exit=$link_exit
    fi
  fi

  github_status="skipped"
  sync_exit=0
  if [ "$update_exit" -eq 0 ]; then
    if [ -f "$GITHUB_SYNC" ]; then
      /bin/zsh "$GITHUB_SYNC"
      sync_exit=$?
      if [ "$sync_exit" -eq 0 ]; then
        github_status="success"
      else
        github_status="failed:$sync_exit"
        log "GitHub sync failed after its recovery attempts with exit code $sync_exit"
      fi
    else
      github_status="not-configured"
      sync_exit=1
      log "GitHub sync script is missing: $GITHUB_SYNC"
    fi
  fi

  final_exit=$update_exit
  if [ "$final_exit" -eq 0 ] && [ "$sync_exit" -ne 0 ]; then
    final_exit=$sync_exit
  fi

  "$PYTHON" - "$PROJECT" "$final_exit" "$github_status" >> "$MEMORY" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

project = Path(sys.argv[1])
exit_code = sys.argv[2]
github_status = sys.argv[3]
run_state_path = project / "state" / "economics_last_run.json"
run = json.loads(run_state_path.read_text(encoding="utf-8")) if run_state_path.exists() else {}
failures = run.get("failures", [])
now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

print()
print(
    f"{now}: launchd wrapper ran economics update. "
    f"Exit code: {exit_code}. Status: {run.get('status', 'unknown')}. "
    f"Candidates: {run.get('candidate_count', 0)}. "
    f"Selected: {run.get('selected_count', 0)}. "
    f"Top: {run.get('top_title') or '无'}. "
    f"Feed failures: {len(failures)}. Validated: {run.get('validated', False)}. "
    f"Daily note: {run.get('output_path') or '无'}. GitHub sync: {github_status}. "
    f"Error: {run.get('error') or '无'}."
)
PY

  log "Finished economics daily update with exit code $final_exit; GitHub sync: $github_status"
  exit "$final_exit"
} >> "$RUN_LOG" 2>&1
