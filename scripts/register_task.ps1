# 작업 스케줄러 등록: 매일 02:00 scripts\night_run.ps1 실행, 절전 중이면 깨움(WakeToRun).
# 관리자 PowerShell 에서 실행:  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 [-User taxbot] [-At 02:00]
param(
    [string]$TaskName = "taxauto-night",
    [string]$At = "02:00",
    [string]$User = $env:USERNAME
)
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\night_run.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 5) -MultipleInstances IgnoreNew
# 로그온 여부와 관계없이 실행하려면 등록 시 비밀번호 입력(S4U 는 네트워크 드라이브 접근 불가)
$Cred = Get-Credential -UserName $User -Message "작업 실행 계정 비밀번호 (저장은 Windows 가 함, 파일에 남지 않음)"
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -User $Cred.UserName -Password $Cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null

Write-Host "등록 완료: $TaskName (매일 $At, WakeToRun)"
Write-Host "전원 옵션 > 고급 > 절전 > '절전 해제 타이머 허용' 을 '사용' 으로 켜세요."
Write-Host "지금 시험 실행: Start-ScheduledTask -TaskName $TaskName"
