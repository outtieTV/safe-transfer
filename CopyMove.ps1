<#
.SYNOPSIS
    Robust copy / move script with logging, pre‑flight checks,
    large‑first ordering, optional shutdown, **pause / resume**.

.PARAMETER Source
    Path to the folder (or file) that you want to copy / move.

.PARAMETER Destination
    Path to the target folder.

.PARAMETER Move
    Switch – if present the operation is a *move* (copy → verify → delete).

.PARAMETER Resume
    Switch – resume a previously paused operation.

.PARAMETER LogFile
    Path to a text file where the operation history will be appended.
    Default: "$env:USERPROFILE\copy_move_history.txt".

.PARAMETER StateFile
    Path to the JSON file that stores pause‑resume state.
    Default: "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\.pauseResumeState.json".
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Destination,
    [switch]$Move,
    [switch]$Resume,
    [string]$LogFile = "$env:USERPROFILE\copy_move_history.txt",
    [string]$StateFile = "$(Split-Path -Parent $MyInvocation.MyCommand.Path)\.pauseResumeState.json"
)

# --------------------------------------------------------------------
# Helper – write a line to the history log
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts`t$Message" | Out-File -FilePath $LogFile -Encoding UTF8 -Append
}

# --------------------------------------------------------------------
# Helper – load / save pause‑resume state
function Load-State {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json }
        catch { return $null }
    }
    return $null
}
function Save-State($obj) {
    $obj | ConvertTo-Json -Depth 3 | Set-Content -Path $StateFile -Encoding UTF8
}

# --------------------------------------------------------------------
# 0️⃣  If user asks to resume, make sure a state file exists
if ($Resume) {
    $saved = Load-State
    if (-not $saved) {
        Write-Error "No pause‑resume state found. Run the script normally first."
        exit 1
    }
    # Overwrite parameters with what was stored originally (prevents mismatches)
    $Source = $saved.Source
    $Destination = $saved.Destination
    $Move = $saved.Move
    Write-Host "Resuming previous operation…"
}
else {
    # Fresh start – create a fresh state object (will be saved later)
    $saved = [pscustomobject]@{
        Source      = $Source
        Destination = $Destination
        Move        = $Move.IsPresent
        Paused      = $false
        Index       = 0   # zero‑based index into the sorted file list
    }
    # Remove any stale state file from a previous run
    if (Test-Path $StateFile) { Remove-Item $StateFile -Force }
}

# --------------------------------------------------------------------
# 1️⃣  Validate inputs (same as before)
if (-not (Test-Path $Source)) {
    Write-Error "Source path does not exist."
    exit 1
}
if (-not (Test-Path $Destination)) {
    try { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }
    catch {
        Write-Error "Could not create destination folder."
        exit 1
    }
}

# --------------------------------------------------------------------
# 2️⃣  Gather file list (recursively) and compute total size
$allFiles = Get-ChildItem -Path $Source -Recurse -File
if ($allFiles.Count -eq 0) {
    Write-Error "No files found in source."
    exit 1
}
$totalBytes = ($allFiles | Measure-Object Length -Sum).Sum

# --------------------------------------------------------------------
# 3️⃣  Check free space on destination drive
$destDrive = (Get-PSDrive -PSProvider FileSystem |
                Where-Object { $Destination -like "$($_.Root)*" })[0]
$freeBytes = $destDrive.Free
if ($totalBytes -gt $freeBytes) {
    Write-Error ("Not enough free space on destination.`n" +
                "Needed: {0:N2} GB, Available: {1:N2} GB" -f
                ($totalBytes/1GB), ($freeBytes/1GB))
    Write-Log "FAILED – insufficient space (need $($totalBytes)B, have $freeBytes"B)"
    exit 1
}

# --------------------------------------------------------------------
# 4️⃣  Estimate time & warn if >= 60 min
$speedBps = 50 * 1MB          # 50 MiB/s – adjust if you know your hardware better
$estSec = $totalBytes / $speedBps
if ($estSec -ge 3600) {
    $mins = [math]::Round($estSec/60)
    $ans = Read-Host "Estimated run‑time ≈ $mins min. Continue? (y/n)"
    if ($ans -ne 'y') {
        Write-Host "Operation cancelled by user."
        Write-Log "CANCELLED – user aborted after time warning."
        exit 0
    }
}

# --------------------------------------------------------------------
# 5️⃣  Recreate directory hierarchy in destination (once)
Get-ChildItem -Path $Source -Recurse -Directory |
    ForEach-Object {
        $targetDir = $_.FullName.Replace($Source, $Destination)
        if (-not (Test-Path $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
    }

# --------------------------------------------------------------------
# 6️⃣  Sort files largest → smallest
$sortedFiles = $allFiles | Sort-Object Length -Descending

# --------------------------------------------------------------------
# 7️⃣  Main copy loop – respects pause / resume index
$bytesCopied = 0
$failed = $false

# start index either from 0 (fresh) or from saved state
$startIdx = $saved.Index

for ($i = $startIdx; $i -lt $sortedFiles.Count; $i++) {
    $f = $sortedFiles[$i]
    $targetPath = $f.FullName.Replace($Source, $Destination)

    try {
        Copy-Item -Path $f.FullName -Destination $targetPath -Force -ErrorAction Stop

        # verify size
        if ($f.Length -ne (Get-Item $targetPath).Length) {
            throw "Size mismatch after copy."
        }

        $bytesCopied += $f.Length
    }
    catch {
        Write-Warning "Failed on $($f.FullName): $_"
        Write-Log "FAILED – $($_.Exception.Message) on $($f.FullName)"
        $failed = $true
        break
    }

    # --------------------------------------------------------------
    # Pause check – after each successful file we look at the state file
    $saved.Index = $i + 1   # next file to process
    Save-State $saved

    if ($saved.Paused) {
        Write-Host "`n=== PAUSED ===`nOperation halted after completing $($i+1) of $($sortedFiles.Count) files."
        Write-Log "PAUSED at index $($i+1) ($bytesCopied bytes transferred)."
        # exit with a special code so a wrapper can detect it if desired
        exit 999
    }
}

# --------------------------------------------------------------------
# 8️⃣  If move && everything succeeded → delete source
if ($Move -and -not $failed) {
    try {
        # delete files that were successfully copied
        $sortedFiles[0..($saved.Index-1)] | Remove-Item -Force
        # delete any empty directories (bottom‑up)
        Get-ChildItem -Path $Source -Recurse -Directory |
            Sort-Object FullName -Descending |
            Remove-Item -Force -Recurse
        Write-Host "Move completed – source deleted."
        Write-Log ("SUCCESS – MOVE completed. $bytesCopied bytes transferred.")
    }
    catch {
        Write-Warning "Source deletion failed: $_"
        Write-Log "PARTIAL – move succeeded but source delete failed."
    }
}
else {
    Write-Host "Copy completed."
    Write-Log ("SUCCESS – COPY completed. $bytesCopied bytes transferred.")
}

# --------------------------------------------------------------------
# 9️⃣  Cleanup state file (operation finished)
if (Test-Path $StateFile) { Remove-Item $StateFile -Force }

# --------------------------------------------------------------------
# 10️⃣  Ask about shutdown
$shutAns = Read-Host "Shutdown computer now? (y/n)"
if ($shutAns -eq 'y') {
    Write-Host "Shutting down..."
    Write-Log "User elected shutdown – system powering off."
    shutdown /s /t 0
}
else {
    Write-Host "Shutdown skipped."
    Write-Log "User skipped shutdown."
}
