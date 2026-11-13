# Publishes a release and uploads the downloadable apps to it.
#
# GitHub Actions builds these automatically on a tag, but Actions costs
# minutes on a private repository and is free only on public ones. This
# script does the same job from your own machine, using binaries you have
# already built, so a billing problem or a private repo does not stop you
# shipping.
#
#   .\tools\publish_release.ps1 -Version v0.1.0
#   .\tools\publish_release.ps1 -Version v0.1.1 -Draft
#
# Needs $env:FREEHOLD_GITHUB_TOKEN with the 'repo' scope.

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Name = "Freehold",
    [switch]$Draft,
    [switch]$Prerelease,
    [string]$Notes = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$token = $env:FREEHOLD_GITHUB_TOKEN
if (-not $token) {
    Write-Error "Set FREEHOLD_GITHUB_TOKEN first (classic PAT, 'repo' scope)."
    exit 1
}

$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "freehold-release"
}

$me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers
$owner = $me.login
Write-Host "authenticated as $owner"

# ── what is there to upload? ───────────────────────────────────────────
$assets = @()
$exe = Join-Path $root "dist\Freehold.exe"
if (Test-Path $exe) {
    $assets += @{ path = $exe; name = "Freehold.exe" }
} else {
    Write-Host "no dist\Freehold.exe - build it first with:"
    Write-Host "    pyinstaller packaging/Freehold.spec --noconfirm"
}

$apk = Get-ChildItem -Path (Join-Path $root "android\app\build\outputs\apk") `
    -Filter *.apk -Recurse -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($apk) {
    $assets += @{ path = $apk.FullName; name = "Freehold-android.apk" }
} else {
    Write-Host "no APK found - build it with: cd android; gradle assembleDebug"
}

if ($assets.Count -eq 0) {
    Write-Error "nothing to upload. Build at least one app first."
    exit 1
}

# ── release notes ──────────────────────────────────────────────────────
if (-not $Notes) {
    $Notes = @"
## Downloads

| File | Platform | Notes |
|---|---|---|
| ``Freehold.exe`` | Windows | Run it. No Python, no installer. |
| ``Freehold-android.apk`` | Android | Sideload; Android will warn about an unknown developer. |

**Neither file contains any credentials.** On first run Freehold generates
**your** encryption key on **your** machine and shows you where it is. Copy
that file somewhere safe — it is the only thing that can read your backups,
and nobody can recover it for you.

To use GitHub or S3 storage you supply your own token. It stays on your
device and is only ever sent to that provider.

Windows will show "Windows protected your PC" because the file is not
code-signed. Choose More info, then Run anyway.
"@
}

# ── create or reuse the release ────────────────────────────────────────
$release = $null
try {
    $release = Invoke-RestMethod -Headers $headers `
        -Uri "https://api.github.com/repos/$owner/$Name/releases/tags/$Version"
    Write-Host "reusing existing release $Version"
} catch {
    $body = @{
        tag_name   = $Version
        name       = $Version
        body       = $Notes
        draft      = [bool]$Draft
        prerelease = [bool]$Prerelease
    } | ConvertTo-Json
    $release = Invoke-RestMethod -Method Post -Headers $headers `
        -Uri "https://api.github.com/repos/$owner/$Name/releases" `
        -Body $body -ContentType "application/json"
    Write-Host "created release $Version"
}

# ── upload, replacing anything already there with the same name ────────
foreach ($asset in $assets) {
    $existing = $release.assets | Where-Object { $_.name -eq $asset.name }
    if ($existing) {
        Invoke-RestMethod -Method Delete -Headers $headers `
            -Uri "https://api.github.com/repos/$owner/$Name/releases/assets/$($existing.id)" | Out-Null
        Write-Host "replaced $($asset.name)"
    }
    $size = (Get-Item $asset.path).Length
    Write-Host ("uploading {0} ({1:N1} MB)..." -f $asset.name, ($size / 1MB))
    $uploadUrl = "https://uploads.github.com/repos/$owner/$Name/releases/$($release.id)/assets?name=$($asset.name)"
    $up = $headers.Clone()
    $up["Content-Type"] = "application/octet-stream"
    Invoke-RestMethod -Method Post -Uri $uploadUrl -Headers $up `
        -InFile $asset.path | Out-Null
}

Write-Host ""
Write-Host "done: https://github.com/$owner/$Name/releases/tag/$Version"
if ($Draft) {
    Write-Host "This is a DRAFT. Publish it on that page when you are ready."
}
$repo = Invoke-RestMethod -Headers $headers `
    -Uri "https://api.github.com/repos/$owner/$Name"
if ($repo.private) {
    Write-Host ""
    Write-Host "NOTE: the repository is private, so only you can download" `
        "these. Make it public to share them:"
    Write-Host "    .\tools\publish_repo.ps1 -GoPublic"
}
