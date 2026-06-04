param(
    [Parameter(Position = 0)]
    [ValidateSet("config", "up", "down", "restart", "ps", "logs", "health", "verify", "doctor", "repair", "pull", "build", "help")]
    [string]$Command = "help",

    [Parameter(Position = 1)]
    [string]$Target = "all",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ComposeDir = Join-Path $RootDir "compose"
$EnvFile = Join-Path $ComposeDir ".env"

$ComposeFiles = @(
    "docker-compose.yml",
    "docker-compose.lmcache.yml",
    "docker-compose.prefill.yml",
    "docker-compose.decode.yml",
    "docker-compose.gateway.yml"
)

function Show-Usage {
    @"
Usage:
  .\ops\pd-stack.ps1 <command> [target] [extra docker compose args...]

Commands:
  config          Render merged Compose config
  up              Start stack or target service group
  down            Stop full stack
  restart         Restart stack or target service group
  ps              Show service status
  logs            Tail logs for stack or target service group
  health          Check gateway health endpoint
  verify          Run MVP TTFT verification script
  doctor          Verify rendered Compose critical settings
  repair          Doctor + force recreate stack or target service group
  pull            Pull images
  build           Build gateway image

Targets:
  all             lmcache + prefill + decode + gateway (default)
  cache           lmcache-server
  prefill         vllm-prefill
  decode          vllm-decode
  gateway         gateway
"@
}

function Get-ComposeArgs {
    $args = @()
    foreach ($file in $ComposeFiles) {
        $args += @("-f", (Join-Path $ComposeDir $file))
    }
    if (Test-Path -LiteralPath $EnvFile) {
        $args += @("--env-file", $EnvFile)
    }
    return $args
}

function Get-Services([string]$Name) {
    switch ($Name) {
        "all" { return @("lmcache-server", "vllm-prefill", "vllm-decode", "gateway") }
        "cache" { return @("lmcache-server") }
        "lmcache" { return @("lmcache-server") }
        "prefill" { return @("vllm-prefill") }
        "decode" { return @("vllm-decode") }
        "gateway" { return @("gateway") }
    }
}

function Invoke-Compose([string[]]$Args) {
    & docker compose @Args
}

function Test-RenderedConfig([string]$RenderedConfig) {
    $failed = $false

    $required = @(
        "lmcache/vllm-openai:v0.4.5-cu129",
        "lmcache/standalone:v0.4.5-cu129",
        "/model",
        "--disable-custom-all-reduce",
        "NCCL_DEBUG"
    )
    foreach ($needle in $required) {
        if (-not $RenderedConfig.Contains($needle)) {
            Write-Host "ERROR: rendered Compose missing required setting: $needle" -ForegroundColor Red
            $failed = $true
        }
    }

    $stale = @(
        "latest-nightly",
        "standalone:nightly",
        "--model /model",
        "lmcache/lmcache-server"
    )
    foreach ($needle in $stale) {
        if ($RenderedConfig.Contains($needle)) {
            Write-Host "ERROR: rendered Compose still contains stale setting: $needle" -ForegroundColor Red
            $failed = $true
        }
    }

    if ($failed) {
        throw "Rendered Compose is stale; fix compose/.env or pull latest repo files before starting containers."
    }

    Write-Host "OK: rendered Compose uses fixed LMCache/vLLM images, positional /model, disabled custom all-reduce, and NCCL_DEBUG."
}

function Invoke-Doctor([string[]]$Args) {
    $rendered = (& docker compose @($ComposeArgs + @("config") + $Args)) -join "`n"
    Test-RenderedConfig $rendered
}

if ($Command -eq "help") {
    Show-Usage
    exit 0
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Warning "$EnvFile not found; using defaults from Compose files."
}

if ($Target -notin @("all", "cache", "lmcache", "prefill", "decode", "gateway")) {
    $ExtraArgs = @($Target) + $ExtraArgs
    $Target = "all"
}

$ComposeArgs = Get-ComposeArgs
$Services = Get-Services $Target

switch ($Command) {
    "config" {
        Invoke-Compose ($ComposeArgs + @("config") + $ExtraArgs)
    }
    "up" {
        Invoke-Compose ($ComposeArgs + @("up", "-d", "--build") + $ExtraArgs + $Services)
    }
    "down" {
        Invoke-Compose ($ComposeArgs + @("down") + $ExtraArgs)
    }
    "restart" {
        Invoke-Compose ($ComposeArgs + @("restart") + $ExtraArgs + $Services)
    }
    "ps" {
        Invoke-Compose ($ComposeArgs + @("ps") + $ExtraArgs)
    }
    "logs" {
        Invoke-Compose ($ComposeArgs + @("logs", "-f", "--tail=200") + $ExtraArgs + $Services)
    }
    "health" {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/healthz" | Select-Object -ExpandProperty Content
    }
    "verify" {
        if (-not $env:API_URL) {
            $env:API_URL = "http://127.0.0.1:8000/v1/chat/completions"
        }
        if (-not $env:GATEWAY_API_KEY) {
            $env:GATEWAY_API_KEY = "sk-mvp-change-me"
        }
        & python (Join-Path $RootDir "backend/tests/test_verification.py")
    }
    "doctor" {
        Invoke-Doctor $ExtraArgs
    }
    "repair" {
        Invoke-Doctor @()
        Invoke-Compose ($ComposeArgs + @("up", "-d", "--build", "--force-recreate", "--remove-orphans") + $ExtraArgs + $Services)
        Invoke-Compose ($ComposeArgs + @("ps") + $Services)
    }
    "pull" {
        Invoke-Compose ($ComposeArgs + @("pull") + $ExtraArgs)
    }
    "build" {
        Invoke-Compose ($ComposeArgs + @("build", "gateway") + $ExtraArgs)
    }
}
