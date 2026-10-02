# 야간 실행 (Windows 작업 스케줄러가 매일 02:00 실행) — 골격. 위하고 에이전트 연결은 설계자 통합 시 추가.
#   1) venv 활성화 → 2) taxauto night → 3) (선택) Claude 헤드리스로 vat-night-run 스킬 → 4) 로그
# 전자신고 제출은 하지 않는다.
$ErrorActionPreference = "Continue"
$Root = if ($env:TAXAUTO_HOME) { $env:TAXAUTO_HOME } else { Split-Path -Parent $PSScriptRoot }
Set-Location $Root
$env:TAXAUTO_HOME = $Root
$env:PYTHONIOENCODING = "utf-8"
$Today = Get-Date -Format "yyyy-MM-dd"
New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
$Log = "$Root\logs\$Today.night_run.log"

function Write-Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Tee-Object -FilePath $Log -Append }

# 1) venv
$Activate = "$Root\.venv\Scripts\Activate.ps1"
if (Test-Path $Activate) { . $Activate } else { Write-Log "[경고] .venv 없음 - 시스템 python 사용" }

# 2) 엔진 야간 실행 (상세 로그: logs\YYYY-MM-DD.log)
Write-Log "taxauto night 시작"
& taxauto night *>> $Log
$EngineExit = $LASTEXITCODE
Write-Log "taxauto night 종료코드 $EngineExit (0=전부 정상, 1=실패/미구현 거래처 있음 또는 중단)"

# 3) (선택) 에이전트 후처리: claude CLI 가 있고 TAXAUTO_AGENT=1 일 때만
if ($env:TAXAUTO_AGENT -eq "1" -and (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Log "claude -p /vat-night-run 시작"
    & claude -p "/vat-night-run 스킬 절차대로 오늘 야간 실행 후처리를 해줘. 제출·위하고 조작은 하지 마." `
        --allowedTools "mcp__taxauto" "Read" "Edit(data/**)" *>> $Log
    Write-Log "claude 종료코드 $LASTEXITCODE"
    # TODO(설계자 통합): wehago-vat-night 스킬(위하고 조작) 호출 순서·조건
} else {
    Write-Log "에이전트 후처리 건너뜀 (TAXAUTO_AGENT=1 아님 또는 claude 없음)"
}

exit $EngineExit
