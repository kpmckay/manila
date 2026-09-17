<#
.SYNOPSIS
    Install Manila on Windows: copy it into place and add a Start Menu entry.

.DESCRIPTION
    Manila is stdlib-only Python, so "installing" is copying a folder and making
    a shortcut. Nothing is downloaded, nothing is compiled, no packages are
    touched, and it all happens under your own user account -- no admin rights.

    Your projects and the registry naming them are NOT written here. They live
    in %LOCALAPPDATA%\Manila and %APPDATA%\Manila and are left alone by both
    install and uninstall, so reinstalling never costs you data.

.PARAMETER Desktop
    Also put a shortcut on the Desktop.

.PARAMETER Port
    Port to serve on (default 8000). Baked into the shortcut.

.PARAMETER Uninstall
    Remove the shortcuts and the installed copy. Your projects are untouched.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File windows\install.ps1 -Desktop
#>

[CmdletBinding()]
param(
    [switch] $Desktop,
    [int]    $Port = 8000,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$AppName   = 'Manila'
$Target    = Join-Path $env:LOCALAPPDATA 'Programs\Manila'
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$Source    = Split-Path -Parent $PSScriptRoot

function Write-Step { param($m) Write-Host "  $m" -ForegroundColor DarkGray }

# Stop a Manila running out of $Target, so its own files are not held open
# while we replace them. Safe at any moment: every edit is committed before its
# response is sent, so there is never anything buffered to lose.
function Stop-InstalledManila {
    $launcher = Join-Path $Target 'windows\manila.pyw'
    $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue `
                   -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
               Where-Object { $_.CommandLine -and $_.CommandLine.Contains($launcher) }
    foreach ($proc in $running) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Step "stopped the running Manila (pid $($proc.ProcessId))"
    }
    if ($running) { Start-Sleep -Milliseconds 500 }   # let the handles go
}

# Empty a folder without deleting the folder itself.
#
# Windows will not remove a directory that any process is standing in, and the
# most likely process is the one running this: installing from a prompt opened
# in the Manila folder is the obvious thing to do, and it failed with a bare
# "because it is in use" that named nothing and suggested nothing. The contents
# are not protected that way, and emptying them is all the install needs.
function Clear-Folder {
    param($Path, $What)
    if (-not (Test-Path $Path)) { return }
    try {
        Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop |
            Remove-Item -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Host ""
        Write-Host "Could not clear $Path." -ForegroundColor Red
        Write-Host "Something in it is still open. Almost always one of:"
        Write-Host "  - Manila is running. Right-click its notification-area icon,"
        Write-Host "    then Stop Manila, and run this again."
        Write-Host "  - A terminal or Explorer window is sitting in that folder."
        Write-Host "    This one counts: if your prompt says $Path,"
        Write-Host "    'cd ~' first."
        Write-Host ""
        Write-Host $_.Exception.Message -ForegroundColor DarkGray
        exit 1
    }
}

# --- uninstall ---------------------------------------------------------------

if ($Uninstall) {
    Write-Host "Removing $AppName..." -ForegroundColor Cyan
    foreach ($lnk in @((Join-Path $StartMenu "$AppName.lnk"),
                       (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"))) {
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Step "removed $lnk" }
    }
    if (Test-Path $Target) {
        Stop-InstalledManila
        Clear-Folder $Target
        # The now-empty folder, if nothing is standing in it. Leaving an empty
        # directory behind is not worth failing an uninstall over.
        Remove-Item $Target -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $Target) {
            Write-Step "emptied $Target (the folder itself is in use, so it stays)"
        } else {
            Write-Step "removed $Target"
        }
    }
    Write-Host ""
    Write-Host "Done. Your projects were not touched:" -ForegroundColor Green
    Write-Host "  $(Join-Path $env:LOCALAPPDATA 'Manila')   (projects)"
    Write-Host "  $(Join-Path $env:APPDATA 'Manila')   (the list of them)"
    return
}

# --- find a Python that will actually do -------------------------------------

Write-Host "Installing $AppName..." -ForegroundColor Cyan

# Which python.exe is real cannot be settled by looking at its path. Windows
# ships a placeholder called python.exe that exits cleanly and prints an advert
# for the Store -- but installing Python *from* the Store then puts a genuine
# interpreter at that same kind of path. So each candidate is asked its version
# and the first to answer with an actual version number wins. Probing uses the
# console build deliberately: pythonw.exe has no stdout to answer on.
$candidates = @()
$candidates += (Get-Command 'python.exe' -All -ErrorAction SilentlyContinue |
                ForEach-Object { $_.Source })
# The py launcher knows about installs that never went on PATH.
if (Get-Command 'py.exe' -ErrorAction SilentlyContinue) {
    $viaLauncher = & py.exe -3 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $viaLauncher) { $candidates += $viaLauncher.Trim() }
}
$candidates += (Get-ChildItem -ErrorAction SilentlyContinue -Path @(
                    "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                    "$env:ProgramFiles\Python3*\python.exe",
                    "C:\Python3*\python.exe") | ForEach-Object { $_.FullName })

$python = $null
$version = $null
foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
    $answer = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $answer) { continue }
    $answer = ("$($answer | Select-Object -Last 1)").Trim()
    if ($answer -notmatch '^\d+\.\d+$') { continue }        # the placeholder's advert
    $parts = $answer.Split('.')
    if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)) {
        $python = $candidate
        $version = $answer
        break
    }
    Write-Step "skipping Python $answer at $candidate (Manila needs 3.11+)"
}

if (-not $python) {
    Write-Host ""
    Write-Host "No usable Python found." -ForegroundColor Red
    Write-Host "Manila needs Python 3.11 or newer. Either:"
    Write-Host "  - install it from python.org, ticking 'Add python.exe to PATH', or"
    Write-Host "  - run 'python' in a terminal and install it from the Store when offered."
    Write-Host "Then run this script again."
    Write-Host ""
    Write-Host "(A bare python.exe on your PATH is not proof: Windows ships a"
    Write-Host " placeholder by that name which only advertises the Store.)"
    exit 1
}

# The windowless twin is what the shortcut runs, so there is no console behind
# the app. If it is somehow missing, fall back rather than fail the install.
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Step "using Python $version ($pythonw)"

# --- copy it into place ------------------------------------------------------

if ((Resolve-Path $Source).Path -eq $Target) {
    Write-Step "already installed in place; refreshing the shortcut only"
} else {
    Stop-InstalledManila
    Clear-Folder $Target
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    foreach ($item in @('manila', 'windows', 'README.md')) {
        $from = Join-Path $Source $item
        if (Test-Path $from) { Copy-Item $from -Destination $Target -Recurse -Force }
    }
    # Bytecode from another machine (or another Python) is only confusing.
    Get-ChildItem $Target -Recurse -Directory -Filter '__pycache__' |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Step "copied to $Target"
}

# --- the shortcut ------------------------------------------------------------

$icon    = Join-Path $Target 'windows\manila.ico'
$launch  = Join-Path $Target 'windows\manila.pyw'
$shell   = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param($Path)
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath       = $pythonw
    $link.Arguments        = '"{0}" --port {1}' -f $launch, $Port
    $link.WorkingDirectory = $Target
    $link.Description      = 'Manila - a simple action item tracking tool'
    if (Test-Path $icon) { $link.IconLocation = $icon }
    $link.Save()
    Write-Step "shortcut: $Path"
}

New-Shortcut (Join-Path $StartMenu "$AppName.lnk")
if ($Desktop) { New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk") }

Write-Host ""
Write-Host "Installed." -ForegroundColor Green
Write-Host "  Press Start and type 'Manila', or pin it to the taskbar."
Write-Host "  It opens http://127.0.0.1:$Port in your browser."
Write-Host ""
Write-Host "  While it runs there is a Manila icon in the notification area:"
Write-Host "    double-click it to open the window again,"
Write-Host "    right-click it to stop Manila."
Write-Host "  Closing the browser tab does not stop it -- closing a tab never"
Write-Host "  stops a server. The icon is how you stop it."
Write-Host ""
Write-Host "  Projects live in $(Join-Path $env:LOCALAPPDATA 'Manila')"
Write-Host ""
Write-Host "  To remove:  powershell -ExecutionPolicy Bypass -File `"$Target\windows\install.ps1`" -Uninstall"
