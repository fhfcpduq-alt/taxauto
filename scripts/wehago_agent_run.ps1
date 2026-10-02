# 위하고 조작 에이전트 야간 실행 (night_run.ps1 이 엔진 실행 뒤 호출 — 설계자 통합 지점)
#   1) 전용 크롬(CDP 9222) 확인/실행  2) 가드 주입 스크립트 재생성  3) claude -p 로 wehago-vat-night 스킬
# 홈택스 전자신고 '제출'은 하지 않는다. 전자신고 파일 제작까지만.
param(
    [string]$Period = "",
    [int]$MaxClients = 0,          # 0 이면 config/wehago/limits.yaml max_clients_per_run
    [switch]$DryRun                # 브라우저·claude 를 띄우지 않고 명령만 출력
)
$ErrorActionPreference = "Continue"
$Root = if ($env:TAXAUTO_HOME) { $env:TAXAUTO_HOME } else { Split-Path -Parent $PSScriptRoot }
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
$Today = Get-Date -Format "yyyy-MM-dd"
New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
$Log = "$Root\logs\$Today.wehago_agent.log"
function Write-Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Tee-Object -FilePath $Log -Append }

$Activate = "$Root\.venv\Scripts\Activate.ps1"
if (Test-Path $Activate) { . $Activate }

if (-not $DryRun) {
    & powershell -ExecutionPolicy Bypass -File "$Root\scripts\start_browser.ps1" *>> $Log
    if ($LASTEXITCODE -ne 0) { Write-Log "[중단] 전용 크롬(CDP) 실행 실패"; exit 4 }
}
& python -m taxauto.wehago.guard --write-init-script *>> $Log

$Prompt = "wehago-vat-night 스킬 절차대로 위하고 야간 작업을 수행하라. 회차: $(if ($Period) { $Period } else { '오늘 기준 자동' }). " +
          "최대 거래처 수: $(if ($MaxClients -gt 0) { $MaxClients } else { 'limits.yaml 값' }). " +
          "홈택스 제출·신고서 제출은 절대 하지 말 것. 불확실하면 추측하지 말고 검토항목을 남기고 다음 거래처로."
$Cmd = @("-p", $Prompt, "--settings", "config/wehago/claude_night_settings.json", "--permission-mode", "dontAsk", "--output-format", "json")
if ($DryRun) { Write-Log ("claude " + ($Cmd -join ' ')); exit 0 }

Write-Log "claude -p wehago-vat-night 시작"
& claude @Cmd *>> $Log
$code = $LASTEXITCODE
Write-Log "claude 종료코드 $code"
exit $code
