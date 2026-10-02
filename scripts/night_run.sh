#!/usr/bin/env bash
# 리눅스/맥 cron 용 야간 실행.  예) crontab:  0 2 * * * /opt/taxauto/scripts/night_run.sh
# 전자신고 제출은 하지 않는다.
set -u
ROOT="${TAXAUTO_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
export TAXAUTO_HOME="$ROOT"
cd "$ROOT" || exit 1
mkdir -p logs
LOG="logs/$(date +%F).night_run.log"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

[ -f .venv/bin/activate ] && . .venv/bin/activate || log "[경고] .venv 없음 - 시스템 python 사용"

log "taxauto night 시작"
taxauto night >>"$LOG" 2>&1
RC=$?
log "taxauto night 종료코드 $RC"

if [ "${TAXAUTO_AGENT:-0}" = "1" ] && command -v claude >/dev/null 2>&1; then
  log "claude -p /vat-night-run 시작"
  claude -p "/vat-night-run 스킬 절차대로 오늘 야간 실행 후처리를 해줘. 제출·위하고 조작은 하지 마." \
    --allowedTools "mcp__taxauto" "Read" "Edit(data/**)" >>"$LOG" 2>&1
  log "claude 종료코드 $?"
fi
exit $RC
