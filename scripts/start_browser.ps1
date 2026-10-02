# 위하고 조작 전용 크롬 실행 (원격 디버깅 포트 127.0.0.1:9222)
#
# - 전용 프로필 폴더를 쓴다(평소 쓰는 크롬 프로필과 분리). 크롬 136+ 는 기본 프로필에서 원격 디버깅을 막으므로 필수.
# - 처음 1회: 이 스크립트로 띄운 창에서 사람이 위하고에 직접 로그인(2차 인증 포함)하고 '로그인 유지'를 켠다.
#   이후 에이전트는 이 세션을 그대로 쓴다. 비밀번호는 어디에도 저장하지 않는다.
# - 포트는 127.0.0.1 에만 연다. 이 포트에 접속하는 프로그램은 브라우저를 완전히 조종할 수 있으므로
#   전용 Windows 계정에서만 실행하고, 방화벽에서 9222 외부 접근을 막는다.
#
# 사용:
#   powershell -ExecutionPolicy Bypass -File scripts\start_browser.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start_browser.ps1 -ApplyUrlPolicy   # 홈택스 URL 차단 정책 등록(1회)
param(
    [string]$ChromePath = "",
    [string]$ProfileDir = "$env:LOCALAPPDATA\taxauto\chrome-wehago",
    [int]$Port = 9222,
    [string]$StartUrl = "https://www.wehago.com/",
    [switch]$ApplyUrlPolicy,
    [switch]$RemoveUrlPolicy
)
$ErrorActionPreference = "Stop"

# --- (선택) 크롬 정책: URLBlocklist ---------------------------------------------------------
# 현재 Windows 사용자(HKCU) 범위로 등록 → 전용 Windows 계정에서 실행하면 그 계정의 크롬에만 적용.
# 홈택스·국세청 사이트를 브라우저 수준에서 차단한다(에이전트가 어떤 방법을 써도 열리지 않음).
# ※ 위하고 '홈택스 자료 수집'이 브라우저에서 홈택스 창을 띄우는 방식이면 이 차단 때문에 실패할 수 있다(추정).
#    그 경우 학습모드에서 확인 후 제출 관련 경로만 막도록 목록을 줄인다.
$PolicyKey = "HKCU:\Software\Policies\Google\Chrome\URLBlocklist"
$Blocked = @(
    "hometax.go.kr",
    "nts.go.kr",
    "wetax.go.kr",
    "chrome://settings/passwords",
    "chrome://password-manager"
)
if ($RemoveUrlPolicy) {
    if (Test-Path $PolicyKey) { Remove-Item $PolicyKey -Recurse -Force }
    Write-Host "URLBlocklist 정책 제거. 크롬을 완전히 껐다 켜야 반영된다."
    exit 0
}
if ($ApplyUrlPolicy) {
    New-Item -Path $PolicyKey -Force | Out-Null
    $i = 1
    foreach ($u in $Blocked) {
        New-ItemProperty -Path $PolicyKey -Name "$i" -Value $u -PropertyType String -Force | Out-Null
        $i++
    }
    # 비밀번호 저장 기능 끄기(프로필에 비밀번호가 남지 않게)
    $Base = "HKCU:\Software\Policies\Google\Chrome"
    New-ItemProperty -Path $Base -Name "PasswordManagerEnabled" -Value 0 -PropertyType DWord -Force | Out-Null
    Write-Host "URLBlocklist 등록: $($Blocked -join ', ')  (chrome://policy 에서 확인)"
}

# --- 이미 떠 있으면 그대로 사용 ------------------------------------------------------------
try {
    $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
    Write-Host "이미 실행 중: $($v.Browser)  (CDP http://127.0.0.1:$Port)"
    exit 0
} catch { }

# --- 크롬 찾기 ----------------------------------------------------------------------------
if (-not $ChromePath) {
    $cands = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    $ChromePath = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $ChromePath -or -not (Test-Path $ChromePath)) {
    Write-Error "chrome.exe 를 찾지 못함. -ChromePath 로 지정"
    exit 1
}
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

$ChromeArgs = @(
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=`"$ProfileDir`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    $StartUrl
)
Start-Process -FilePath $ChromePath -ArgumentList $ChromeArgs | Out-Null

# --- 포트 열릴 때까지 대기 ----------------------------------------------------------------
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        Write-Host "실행됨: $($v.Browser)  (CDP http://127.0.0.1:$Port, 프로필 $ProfileDir)"
        exit 0
    } catch { }
}
Write-Error "CDP 포트($Port)가 열리지 않음. 같은 프로필로 이미 떠 있는 크롬(디버깅 포트 없이)을 모두 닫고 다시 실행"
exit 1
