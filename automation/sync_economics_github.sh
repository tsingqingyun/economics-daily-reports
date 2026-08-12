#!/bin/zsh
set -u
setopt NULL_GLOB

PROJECT="${ECONOMICS_PROJECT:-/Users/niejingchen/Documents/Update}"
PYTHON="/usr/bin/python3"
PUBLISH_ROOT="${ECONOMICS_PUBLISH_ROOT:-$PROJECT/.github-publish/economics-daily-reports}"
REMOTE_URL="${ECONOMICS_REMOTE_URL:-git@github.com:tsingqingyun/economics-daily-reports.git}"
REPORTS_ROOT="$PUBLISH_ROOT/reports"
AUTOMATION_ROOT="$PUBLISH_ROOT/automation"
STATE_FILE="${ECONOMICS_GITHUB_STATE_FILE:-$PROJECT/state/github_economics_sync.json}"
LOG_FILE="${ECONOMICS_GITHUB_LOG_FILE:-$PROJECT/state/github-economics-sync.log}"

mkdir -p "$PROJECT/state" "$PROJECT/.github-publish"

timestamp() {
  /bin/date "+%Y-%m-%d %H:%M:%S %Z"
}

log() {
  line="[$(timestamp)] $*"
  /bin/echo "$line"
  /bin/echo "$line" >> "$LOG_FILE"
}

write_state() {
  local sync_status="$1"
  local sync_error="$2"
  local sync_commit_sha="$3"
  local sync_report_count="$4"
  "$PYTHON" - "$STATE_FILE" "$sync_status" "$sync_error" "$sync_commit_sha" "$sync_report_count" <<'PY'
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    "status": sys.argv[2],
    "error": sys.argv[3],
    "commit": sys.argv[4],
    "report_count": int(sys.argv[5]),
    "repository": "tsingqingyun/economics-daily-reports",
}
path.parent.mkdir(parents=True, exist_ok=True)
temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
with temp.open("w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temp, path)
PY
}

fail_sync() {
  local sync_error="$1"
  local sync_code="${2:-1}"
  local sync_report_count=0
  if [ -d "$REPORTS_ROOT" ]; then
    sync_report_count=$(find "$REPORTS_ROOT" -type f -name '*.md' | wc -l | tr -d ' ')
  fi
  write_state "failed" "$sync_error" "" "$sync_report_count"
  log "$sync_error"
  exit "$sync_code"
}

log "Starting economics GitHub sync"

if [ ! -d "$PUBLISH_ROOT/.git" ]; then
  if [ -e "$PUBLISH_ROOT" ]; then
    fail_sync "Publish path exists but is not a Git repository: $PUBLISH_ROOT"
  fi
  git clone "$REMOTE_URL" "$PUBLISH_ROOT" || fail_sync "Cannot clone $REMOTE_URL"
fi

origin_url=$(git -C "$PUBLISH_ROOT" remote get-url origin 2>/dev/null || true)
if [ "$origin_url" != "$REMOTE_URL" ]; then
  fail_sync "Unexpected Git remote: $origin_url"
fi

dirty_before=$(git -C "$PUBLISH_ROOT" status --porcelain)
if [ -n "$dirty_before" ]; then
  fail_sync "Publish repository has pre-existing local changes; refusing to mix scopes"
fi

if git -C "$PUBLISH_ROOT" ls-remote --exit-code --heads origin refs/heads/main >/dev/null 2>&1; then
  git -C "$PUBLISH_ROOT" pull --ff-only origin main || fail_sync "Cannot fast-forward from origin/main"
fi

current_branch=$(git -C "$PUBLISH_ROOT" branch --show-current)
if [ -z "$current_branch" ]; then
  git -C "$PUBLISH_ROOT" checkout -b main || fail_sync "Cannot create local main branch"
elif [ "$current_branch" != "main" ]; then
  fail_sync "Publish repository is on unexpected branch: $current_branch"
fi

mkdir -p "$REPORTS_ROOT" "$AUTOMATION_ROOT"

if [ ! -f "$PUBLISH_ROOT/.gitignore" ]; then
  "$PYTHON" - "$PUBLISH_ROOT/.gitignore" <<'PY' || fail_sync "Cannot create publication .gitignore"
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    "*\n"
    "!/.gitignore\n"
    "!/README.md\n"
    "!/reports/\n"
    "!/reports/**\n"
    "!/automation/\n"
    "!/automation/**\n",
    encoding="utf-8",
)
PY
fi

if [ ! -f "$PUBLISH_ROOT/README.md" ]; then
  "$PYTHON" - "$PUBLISH_ROOT/README.md" <<'PY' || fail_sync "Cannot create publication README"
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    "# Economics Daily Reports\n\n"
    "Automated economics and investment daily reports from a private Obsidian workflow.\n\n"
    "Only aggregate reports and the automation code/configuration are published. "
    "Vault state, credentials, Obsidian settings, and item-level notes are excluded.\n",
    encoding="utf-8",
)
PY
fi

copied=0
for source in "$PROJECT"/30_Updates/????-??-??\ 经济投资简报*.md; do
  base=${source:t}
  year=${base%%-*}
  destination_dir="$REPORTS_ROOT/$year"
  mkdir -p "$destination_dir"
  stem=${base%.md}
  identical=0
  for existing in "$destination_dir/$stem.md" "$destination_dir/$stem-v"*.md; do
    if [ -f "$existing" ] && cmp -s "$source" "$existing"; then
      identical=1
      break
    fi
  done
  if [ "$identical" -eq 1 ]; then
    continue
  fi

  destination="$destination_dir/$base"
  if [ -e "$destination" ]; then
    version=2
    while [ -e "$destination_dir/$stem-v$version.md" ]; do
      version=$((version + 1))
    done
    destination="$destination_dir/$stem-v$version.md"
  fi
  cp "$source" "$destination" || fail_sync "Cannot copy report: $source"
  copied=$((copied + 1))
done

cp "$PROJECT/scripts/update_economics_flow.py" "$AUTOMATION_ROOT/update_economics_flow.py" \
  || fail_sync "Cannot copy update_economics_flow.py"
cp "$PROJECT/scripts/run_economics_daily.sh" "$AUTOMATION_ROOT/run_economics_daily.sh" \
  || fail_sync "Cannot copy run_economics_daily.sh"
cp "$PROJECT/scripts/sync_economics_github.sh" "$AUTOMATION_ROOT/sync_economics_github.sh" \
  || fail_sync "Cannot copy sync_economics_github.sh"
cp "$PROJECT/40_Sources/economics_sources.json" "$AUTOMATION_ROOT/economics_sources.json" \
  || fail_sync "Cannot copy economics_sources.json"

git -C "$PUBLISH_ROOT" add -- reports automation README.md .gitignore \
  || fail_sync "Cannot stage the publication whitelist"

"$PYTHON" - "$PUBLISH_ROOT" <<'PY' || fail_sync "Staged-path whitelist validation failed"
import re
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
raw = subprocess.check_output(
    ["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z"]
)
paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
report_pattern = re.compile(
    r"^reports/[0-9]{4}/[0-9]{4}-[0-9]{2}-[0-9]{2} 经济投资简报(?: [0-9]+|-v[0-9]+)?\.md$"
)
allowed_automation = {
    "automation/update_economics_flow.py",
    "automation/run_economics_daily.sh",
    "automation/sync_economics_github.sh",
    "automation/economics_sources.json",
}
unexpected = [
    path
    for path in paths
    if path not in {"README.md", ".gitignore"}
    and path not in allowed_automation
    and not report_pattern.fullmatch(path)
]
if unexpected:
    raise SystemExit("Unexpected staged paths: " + ", ".join(unexpected))
PY

report_count=$(find "$REPORTS_ROOT" -type f -name '*.md' | wc -l | tr -d ' ')
content_changed=0
if ! git -C "$PUBLISH_ROOT" diff --cached --quiet; then
  content_changed=1
  git -C "$PUBLISH_ROOT" config user.name >/dev/null 2>&1 \
    || git -C "$PUBLISH_ROOT" config user.name "Economics Daily Bot"
  git -C "$PUBLISH_ROOT" config user.email >/dev/null 2>&1 \
    || git -C "$PUBLISH_ROOT" config user.email "24307130136@m.fudan.edu.cn"

  commit_date=$(/bin/date "+%Y-%m-%d")
  git -C "$PUBLISH_ROOT" commit -m "daily: $commit_date economics reports" \
    || fail_sync "Cannot commit synchronized reports"
fi
commit_sha=$(git -C "$PUBLISH_ROOT" rev-parse HEAD 2>/dev/null) \
  || fail_sync "Publication repository has no commit to push"

push_attempt=1
push_exit=1
while [ "$push_attempt" -le 3 ]; do
  log "Git push attempt $push_attempt/3"
  git -C "$PUBLISH_ROOT" push -u origin main
  push_exit=$?
  if [ "$push_exit" -eq 0 ]; then
    break
  fi
  if [ "$push_attempt" -lt 3 ]; then
    if [ "$push_attempt" -eq 1 ]; then
      push_wait=10
    else
      push_wait=30
    fi
    log "Waiting ${push_wait}s before retrying Git push"
    /bin/sleep "$push_wait"
  fi
  push_attempt=$((push_attempt + 1))
done

if [ "$push_exit" -ne 0 ]; then
  fail_sync "Git push failed after 3 attempts" "$push_exit"
fi

write_state "success" "" "$commit_sha" "$report_count"
if [ "$content_changed" -eq 1 ]; then
  log "GitHub sync complete: commit=$commit_sha reports=$report_count copied=$copied"
else
  log "GitHub push verified: commit=$commit_sha reports=$report_count; no content changes"
fi
