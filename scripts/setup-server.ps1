# One-time server setup: the data directory for the bot (uid/gid 101) and the local .env, private to root.
param([switch]$Force)
. (Join-Path $PSScriptRoot "server-common.ps1")
$cfg = Read-ServerEnv
$dir = $cfg.UNIQBOT_DIR
$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw "Нет локального .env — создайте его по .env.example и заполните" }
$guard = if ($Force) { "" } else { "if [ -f $dir/.env ]; then echo 'На сервере уже есть .env — заменить его можно только с -Force' >&2; exit 1; fi; " }
# scp then writes .env into an already existing 0600 file: the secrets are never readable by others, even briefly
Invoke-Remote $cfg ("set -e; " + $guard + "mkdir -p $dir/data; chown 101:101 $dir/data; rm -f $dir/.env.upload; umask 077; : > $dir/.env.upload")
Send-File $cfg $envFile "$dir/.env.upload"
Invoke-Remote $cfg "set -e; cd $dir; chmod 600 .env.upload; mv .env.upload .env"
Write-Host "На сервере готовы $dir/.env (600) и $dir/data (101:101). Дальше: powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1 -Test"
