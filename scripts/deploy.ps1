# Ships the committed code (git archive HEAD) to the server and rebuilds/restarts the containers.
param([switch]$Test)
. (Join-Path $PSScriptRoot "server-common.ps1")
$archive = Join-Path $env:TEMP "uniqbot-app.tar.gz"
try {
    $cfg = Read-ServerEnv
    $dir = $cfg.UNIQBOT_DIR
    if (git -C $ProjectRoot status --porcelain) { throw "Есть незакоммиченные изменения — на сервер уходит только закоммиченный код. Закоммитьте их или уберите." }
    $commit = (git -C $ProjectRoot rev-parse --short HEAD).Trim()
    git -C $ProjectRoot archive --format=tar.gz -o $archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive не удался" }
    Invoke-Remote $cfg "test -f $dir/.env || { echo 'На сервере нет .env — сначала запустите powershell -ExecutionPolicy Bypass -File scripts\setup-server.ps1' >&2; exit 1; }"
    Send-File $cfg $archive "$dir/app.tar.gz"

    $steps = @(
        "set -e",
        "cd $dir",
        "rm -rf app.new",
        "mkdir app.new",
        "tar -xzf app.tar.gz -C app.new",
        "rm app.tar.gz",
        "echo $commit > app.new/REVISION"
    )
    # The full suite in the Linux image, before anything is replaced: a failing build stops the deploy here
    # (its image is removed right away: the server is shared and the next build uses the layer cache, not this image)
    if ($Test) { $steps += @("docker build --target test -t uniqbot:test app.new", "docker image rm uniqbot:test || true") }
    $steps += @(
        "rm -rf app.old",
        "if [ -d app ]; then mv app app.old; fi",
        "mv app.new app",
        "cd app",
        # The current container's id and restart count, for the health check after the update
        "docker inspect --type container -f '{{.Id}} {{.RestartCount}}' uniqbot > $dir/restarts.before 2>/dev/null || echo 'none 0' > $dir/restarts.before",
        "docker compose up -d --build --remove-orphans",
        "docker compose ps",
        # Only the images labelled in the Dockerfile: the host is shared with the reels bot, whose dangling images and
        # build cache are not ours to prune, so nothing here touches the cache
        "docker image prune -f --filter label=app=uniqbot || true"
    )
    $remoteCmd = $steps -join "; "
    Invoke-Remote $cfg $remoteCmd
    # A bot that fails at start is restarted by Docker and looks "up" at first: look again after a while.
    # A container compose left as it was keeps its restart count; a new or recreated one starts from 0.
    # Only these fields: a plain docker inspect would print the environment from .env.
    Start-Sleep -Seconds 20
    $state = Invoke-Remote $cfg ("cat $dir/restarts.before 2>/dev/null || echo 'none 0'; rm -f $dir/restarts.before; " +
        "docker inspect --type container -f '{{.State.Running}} {{.Id}} {{.RestartCount}}' uniqbot 2>/dev/null || echo missing")
    $fields = (@($state) -join " ").Trim() -split '\s+'  # id and count before, then running, id, count now
    $expected = if ($fields.Count -eq 5 -and $fields[3] -eq $fields[0]) { $fields[1] } else { "0" }
    $healthy = $fields.Count -eq 5 -and $fields[2] -eq "true" -and $fields[4] -eq $expected
    Invoke-Remote $cfg "cd $dir/app; docker compose logs --tail 30"
    if (-not $healthy) { throw "Бот не запустился — смотрите лог выше" }
    Write-Host "Развёрнут коммит $commit"
} finally {
    # Remove-Item fails on the short 8.3 TEMP path that Windows gives a Cyrillic user name
    if (Test-Path $archive) { [IO.File]::Delete($archive) }
}
