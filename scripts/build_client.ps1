# Build the Windows client installer (FR-UPD-01, FR-UPD-02, packaging.md). Windows only.
#
#   pwsh scripts/build_client.ps1 -ServerUrl https://dropshare.example.com `
#        -Publisher "StateLife Test" -CertThumbprint <SHA-1 thumbprint> -TimestampUrl <url> `
#        [-AlsoTrust <certificate SHA-256>, ...]
#   pwsh scripts/build_client.ps1 -ServerUrl https://dropshare.example.com -Publisher x -Unsigned
#
# Steps: build_info.json and icon -> freeze (PyInstaller) -> sign DropShare.exe -> installer
# (Inno Setup) -> sign it -> verify the signature, certificate and version -> SHA-256. The
# certificate is taken from the current user's certificate store by thumbprint; no key is ever
# in the repo (OQ-6). Clients pin its SHA-256 (in build_info.json), plus any -AlsoTrust ones
# while a renewed certificate is rolled out. Signatures are timestamped, so they stay valid
# after the certificate expires. -Unsigned builds are for local testing only: they pin no
# certificate so never install updates, and `dropshare-server releases publish` refuses them.
#
# Needs: uv, Inno Setup 6 (iscc.exe), and for signed builds signtool.exe (Windows SDK).

param(
    [Parameter(Mandatory)][ValidatePattern('^https://')][string]$ServerUrl,
    # The certificate's subject name (CN), as clients and the release CLI will expect it.
    [Parameter(Mandatory)][string]$Publisher,
    [string]$CertThumbprint,
    [string]$TimestampUrl,
    [string[]]$AlsoTrust = @(),
    [switch]$Unsigned
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'The client can only be built on Windows (PyInstaller and Inno Setup are Windows-only).'
}
if (-not $Unsigned -and -not ($CertThumbprint -and $TimestampUrl)) {
    throw 'Pass -CertThumbprint and -TimestampUrl to sign, or -Unsigned for a local test build.'
}
$pinned = @()
if (-not $Unsigned) {
    $certificate = Get-Item "Cert:\CurrentUser\My\$CertThumbprint" -ErrorAction Stop
    $signingSha256 = $certificate.GetCertHashString(
        [System.Security.Cryptography.HashAlgorithmName]::SHA256).ToLowerInvariant()
    $pinned = @($signingSha256) + @($AlsoTrust | ForEach-Object { ($_ -replace '[\s:]', '').ToLowerInvariant() })
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$build = Join-Path $repo 'build\client'
$dist = Join-Path $repo 'dist'
$pyproject = Get-Content (Join-Path $repo 'client\pyproject.toml') -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"(\d+\.\d+\.\d+)"') {
    throw 'No MAJOR.MINOR.PATCH version in client/pyproject.toml (OQ-17).'
}
$version = $Matches[1]
Write-Output "Building StateLife DropShare $version for $ServerUrl"

function Find-Tool([string]$name, [string[]]$candidates) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($candidate in $candidates) {
        $match = Get-Item $candidate -ErrorAction SilentlyContinue | Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($match) { return $match.FullName }
    }
    throw "$name not found. Install it or add it to PATH."
}

function Invoke-Checked([string]$tool, [string[]]$arguments) {
    & $tool @arguments
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path $tool -Leaf) failed (exit $LASTEXITCODE)." }
}

function Set-Signature([string]$path) {
    if ($Unsigned) { return }
    $arguments = @('sign', '/sha1', $CertThumbprint, '/fd', 'sha256', '/s', 'My',
        '/tr', $TimestampUrl, '/td', 'sha256')
    Invoke-Checked $script:signtool ($arguments + $path)
}

$iscc = Find-Tool 'iscc.exe' @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
if (-not $Unsigned) {
    $signtool = Find-Tool 'signtool.exe' @("${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe")
}

# 1. What the build bakes in (FR-CLIENT-05, FR-UPD-03), and the icon.
Remove-Item $build, (Join-Path $dist 'DropShare') -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $build | Out-Null
@{ server_url = $ServerUrl; publisher = $Publisher; cert_sha256 = @($pinned) } | ConvertTo-Json |
    Set-Content -Encoding utf8NoBOM (Join-Path $build 'build_info.json')
Invoke-Checked 'uv' @('run', '--project', $repo, 'python', (Join-Path $repo 'scripts\packaging\make_icon.py'), (Join-Path $build 'DropShare.ico'))

# 2. Freeze, one folder.
$env:DROPSHARE_VERSION = $version
$env:DROPSHARE_BUILD_DIR = $build
Invoke-Checked 'uv' @('run', '--project', $repo, 'pyinstaller', '--noconfirm', '--clean',
    '--distpath', $dist, '--workpath', (Join-Path $build 'pyinstaller'),
    (Join-Path $repo 'installer\dropshare.spec'))
$app = Join-Path $dist 'DropShare'

# 3. Sign the executable.
Set-Signature (Join-Path $app 'DropShare.exe')

# 4. Installer.
Invoke-Checked $iscc @("/DAppVersion=$version", "/DSourceDir=$app", "/DOutputDir=$dist",
    "/DIconFile=$(Join-Path $build 'DropShare.ico')", (Join-Path $repo 'installer\dropshare.iss'))
$installer = Join-Path $dist "DropShare-$version-setup.exe"

# 5. Sign the installer, and check what clients will check (FR-UPD-02).
Set-Signature $installer
if (-not $Unsigned) {
    foreach ($file in @((Join-Path $app 'DropShare.exe'), $installer)) {
        $signature = Get-AuthenticodeSignature $file
        $signer = $signature.SignerCertificate.GetNameInfo('SimpleName', $false)
        $signerSha256 = $signature.SignerCertificate.GetCertHashString(
            [System.Security.Cryptography.HashAlgorithmName]::SHA256).ToLowerInvariant()
        if ($signature.Status -ne 'Valid' -or $signer -ne $Publisher -or $signerSha256 -ne $signingSha256) {
            throw "$(Split-Path $file -Leaf): signature $($signature.Status), signed by '$signer' ($signerSha256)."
        }
        if (-not $signature.TimeStamperCertificate) {
            throw "$(Split-Path $file -Leaf) has no timestamp."
        }
    }
    # Clients and the release CLI also require the installer's own version to be the release's.
    # Inno Setup pads the version string with spaces.
    $fileVersion = (Get-Item $installer).VersionInfo.FileVersion.Trim()
    if ($fileVersion -notin @($version, "$version.0")) {
        throw "The installer's version is '$fileVersion', not $version."
    }
}

# 6. Checksum, for `releases publish --sha256` (FR-UPD-05).
$sha256 = (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLowerInvariant()
Set-Content -Encoding ascii "$installer.sha256" "$sha256  $(Split-Path $installer -Leaf)"
Write-Output ''
Write-Output "Installer: $installer"
Write-Output "SHA-256:   $sha256"
if ($Unsigned) {
    Write-Output 'UNSIGNED: for local testing only. It can''t be published, and clients won''t install it as an update.'
} else {
    Write-Output "Signing certificate SHA-256 (the server's RELEASE_CERT_SHA256 must include it): $signingSha256"
    Write-Output 'Publish it from the server with:'
    Write-Output "  dropshare-server releases publish --version $version --channel <stable|beta> --min-supported <x.y.z> --installer <path> --sha256 $sha256 [--notes-file notes.txt]"
}
