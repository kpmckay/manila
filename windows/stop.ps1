<#
.SYNOPSIS
    Stop a running Manila. A fallback -- normally you use the tray icon.

.DESCRIPTION
    Manila is stopped by right-clicking its notification-area icon. This script
    is for the case where that icon could not be created, which Manila tells you
    about when it happens: the server is still running and still perfectly
    usable, but there is nothing to stop it from.

    It ends the process the Start Menu shortcut started and nothing else -- it
    matches on the launcher's own path, so another Python you have running is
    left alone.

    Stopping is safe at any moment. Every edit is committed before its response
    is sent, so there is nothing buffered to lose.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'manila.pyw'

$running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
           Where-Object { $_.CommandLine -and $_.CommandLine.Contains($launcher) }

if (-not $running) {
    Write-Host "Manila is not running." -ForegroundColor DarkGray
    return
}

foreach ($proc in $running) {
    Stop-Process -Id $proc.ProcessId -Force
    Write-Host "Stopped Manila (pid $($proc.ProcessId))." -ForegroundColor Green
}
