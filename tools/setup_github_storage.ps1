# Replaces the broken "Safe" storage with a working GitHub one, and proves
# it works on something small before trusting it with anything.
#
#   $env:FREEHOLD_GITHUB_TOKEN = "ghp_..."
#   .\tools\setup_github_storage.ps1
#
# It will not touch any existing backup data. Removing a storage entry
# forgets where blocks were kept; it does not delete them.

param(
    [string]$Name = "github",
    [string]$Owner = "",
    [string]$Remove = "Safe",
    [string]$TestFolder = ""
)

$ErrorActionPreference = "Stop"
if (-not $env:FREEHOLD_HOME) { $env:FREEHOLD_HOME = "$HOME\.freehold" }

# Freehold already stores tokens for people who do not use a shell, so
# look there too rather than demanding an environment variable.
$token = $env:FREEHOLD_GITHUB_TOKEN
if (-not $token) {
    $credFile = Join-Path $env:FREEHOLD_HOME "credentials.json"
    if (Test-Path $credFile) {
        $saved = (Get-Content -Raw $credFile | ConvertFrom-Json)
        $token = $saved.FREEHOLD_GITHUB_TOKEN
        if ($token) { Write-Host "using the token saved in credentials.json" }
    }
}
if (-not $token) {
    Write-Error ("No GitHub token. Either set FREEHOLD_GITHUB_TOKEN, or " +
        "save one with: python -m freehold.cli credentials set FREEHOLD_GITHUB_TOKEN")
    exit 1
}

# Who does the token actually belong to? Typing the username by hand is
# what broke the last attempt, so do not ask for it.
$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "freehold-setup"
}
if (-not $Owner) {
    try {
        $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers
        $Owner = $me.login
        Write-Host "token belongs to: $Owner"
    } catch {
        Write-Error ("GitHub rejected the token: " + $_.Exception.Message +
            "`nIf your account is restricted, that must be resolved first " +
            "at https://support.github.com - a new token will not help.")
        exit 1
    }
}

Write-Host ""
Write-Host "1. removing the broken entry, if it is there"
python -m freehold.cli backend remove $Remove 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "   (nothing called $Remove; fine)" }

Write-Host ""
Write-Host "2. re-enabling your local storage"
foreach ($b in @("disk", "archive")) {
    python -m freehold.cli backend enable $b 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "   $b resumed" }
}

Write-Host ""
Write-Host "3. adding GitHub storage for $Owner"
Write-Host "   (this is checked against the live API before it is accepted)"
python -m freehold.cli backend add $Name github "owner=$Owner"
if ($LASTEXITCODE -ne 0) {
    Write-Error "GitHub storage was not added, so nothing has changed."
    exit 1
}

Write-Host ""
Write-Host "4. proving it works on something tiny"
$probe = Join-Path $env:TEMP "freehold-probe"
if (Test-Path $probe) { Remove-Item $probe -Recurse -Force }
New-Item -ItemType Directory -Path $probe | Out-Null
"Freehold storage test. Safe to delete." | Out-File (Join-Path $probe "hello.txt") -Encoding utf8

python -m freehold.cli check $probe --backend $Name
if ($LASTEXITCODE -ne 0) {
    Write-Error "the size check refused even this; something is wrong."
    exit 1
}

python -m freehold.cli backup $probe --backend $Name --device "storage-test"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Error ("The test backup failed. If your account is restricted " +
        "from creating repositories, that is the cause and it has to be " +
        "lifted by GitHub support first.")
    exit 1
}

Write-Host ""
Write-Host "5. reading it back to be sure it is really there"
python -m freehold.cli verify --deep
if ($LASTEXITCODE -ne 0) {
    Write-Error "the data could not be read back. Do not trust this yet."
    exit 1
}

Remove-Item $probe -Recurse -Force
Write-Host ""
Write-Host "GitHub storage is working. Check what you have with:"
Write-Host "    python -m freehold.cli backend info $Name"
Write-Host "    python -m freehold.cli doctor"
Write-Host ""
Write-Host "Back up a real folder only after 'freehold check <folder>'" `
    "says the size is acceptable."
