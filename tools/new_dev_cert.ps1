<#
.SYNOPSIS
    Issues an HTTPS certificate for the workspaceradio web UI, signed by the
    same mkcert development CA that f:\tracker\Certificate\rootCA.pem uses.

.DESCRIPTION
    Run this on the Windows development machine whenever the Pi's hostname or
    IP address changes, then copy Certificate\workspaceradio.pem and
    Certificate\workspaceradio-key.pem to the Raspberry Pi.

    rootCA.pem is what every client that opens the UI needs to trust. Install
    it once per device:
      - Windows: double-click it, Install Certificate, Local Machine, Trusted
        Root Certification Authorities. `mkcert -install` does this for you on
        the machine that runs this script.
      - Raspberry Pi: sudo cp rootCA.pem /usr/local/share/ca-certificates/ &&
        sudo update-ca-certificates
      - Phone/tablet: install it, then make sure it is *also* enabled for
        Wi-Fi/VPN under user credentials, which Android and iOS require
        separately.

.PARAMETER PiHost
    Extra host names or IPs to embed in the certificate. Defaults to the
    current Pi (workspaceradio.local and its LAN address).

.EXAMPLE
    .\tools\new_dev_cert.ps1
    .\tools\new_dev_cert.ps1 -PiHost workspaceradio.local -PiHost 192.168.1.148
#>
[CmdletBinding()]
param(
    [string[]]$PiHost = @(),
    [string]$Alias = "piradio",
    [switch]$InstallCa
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$certDir = Join-Path $repoRoot "Certificate"
$leafName = "workspaceradio"

if (-not (Get-Command mkcert -ErrorAction SilentlyContinue)) {
    throw "mkcert is not on PATH. Install it with: winget install FiloSottile.mkcert"
}

function Invoke-Mkcert {
    <#  mkcert writes its progress notes to stderr, which PowerShell turns into
        error records. Run it out-of-process with the streams redirected to
        files so the notes can be shown as plain text, then decide success from
        the exit code. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$MkcertArgs)
    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $quoted = $MkcertArgs | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        }
        $process = Start-Process -FilePath (Get-Command mkcert).Source -ArgumentList $quoted `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $lines = @(Get-Content -LiteralPath $stdoutFile -ErrorAction SilentlyContinue)
        $lines += @(Get-Content -LiteralPath $stderrFile -ErrorAction SilentlyContinue)
    } finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
    $lines | ForEach-Object { Write-Host $_ }
    if ($process.ExitCode -ne 0) {
        throw "mkcert $($MkcertArgs -join ' ') failed with exit code $($process.ExitCode)"
    }
    return $lines
}

# Every name the UI might be opened with. The Pi answers to several of these
# at once: an mDNS name, a router DNS name, and a raw IP, and the browser
# shows a certificate warning unless the name in the address bar is one the
# certificate covers.
$names = @("localhost", "127.0.0.1", "::1")
foreach ($hostName in $PiHost) {
    $names += $hostName
}
if ($names -notcontains "$Alias.local") {
    $names += "$Alias.local"
}

$resolved = @()
foreach ($name in $names) {
    try {
        foreach ($address in [System.Net.Dns]::GetHostAddresses($name)) {
            if ($address.AddressFamily -eq "InterNetwork" -and $address.IPAddressToString -notlike "169.254.*") {
                $resolved += $address.IPAddressToString
            }
        }
    } catch {
        # A name that does not resolve is still worth putting in the cert: it
        # may resolve on the Pi, or on whichever machine is browsing.
    }
}
$names += $resolved | Sort-Object -Unique
$names = $names | Sort-Object -Unique

Write-Host "Certificate will cover:"
$names | ForEach-Object { Write-Host "  $_" }

if ($InstallCa) {
    Invoke-Mkcert -MkcertArgs "-install" | Out-Null
}

New-Item -ItemType Directory -Path $certDir -Force | Out-Null

# -CAROOT is not needed: mkcert reuses its own root, which is the CA already
# trusted by the machines set up for the timetracker.
Push-Location $certDir
try {
    Invoke-Mkcert -MkcertArgs (@("-cert-file", "$leafName.pem", "-key-file", "$leafName-key.pem") + $names) | Out-Null
    $caRoot = (Invoke-Mkcert -MkcertArgs "-CAROOT" | Select-Object -Last 1).ToString().Trim()
    Copy-Item -LiteralPath (Join-Path $caRoot "rootCA.pem") -Destination (Join-Path $certDir "rootCA.pem") -Force
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Wrote:"
Get-ChildItem -LiteralPath $certDir -Filter *.pem | ForEach-Object { Write-Host "  $($_.Name)" }
Write-Host ""
Write-Host "Copy these to the Raspberry Pi (both are needed to serve HTTPS):"
Write-Host "  scp `"$certDir\$leafName.pem`" `"$certDir\$leafName-key.pem`" <pi>:~/Workspaceradio/Certificate/"
Write-Host ""
Write-Host "Then start the server with HTTPS alongside plain HTTP:"
Write-Host "  python radio.py serve --https-port 8687"
Write-Host ""
Write-Host "Clients that open https://workspaceradio.local:8687 need Certificate\rootCA.pem installed and trusted."