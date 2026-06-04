param(
    [Parameter(Position = 0)]
    [ValidateSet("doctor", "repair", "logs", "ps", "health", "verify", "exec", "shell", "help")]
    [string]$Command = "doctor",

    [Parameter(Position = 1)]
    [string]$Target = "prefill"
)

$ErrorActionPreference = "Stop"

$Remote = $env:PD_REMOTE
$RemoteDir = if ($env:PD_REMOTE_DIR) { $env:PD_REMOTE_DIR } else { "/data/temp/txs/ai-infra" }
$RemotePort = if ($env:PD_REMOTE_PORT) { $env:PD_REMOTE_PORT } else { "22" }
$RemotePassword = $env:PD_REMOTE_PASSWORD
$ConnectTimeout = if ($env:PD_REMOTE_CONNECT_TIMEOUT) { $env:PD_REMOTE_CONNECT_TIMEOUT } else { "10" }
$ServerAliveInterval = if ($env:PD_REMOTE_SERVER_ALIVE_INTERVAL) { $env:PD_REMOTE_SERVER_ALIVE_INTERVAL } else { "30" }

function Show-Usage {
    @"
Usage:
  `$env:PD_REMOTE="root@117.190.94.226"; `$env:PD_REMOTE_PORT="24132"; .\ops\pd-remote.ps1 <command> [target]
  `$env:PD_REMOTE_DIR="/data/temp/txs/ai-infra"
  `$env:PD_REMOTE_PASSWORD="<set outside git-tracked files>"

Commands:
  doctor          Run remote docker/Compose config checks
  repair          Force recreate remote target service group
  logs            Tail remote target logs
  ps              Show remote service status
  health          Check remote gateway health endpoint
  verify          Run remote MVP TTFT verification
  exec            Run a remote command in the repo directory
  shell           Open an interactive SSH shell in remote repo directory

Examples:
  .\ops\pd-remote.ps1 doctor
  .\ops\pd-remote.ps1 repair prefill
  .\ops\pd-remote.ps1 logs prefill
"@
}

if ($Command -eq "help") {
    Show-Usage
    exit 0
}

if (-not $Remote) {
    Show-Usage
    throw "PD_REMOTE is required, for example: root@node-2"
}

$SshOptions = @(
    "-p", $RemotePort,
    "-o", "ConnectTimeout=$ConnectTimeout",
    "-o", "ServerAliveInterval=$ServerAliveInterval"
)

$AskpassFile = $null
function Initialize-PasswordAuth {
    if (-not $RemotePassword) {
        $script:SshOptions += @("-o", "BatchMode=yes")
        return
    }

    $script:SshOptions += @("-o", "BatchMode=no")
    $script:AskpassFile = Join-Path ([System.IO.Path]::GetTempPath()) ("pd-askpass-" + [System.Guid]::NewGuid().ToString("N") + ".cmd")
    @"
@echo off
powershell -NoProfile -Command "[Console]::Out.Write(`$env:PD_REMOTE_PASSWORD)"
"@ | Set-Content -Encoding ASCII -LiteralPath $script:AskpassFile
    $env:SSH_ASKPASS = $script:AskpassFile
    $env:SSH_ASKPASS_REQUIRE = "force"
    if (-not $env:DISPLAY) {
        $env:DISPLAY = "pd-remote"
    }
}

function Clear-PasswordAuth {
    if ($script:AskpassFile -and (Test-Path -LiteralPath $script:AskpassFile)) {
        Remove-Item -LiteralPath $script:AskpassFile -Force
    }
}

Initialize-PasswordAuth
trap {
    Clear-PasswordAuth
    throw
}

function Invoke-RemoteStack([string]$StackCommand, [string]$StackTarget = "") {
    # Remote examples: bash ops/pd-stack.sh doctor; bash ops/pd-stack.sh repair prefill; bash ops/pd-stack.sh logs prefill.
    $remoteLine = "cd '$RemoteDir' && docker compose version && bash ops/pd-stack.sh $StackCommand"
    if ($StackTarget) {
        $remoteLine = "$remoteLine $StackTarget"
    }
    & ssh @SshOptions $Remote $remoteLine
}

switch ($Command) {
    "doctor" {
        Invoke-RemoteStack "doctor"
    }
    "repair" {
        Invoke-RemoteStack "repair" $Target
    }
    "logs" {
        Invoke-RemoteStack "logs" $Target
    }
    "ps" {
        Invoke-RemoteStack "ps" $Target
    }
    "health" {
        Invoke-RemoteStack "health"
    }
    "verify" {
        Invoke-RemoteStack "verify"
    }
    "exec" {
        if (-not $Target) {
            throw "exec requires a remote command argument"
        }
        & ssh @SshOptions $Remote "cd '$RemoteDir' && $Target"
    }
    "shell" {
        & ssh @SshOptions -t $Remote "cd '$RemoteDir' && exec bash"
    }
}

Clear-PasswordAuth
