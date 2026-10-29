# Development only: make statelife-dropshare:// links open the client run from this checkout
# (FR-SHARE-07). The installer registers the scheme for real in M13; this does the same for the
# current Windows user only (HKCU, no admin rights), pointing at `uv run` in this repository.
#
#   pwsh scripts/dev/register_link_scheme.ps1             # register
#   pwsh scripts/dev/register_link_scheme.ps1 -Remove     # remove again
#   pwsh scripts/dev/register_link_scheme.ps1 -ClientProfile b  # a second client (--profile, OQ-13)

param(
    [switch]$Remove,
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$ClientProfile
)

$ErrorActionPreference = 'Stop'
$key = 'HKCU:\Software\Classes\statelife-dropshare'

if ($Remove) {
    if (Test-Path $key) { Remove-Item -Path $key -Recurse }
    Write-Output 'Removed the statelife-dropshare:// link scheme.'
    return
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$uv = (Get-Command uv -ErrorAction Stop).Source
$profileArgument = if ($ClientProfile) { " --profile $ClientProfile" } else { '' }
# "%1" is the clicked link; the client checks it strictly and ignores anything else.
$command = "`"$uv`" run --project `"$repo`" --package dropshare-client python -m dropshare_client$profileArgument `"%1`""

New-Item -Path $key -Force | Out-Null
Set-ItemProperty -Path $key -Name '(default)' -Value 'URL:StateLife DropShare link'
Set-ItemProperty -Path $key -Name 'URL Protocol' -Value ''
New-Item -Path "$key\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "$key\shell\open\command" -Name '(default)' -Value $command
Write-Output "Registered statelife-dropshare:// for this user: $command"
