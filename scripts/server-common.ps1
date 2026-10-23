# Shared by setup-server.ps1 and deploy.ps1: server settings and ssh/scp with the deploy key.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
# Windows' own OpenSSH client when it is installed: Protect-Key makes the key pass exactly its permission check
$OpenSsh = Join-Path $env:WINDIR "System32\OpenSSH"
$SshExe = if (Test-Path (Join-Path $OpenSsh "ssh.exe")) { Join-Path $OpenSsh "ssh.exe" } else { "ssh" }
$ScpExe = if (Test-Path (Join-Path $OpenSsh "scp.exe")) { Join-Path $OpenSsh "scp.exe" } else { "scp" }

function Protect-Key([string]$Key) {
    # Windows OpenSSH refuses a private key that other local users can read: leave access to the current user only
    if (-not (Test-Path -LiteralPath $Key -PathType Leaf)) { throw "Нет ключа $Key — проверьте UNIQBOT_KEY в scripts/server.env" }
    $ErrorActionPreference = "Continue"  # icacls reports errors on stderr; its exit code is checked below
    $null = & icacls $Key /inheritance:r /grant:r "$($env:USERNAME):F" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Не удалось ограничить доступ к ключу $Key (icacls, код $LASTEXITCODE)" }
}

function Read-ServerEnv {
    $path = Join-Path $PSScriptRoot "server.env"
    if (-not (Test-Path $path)) { throw "Нет scripts/server.env — скопируйте scripts/server.env.example и заполните" }
    $cfg = @{ UNIQBOT_DIR = "/opt/uniqbot" }
    foreach ($line in Get-Content $path -Encoding UTF8) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $cfg[$Matches[1]] = $Matches[2] }
    }
    foreach ($key in "UNIQBOT_HOST", "UNIQBOT_KEY") { if (-not $cfg[$key]) { throw "В scripts/server.env не задан $key" } }
    $cfg.UNIQBOT_KEY = $cfg.UNIQBOT_KEY -replace '^~', $HOME
    Protect-Key $cfg.UNIQBOT_KEY
    return $cfg
}

function Invoke-Remote([hashtable]$Cfg, [string]$Command) {
    & $SshExe -i $Cfg.UNIQBOT_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes $Cfg.UNIQBOT_HOST $Command
    if ($LASTEXITCODE -ne 0) { throw "Команда на сервере завершилась с кодом $LASTEXITCODE" }
}

function Send-File([hashtable]$Cfg, [string]$Local, [string]$Remote) {
    & $ScpExe -i $Cfg.UNIQBOT_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes $Local "$($Cfg.UNIQBOT_HOST):$Remote"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось скопировать $Local на сервер" }
}
