<#
.SYNOPSIS
    Build the portable Windows bundle that ships to the analyst.

.DESCRIPTION
    Produces a folder the analyst double-clicks. No installer, no admin rights,
    no Python, no internet, no PATH changes, nothing written outside the folder.

    Why embeddable Python rather than PyInstaller: PyInstaller fights with
    native extensions — onnxruntime, pypdfium2 and numpy all ship compiled
    binaries — and when it loses, it fails at runtime on the client's machine
    with an opaque message about a missing hidden import. The embeddable
    distribution is just a directory of files that already work.

    RUN THIS ON WINDOWS. Wheels are platform-specific and a set downloaded on
    Linux will not install on the target. This script refuses to run elsewhere
    rather than producing a bundle that fails on delivery day.

.PARAMETER PythonVersion
    Embeddable Python to bundle. Must be a version with a Windows embeddable
    build and wheels for every dependency.

.PARAMETER NoOcr
    Build the light variant, without the ~130 MB OCR stack. Scanned pages then
    report a clear message instead of being read.

.PARAMETER Zip
    Also produce a .zip for handover.

.EXAMPLE
    .\packaging\build_portable.ps1 -Zip

.EXAMPLE
    .\packaging\build_portable.ps1 -NoOcr -Zip
#>

[CmdletBinding()]
param(
    [string] $PythonVersion = "3.11.9",
    [string] $OutputDir     = "build_portable",
    [switch] $NoOcr,
    [switch] $Zip,
    [switch] $KeepWheels
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # a progress bar per file is slow

# Isolate the bundled interpreter from this machine's Python installation.
#
# Enabling `import site` in the ._pth (which we must do, or nothing imports)
# also re-enables the per-user site-packages directory. The build machine's
# %APPDATA%\Python\Python311\site-packages then lands on the bundle's sys.path,
# pip resolves against packages that will not exist on the client, and — worse
# — the delivered bundle imports whatever the *client* happens to have there.
# The launchers set this too; it matters in both places.
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONPATH       = ""
$env:PYTHONHOME       = ""

# Windows consoles are rarely UTF-8, and the diagnostics print non-ASCII.
$env:PYTHONUTF8       = "1"
$env:PYTHONIOENCODING = "utf-8"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

$script:step = 0
function Write-Step([string] $Message) {
    $script:step++
    Write-Host ""
    Write-Host ("  [{0}] {1}" -f $script:step, $Message) -ForegroundColor Cyan
}
function Write-Detail([string] $Message) { Write-Host "      $Message" -ForegroundColor DarkGray }
function Write-Good([string] $Message)   { Write-Host "      $Message" -ForegroundColor Green }
function Fail([string] $Message) { Write-Host ""; Write-Host "  BUILD FAILED: $Message" -ForegroundColor Red; exit 1 }

if ($env:OS -ne "Windows_NT") {
    Fail "This must run on Windows. Wheels built elsewhere will not install on the target."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Staging  = Join-Path $RepoRoot $OutputDir
$Bundle   = Join-Path $Staging "PDF2CSV"
$Wheels   = Join-Path $Staging "wheels"
$Cache    = Join-Path $Staging "downloads"

$EmbedUrl  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"

Write-Host ""
Write-Host "  PDF2CSV portable build" -ForegroundColor White
Write-Host "  ----------------------" -ForegroundColor DarkGray
Write-Detail "python   $PythonVersion (embeddable, amd64)"
Write-Detail "variant  $(if ($NoOcr) { 'light - no OCR' } else { 'full - includes scanned-document support' })"
Write-Detail "output   $Bundle"

# --------------------------------------------------------------------------
Write-Step "Preparing folders"

if (Test-Path $Bundle) { Remove-Item $Bundle -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Bundle, $Wheels, $Cache | Out-Null
Write-Good "clean staging area ready"

# --------------------------------------------------------------------------
Write-Step "Fetching the embeddable Python runtime"

$EmbedZip = Join-Path $Cache "python-$PythonVersion-embed-amd64.zip"
if (Test-Path $EmbedZip) {
    Write-Detail "already downloaded, reusing"
} else {
    Write-Detail $EmbedUrl
    try { Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip -UseBasicParsing }
    catch { Fail "could not download Python $PythonVersion. Check the version exists and the network is reachable.`n$_" }
}

$PythonDir = Join-Path $Bundle "python"
Expand-Archive -Path $EmbedZip -DestinationPath $PythonDir -Force
$PythonExe = Join-Path $PythonDir "python.exe"
if (-not (Test-Path $PythonExe)) { Fail "the embeddable archive did not contain python.exe" }
Write-Good "runtime unpacked"

# --------------------------------------------------------------------------
Write-Step "Enabling site-packages in the embeddable runtime"

# The embeddable build ships with `import site` commented out and no
# site-packages on the path. Without this edit, pip installs succeed and then
# nothing can be imported — the single most common way this build goes wrong.
$PthFile = Get-ChildItem -Path $PythonDir -Filter "python*._pth" | Select-Object -First 1
if (-not $PthFile) { Fail "no python*._pth found in the embeddable runtime" }

# Entries in a ._pth file resolve relative to the folder holding python.exe,
# NOT to the working directory. The application lives one level up in
# PDF2CSV\app, so it must be written as "..\app" — plain "app" silently points
# at PDF2CSV\python\app, which does not exist, and the bundle then starts and
# immediately fails to import itself.
$pth = Get-Content $PthFile.FullName
$pth = $pth -replace '^\s*#\s*import\s+site\s*$', 'import site'
if ($pth -notcontains "Lib\site-packages") { $pth += "Lib\site-packages" }
if ($pth -notcontains "..\app")            { $pth += "..\app" }
if ($pth -notcontains "import site")       { $pth += "import site" }
Set-Content -Path $PthFile.FullName -Value $pth -Encoding ascii

Write-Good "$($PthFile.Name) now loads site-packages and ..\app"

# --------------------------------------------------------------------------
Write-Step "Installing pip into the runtime"

$GetPip = Join-Path $Cache "get-pip.py"
if (-not (Test-Path $GetPip)) {
    try { Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPip -UseBasicParsing }
    catch { Fail "could not download get-pip.py.`n$_" }
}

& $PythonExe $GetPip --no-warn-script-location --quiet
if ($LASTEXITCODE -ne 0) { Fail "get-pip.py failed" }
Write-Good "pip installed"

# --------------------------------------------------------------------------
Write-Step "Downloading dependency wheels"

# Downloaded here, on Windows, with this exact interpreter. Wheels are
# platform- and version-specific; a set collected anywhere else is a bundle
# that fails on the client's desktop and nowhere before it.
$BaseReq = Join-Path $RepoRoot "requirements.txt"
& $PythonExe -m pip download -r $BaseReq -d $Wheels --quiet
if ($LASTEXITCODE -ne 0) { Fail "could not download the base wheels" }
Write-Good "base dependencies collected"

if (-not $NoOcr) {
    $OcrReq       = Join-Path $RepoRoot "requirements-ocr.txt"
    $OcrNoDepsReq = Join-Path $RepoRoot "requirements-ocr-nodeps.txt"

    & $PythonExe -m pip download -r $OcrReq -d $Wheels --quiet
    if ($LASTEXITCODE -ne 0) { Fail "could not download the OCR wheels" }

    # --no-deps because rapidocr-onnxruntime hard-requires opencv-python, the
    # GUI build, which silently overwrites opencv-python-headless. See the
    # header of requirements-ocr.txt.
    & $PythonExe -m pip download -r $OcrNoDepsReq -d $Wheels --no-deps --quiet
    if ($LASTEXITCODE -ne 0) { Fail "could not download the rapidocr wheel" }
    Write-Good "OCR dependencies collected"
}

# Fetched now, while there is still a network, so the licence audit below can
# run against the offline wheel cache. It is uninstalled again before handover.
& $PythonExe -m pip download pip-licenses -d $Wheels --quiet
if ($LASTEXITCODE -ne 0) { Write-Detail "pip-licenses unavailable; the licence audit will be skipped" }

$wheelCount = (Get-ChildItem $Wheels -File).Count
$wheelSize  = [math]::Round(((Get-ChildItem $Wheels -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Detail "$wheelCount files, $wheelSize MB"

# --------------------------------------------------------------------------
Write-Step "Installing dependencies offline"

# --no-index proves the offline install works now, on this machine, rather
# than discovering on delivery day that something still wanted the network.
& $PythonExe -m pip install --no-index --find-links $Wheels -r $BaseReq --no-warn-script-location --quiet
if ($LASTEXITCODE -ne 0) { Fail "offline install of the base dependencies failed" }

if (-not $NoOcr) {
    & $PythonExe -m pip install --no-index --find-links $Wheels -r (Join-Path $RepoRoot "requirements-ocr.txt") --no-warn-script-location --quiet
    if ($LASTEXITCODE -ne 0) { Fail "offline install of the OCR dependencies failed" }

    & $PythonExe -m pip install --no-index --find-links $Wheels --no-deps -r (Join-Path $RepoRoot "requirements-ocr-nodeps.txt") --no-warn-script-location --quiet
    if ($LASTEXITCODE -ne 0) { Fail "offline install of rapidocr failed" }
}
Write-Good "installed with --no-index (no network was used)"

# --------------------------------------------------------------------------
Write-Step "Verifying the OpenCV variant"

# opencv-python and opencv-python-headless own the same `cv2` module, so
# installing both leaves whichever landed last. The GUI build drags a Qt stack
# onto a locked-down desktop; catch it here rather than on the client's.
if (-not $NoOcr) {
    $guiOpenCv = & $PythonExe -m pip list --format=freeze 2>$null | Select-String -Pattern "^opencv-python=="
    if ($guiOpenCv) {
        Write-Detail "removing the GUI OpenCV build that a dependency pulled in"
        & $PythonExe -m pip uninstall -y opencv-python --quiet | Out-Null
        & $PythonExe -m pip install --no-index --find-links $Wheels --force-reinstall opencv-python-headless --no-warn-script-location --quiet
    }
    $cvSource = & $PythonExe -c "import cv2, sys; sys.stdout.write(cv2.__version__)" 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "cv2 could not be imported after install" }
    Write-Good "cv2 $cvSource (headless)"
} else {
    Write-Detail "skipped - light build has no OpenCV"
}

# --------------------------------------------------------------------------
Write-Step "Copying the application"

$AppDir = Join-Path $Bundle "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item -Path (Join-Path $RepoRoot "src\pdf2csv") -Destination $AppDir -Recurse -Force

# Never ship caches; they bloat the bundle and can hold stale bytecode.
Get-ChildItem -Path $AppDir -Filter "__pycache__" -Recurse -Directory |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$staticFiles = Get-ChildItem -Path (Join-Path $AppDir "pdf2csv\server\static") -File -ErrorAction SilentlyContinue
if (-not $staticFiles) { Fail "the interface files are missing from the copied application" }
Write-Good "application copied ($($staticFiles.Count) interface files)"

# --------------------------------------------------------------------------
Write-Step "Writing launchers and documentation"

New-Item -ItemType Directory -Force -Path (Join-Path $Bundle "output"), (Join-Path $Bundle "logs") | Out-Null

Copy-Item (Join-Path $PSScriptRoot "bundle\Start PDF2CSV.bat")     $Bundle -Force
Copy-Item (Join-Path $PSScriptRoot "bundle\Check installation.bat") $Bundle -Force
Copy-Item (Join-Path $PSScriptRoot "bundle\READ ME FIRST.txt")      $Bundle -Force

$runbook = Join-Path $RepoRoot "docs\RUNBOOK.md"
if (Test-Path $runbook) { Copy-Item $runbook (Join-Path $Bundle "Runbook.md") -Force }
Write-Good "launchers in place"

# --------------------------------------------------------------------------
Write-Step "Recording the exact shipped versions"

# The lock file is the answer to "what was actually delivered?" six months
# later, when the bundle misbehaves and nobody remembers which pandas it had.
& $PythonExe -m pip freeze | Out-File -FilePath (Join-Path $RepoRoot "requirements.lock.txt") -Encoding utf8
Copy-Item (Join-Path $RepoRoot "requirements.lock.txt") (Join-Path $Bundle "installed-packages.txt") -Force

$manifest = [ordered]@{
    built_at       = (Get-Date).ToString("s")
    built_on       = $env:COMPUTERNAME
    python_version = $PythonVersion
    variant        = $(if ($NoOcr) { "light" } else { "full" })
    package_count  = ((& $PythonExe -m pip list --format=freeze) | Measure-Object).Count
}
$manifest | ConvertTo-Json | Out-File (Join-Path $Bundle "build-info.json") -Encoding utf8
Write-Good "requirements.lock.txt and build-info.json written"

# --------------------------------------------------------------------------
Write-Step "Self-test: running the bundle's own diagnostics"

# The build is not finished until the thing it produced actually runs. This
# catches a broken _pth, a missing DLL, and a half-installed dependency —
# every one of which otherwise surfaces first on the client's desktop.
$env:PDF2CSV_HOME = $Bundle
& $PythonExe -m pdf2csv check
if ($LASTEXITCODE -ne 0) { Fail "the bundled application failed its own environment check" }
Remove-Item Env:\PDF2CSV_HOME

# --------------------------------------------------------------------------
Write-Step "Licence audit"

# Never allowed to fail the build. A missing licence report is a note in the
# handover checklist; a build that will not complete because of one is not.
# ErrorActionPreference is relaxed here because a native command writing to
# stderr is a terminating error under "Stop", and pip is chatty.
try {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    & $PythonExe -m pip install --no-index --find-links $Wheels pip-licenses --quiet | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $licences = & $PythonExe -m piplicenses --format=plain --with-urls
        if ($LASTEXITCODE -eq 0 -and $licences) {
            $licences | Out-File (Join-Path $Bundle "licences.txt") -Encoding utf8

            # LGPL is excluded from the match: it is not a problem for dynamic
            # linking and flagging it every build teaches people to skip this.
            $copyleft = $licences | Select-String -Pattern "GPL" |
                        Where-Object { $_ -notmatch "LGPL" }
            if ($copyleft) {
                Write-Host "      REVIEW NEEDED - possible copyleft licences:" -ForegroundColor Yellow
                $copyleft | ForEach-Object { Write-Host "        $_" -ForegroundColor Yellow }
            } else {
                Write-Good "no GPL or AGPL packages in the shipped set"
            }
        }
        # Uninstalled so it does not ship: it is a build tool, not a dependency.
        & $PythonExe -m pip uninstall -y pip-licenses prettytable wcwidth --quiet | Out-Null
    } else {
        Write-Detail "pip-licenses not in the wheel cache; run the audit separately"
    }
} catch {
    Write-Detail "licence audit skipped: $_"
} finally {
    $ErrorActionPreference = $previousPreference
}

# --------------------------------------------------------------------------
Write-Step "Finishing"

if (-not $KeepWheels) {
    Remove-Item $Wheels -Recurse -Force -ErrorAction SilentlyContinue
    Write-Detail "wheel cache removed (pass -KeepWheels to retain it)"
}

$bundleSize = [math]::Round(((Get-ChildItem $Bundle -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)

if ($Zip) {
    $zipPath = Join-Path $Staging "PDF2CSV.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path $Bundle -DestinationPath $zipPath
    $zipSize = [math]::Round(((Get-Item $zipPath).Length / 1MB), 1)
    Write-Good "PDF2CSV.zip ($zipSize MB)"
}

Write-Host ""
Write-Host "  Build complete." -ForegroundColor Green
Write-Host ""
Write-Host "    Folder    $Bundle"
Write-Host "    Size      $bundleSize MB"
Write-Host "    Variant   $(if ($NoOcr) { 'light (no scanned-document support)' } else { 'full' })"
Write-Host ""
Write-Host "  Before handover, on a machine that has never had this project:" -ForegroundColor White
Write-Host "    1. Copy the folder across."
Write-Host "    2. Disable the network adapter."
Write-Host "    3. Double-click 'Start PDF2CSV.bat' and convert a real document."
Write-Host ""
Write-Host "  A cached model or an installed package on this build machine hides" -ForegroundColor DarkGray
Write-Host "  a first-run download perfectly. That test is the only way to catch it." -ForegroundColor DarkGray
Write-Host ""
