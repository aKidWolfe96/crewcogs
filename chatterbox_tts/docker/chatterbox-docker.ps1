<#
Runs the Chatterbox TTS server (github.com/devnen/Chatterbox-TTS-Server) in
Docker Desktop on Windows for the ChatterboxTTS Red cog, then tells you the
[p]ttsset url to use. Windows version of chatterbox-docker.sh.

Easiest way to run it (works from PowerShell or Command Prompt):
    .\chatterbox-docker.cmd up

Commands:
    up        build and start the server (default)
    down      stop it
    restart   restart it
    logs      follow its logs (Ctrl+C to stop watching)
    status    show whether it's up and answering
    update    pull the newest server code and rebuild

Options:
    -Gpu nvidia|cpu|cu128|cu130|rocm|rdna4|strixhalo
             default: nvidia if nvidia-smi works, otherwise cpu.
             Use cu128 for RTX 50-series cards.
    -Port 8000          host port to publish
    -Dir PATH           where the server code lives (default: %USERPROFILE%\chatterbox-tts-server)
    -Ref REF            git commit/branch/tag to run
    $env:HF_TOKEN       optional Hugging Face token (normally not needed)

Settings chosen on the first `up` are saved in <Dir>\.env, so later commands
reuse them without repeating the options.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "restart", "logs", "status", "update", "help")]
    [string]$Command = "up",
    [string]$Gpu = "",
    [string]$Port = "",
    [string]$Dir = "",
    [string]$Ref = ""
)

$ErrorActionPreference = "Continue"   # native tools are checked via $LASTEXITCODE
$RepoUrl = "https://github.com/devnen/Chatterbox-TTS-Server.git"
# Known-good upstream commit. `update` (or -Ref main) moves to the latest.
$PinnedRef = "915ae289340e10c6047f27f47e22eae9bf350c32"
$Project = "chatterbox-tts"
$Container = "chatterbox-tts"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "warning: $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

if ($Command -eq "help") { Get-Help $MyInvocation.MyCommand.Path -Full | Out-Host; exit 0 }

if (-not $Dir) { $Dir = if ($env:CHATTERBOX_DIR) { $env:CHATTERBOX_DIR } else { Join-Path $HOME "chatterbox-tts-server" } }
if (-not $Gpu -and $env:CHATTERBOX_GPU) { $Gpu = $env:CHATTERBOX_GPU }
if (-not $Port -and $env:CHATTERBOX_PORT) { $Port = $env:CHATTERBOX_PORT }
if (-not $Ref -and $env:CHATTERBOX_REF) { $Ref = $env:CHATTERBOX_REF }

Info "Chatterbox TTS Docker setup: '$Command' (server folder: $Dir)"

# ---------------------------------------------------------------- prerequisites
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git isn't installed: https://git-scm.com/download/win" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Die "Docker isn't installed. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker Compose isn't available. Update Docker Desktop." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Can't talk to Docker. Start Docker Desktop, wait until it says it's running, then try again." }

function Get-Saved([string]$key) {
    $envFile = Join-Path $Dir ".env"
    if (-not (Test-Path $envFile)) { return "" }
    $line = Get-Content $envFile | Where-Object { $_ -match "^$key=" } | Select-Object -Last 1
    if ($line) { return $line.Substring($key.Length + 1) } else { return "" }
}
if (-not $Gpu) { $Gpu = Get-Saved "CHATTERBOX_GPU" }
if (-not $Port) { $Port = Get-Saved "PORT" }
if (-not $Port) { $Port = "8000" }
if ($Port -notmatch '^\d+$') { Die "-Port must be a number" }

function Compose {
    Push-Location $Dir
    # Out-Host keeps docker's output on screen instead of in the return value.
    try { & docker compose -p $Project @args | Out-Host; $code = $LASTEXITCODE } finally { Pop-Location }
    return $code
}

function Test-Server {
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/get_predefined_voices"
        return $true
    } catch { return $false }
}

function Get-LanIp {
    try {
        $cfg = Get-NetIPConfiguration -ErrorAction Stop |
            Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
            Select-Object -First 1
        if ($cfg) { return ($cfg.IPv4Address | Select-Object -First 1).IPAddress }
    } catch { }
    return "<this-PC's-IP>"
}

function Wait-ForServer {
    $limit = 1200; $waited = 0
    Info "Waiting for the server to answer on port $Port."
    Info "The first start downloads the model (a few GB), so this can take a while..."
    while (-not (Test-Server)) {
        $running = docker inspect -f "{{.State.Running}}" $Container 2> $null
        if ("$running".Trim() -ne "true") {
            $null = Compose logs --tail 40
            Die "The container stopped. The log above should say why."
        }
        if ($waited -ge $limit) { Die "Still not answering after $($limit / 60) minutes. Check: .\chatterbox-docker.cmd logs" }
        Start-Sleep -Seconds 10; $waited += 10
        if ($waited % 60 -eq 0) { Write-Host "    ...still starting ($($waited / 60) min)" }
    }
}

function Show-NextSteps {
    $ip = Get-LanIp
    Write-Host ""
    Write-Host "Chatterbox TTS is running." -ForegroundColor Green
    Write-Host "  Web UI:  http://${ip}:$Port"
    Write-Host "  Voices:  $(Join-Path $Dir 'voices')  (drop extra .wav files here, then [p]ttsvoice list)"
    Write-Host ""
    Write-Host "Point the cog at it from Discord:"
    Write-Host "  [p]ttsset url http://${ip}:$Port" -ForegroundColor Green
    Write-Host "  [p]ttsset status"
    Write-Host ""
    Write-Host "If the bot runs on another computer and can't connect, allow port $Port"
    Write-Host "through Windows Firewall (run in an admin PowerShell):"
    Write-Host "  New-NetFirewallRule -DisplayName 'Chatterbox TTS' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow"
}

function Sync-Code([string]$want) {
    if (-not (Test-Path (Join-Path $Dir ".git"))) {
        if ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
            Die "$Dir exists and isn't the Chatterbox repo. Pick another -Dir."
        }
        Info "Downloading Chatterbox-TTS-Server into $Dir"
        # autocrlf=false keeps Linux line endings for the files copied into the container.
        git -c core.autocrlf=false clone --quiet $RepoUrl $Dir
        if ($LASTEXITCODE -ne 0) { Die "git clone failed." }
        git -C $Dir config core.autocrlf false
    } else {
        git -C $Dir fetch --quiet origin
        if ($LASTEXITCODE -ne 0) { Die "git fetch failed." }
    }
    if ($want -eq "main" -or $want -eq "latest") { $want = "origin/main" }
    # Keep config.yaml edits made through the web UI across updates.
    $stashed = $false
    git -C $Dir diff --quiet -- config.yaml
    if ($LASTEXITCODE -ne 0) {
        git -C $Dir stash push --quiet -- config.yaml
        if ($LASTEXITCODE -eq 0) { $stashed = $true }
    }
    git -C $Dir checkout --quiet --detach $want
    if ($LASTEXITCODE -ne 0) { Die "Couldn't check out '$want'." }
    if ($stashed) {
        git -C $Dir stash pop --quiet
        if ($LASTEXITCODE -ne 0) { Warn "your config.yaml changes are kept in 'git stash' in $Dir" }
    }
}

function Write-Settings {
    if (-not $Gpu) {
        $script:Gpu = "cpu"
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            nvidia-smi *> $null
            if ($LASTEXITCODE -eq 0) { $script:Gpu = "nvidia" }
        }
        Info "No -Gpu given; using '$script:Gpu'."
    }
    $file = if ($Gpu -eq "nvidia") { "docker-compose.yml" } else { "docker-compose-$Gpu.yml" }
    if (-not (Test-Path (Join-Path $Dir $file))) {
        Die "Unknown -Gpu '$Gpu' (no $file upstream). Options: nvidia, cpu, cu128, cu130, rocm, rdna4, strixhalo"
    }

    # Upstream hardcodes HF_TOKEN=YOUR_TOKEN_HERE, which Hugging Face rejects as a
    # bad token. Replace it with yours, or blank (fine for the public model).
    $override = @(
        "# Written by chatterbox-docker.ps1 from the crewcogs repo.",
        "services:",
        "  chatterbox-tts-server:",
        "    container_name: $Container",
        "    environment:",
        '      - HF_TOKEN=${HF_TOKEN:-}',
        ""
    ) -join "`n"
    [System.IO.File]::WriteAllText((Join-Path $Dir "docker-compose.override.yml"), $override, $Utf8NoBom)

    $token = if ($env:HF_TOKEN) { $env:HF_TOKEN } else { Get-Saved "HF_TOKEN" }
    # Docker Compose separates COMPOSE_FILE entries with ';' on Windows.
    $envText = @(
        "# Written by chatterbox-docker.ps1. Docker Compose reads this automatically.",
        "CHATTERBOX_GPU=$Gpu",
        "PORT=$Port",
        "COMPOSE_FILE=$file;docker-compose.override.yml",
        "COMPOSE_PATH_SEPARATOR=;",
        "HF_TOKEN=$token",
        ""
    ) -join "`n"
    [System.IO.File]::WriteAllText((Join-Path $Dir ".env"), $envText, $Utf8NoBom)
}

function Test-PortBusy {
    try { return [bool](Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction Stop) }
    catch { return $false }
}

function Test-OurContainer {
    $id = docker ps -q --filter "name=^$Container$" 2> $null
    return [bool]"$id".Trim()
}

function Assert-Installed {
    if (-not (Test-Path (Join-Path $Dir ".git"))) { Die "Not installed at $Dir yet. Run: .\chatterbox-docker.cmd up" }
}

switch ($Command) {
    { $_ -in "up", "update" } {
        if ($Command -eq "update") { Sync-Code $(if ($Ref) { $Ref } else { "main" }) }
        elseif (-not (Test-Path (Join-Path $Dir ".git"))) { Sync-Code $(if ($Ref) { $Ref } else { $PinnedRef }) }
        elseif ($Ref) { Sync-Code $Ref }
        Write-Settings
        if ((Test-PortBusy) -and -not (Test-OurContainer)) {
            Die "Port $Port is already in use (is your old Chatterbox server still running?). Close it, or use -Port 8004."
        }
        Info "Building and starting Chatterbox ($Gpu, port $Port). The first build takes a while."
        $code = Compose up -d --build
        if ($code -ne 0) {
            if ($Gpu -ne "cpu") {
                Warn "If the error mentions the GPU: in Docker Desktop, make sure Settings > General > 'Use the WSL 2 based engine' is on and your NVIDIA driver is up to date. Or rerun with -Gpu cpu."
            }
            Die "Docker Compose failed (see above)."
        }
        Wait-ForServer
        Show-NextSteps
    }
    "down" { Assert-Installed; $null = Compose down }
    "restart" { Assert-Installed; $null = Compose restart; Wait-ForServer; Show-NextSteps }
    "logs" { Assert-Installed; $null = Compose logs -f --tail 100 }
    "status" {
        Assert-Installed
        $null = Compose ps
        if (Test-Server) { Write-Host "Server is answering on port $Port." -ForegroundColor Green }
        else { Write-Host "Server is not answering on port $Port yet." -ForegroundColor Yellow }
    }
}
