param(
  [string]$DataRoot = "",
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "== douyin-auto-upload setup =="
Write-Host "skill: $SkillRoot"

if ([string]::IsNullOrWhiteSpace($DataRoot)) {
  $DataRoot = Read-Host "请输入数据根目录（建议非 C 盘，例如 D:\AgentData\douyin-auto-upload）"
}
if ([string]::IsNullOrWhiteSpace($DataRoot)) { throw "未指定数据根" }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
if ($DataRoot -like "C:\*") { Write-Warning "数据根在 C 盘：$DataRoot；建议改到非系统盘，并确认有写权限" }

$candidates = @()
if (-not [string]::IsNullOrWhiteSpace($Python)) { $candidates += ,@($Python) }
if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3.11") }
if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
if (Get-Command python3 -ErrorAction SilentlyContinue) { $candidates += ,@("python3") }
$PythonExe = ""
$PythonArgs = @()
foreach ($cand in $candidates) {
  $exe = $cand[0]
  $extra = @()
  if ($cand.Count -gt 1) { $extra = $cand[1..($cand.Count - 1)] }
  & $exe @extra -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>$null
  if ($LASTEXITCODE -eq 0) {
    $PythonExe = $exe
    $PythonArgs = $extra
    break
  }
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
  throw "未找到 Python 3.11+；请安装后重试，或用 -Python 指定路径"
}
& $PythonExe @PythonArgs -c "import sys; print('python', sys.version)"
if ($LASTEXITCODE -ne 0) { throw "Python 版本检查失败" }

$chromeCandidates = @(
  "C:\Program Files\Google\Chrome\Application\chrome.exe",
  "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chrome) { throw "未找到 Google Chrome；请先安装 Chrome" }
Write-Host "chrome: $chrome"

& $PythonExe @PythonArgs -c "import websocket" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "安装 websocket-client ..."
  & $PythonExe @PythonArgs -m pip install --user -r "$SkillRoot\requirements.txt"
  if ($LASTEXITCODE -ne 0) { throw "websocket-client 安装失败；请检查网络/pip" }
}

& $PythonExe @PythonArgs "$SkillRoot\scripts\cli.py" --data-root $DataRoot init
if ($LASTEXITCODE -ne 0) { throw "初始化数据根失败" }
& $PythonExe @PythonArgs "$SkillRoot\scripts\cli.py" doctor
if ($LASTEXITCODE -ne 0) { throw "doctor 自检失败" }

Write-Host ""
Write-Host "== 安装完成 =="
Write-Host "数据根：$DataRoot"
Write-Host "之后用这个 Python 执行命令：$PythonExe $($PythonArgs -join ' ')"
Write-Host '下一步：python scripts\cli.py account add --display "账号名" --slug your-account'
Write-Host "然后：python scripts\cli.py account check --slug your-account --wait-login 300"
