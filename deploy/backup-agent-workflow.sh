#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/agent-workflow-2.0}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/agent-workflow-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

if [ ! -d "$PROJECT_DIR" ]; then
  echo "项目目录不存在：$PROJECT_DIR" >&2
  exit 1
fi

mkdir -p "$BACKUP_ROOT"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_name="agent-workflow-backup-${timestamp}.tar.gz"
backup_path="${BACKUP_ROOT}/${backup_name}"

cd "$PROJECT_DIR"

tar \
  --warning=no-file-changed \
  --exclude='.playwright-browsers' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  -czf "$backup_path" \
  data \
  materials \
  logs \
  publish_tasks \
  publish_debug \
  .playwright_profile \
  .playwright_runtime \
  PROJECT-CHECKPOINT.md 2>/tmp/agent-workflow-backup-warnings.log || {
    code="$?"
    if [ "$code" != "1" ]; then
      cat /tmp/agent-workflow-backup-warnings.log >&2 || true
      exit "$code"
    fi
  }

chmod 600 "$backup_path"

find "$BACKUP_ROOT" -type f -name 'agent-workflow-backup-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

echo "备份完成：$backup_path"
du -h "$backup_path" | awk '{print "备份大小：" $1}'
