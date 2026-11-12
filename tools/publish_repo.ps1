# Creates the GitHub repository and pushes this project to it.
#
# Read this before running it. It does four things, all to YOUR account:
#   1. creates a repository called Freehold (public by default)
#   2. sets its description, homepage and topics
#   3. adds it as the 'origin' remote here
#   4. pushes the main branch
#
# Needs a classic personal access token with the 'repo' scope in
# $env:FREEHOLD_GITHUB_TOKEN  (the same one the backup backend uses).
#
#   .\tools\publish_repo.ps1                 # public
#   .\tools\publish_repo.ps1 -Private        # private instead
#   .\tools\publish_repo.ps1 -Name MyBackup  # a different repo name

param(
    [string]$Name = "Freehold",
    [switch]$Private,
    [string]$Description = "Vendor-agnostic, client-encrypted, deduplicating backup for PC and mobile. Your files are chunked, deduplicated and encrypted before they leave the machine; GitHub, R2, B2 or a disk are interchangeable dumb blob stores underneath."
)

$ErrorActionPreference = "Stop"

$token = $env:FREEHOLD_GITHUB_TOKEN
if (-not $token) {
    Write-Error "Set FREEHOLD_GITHUB_TOKEN first (classic PAT, 'repo' scope)."
    exit 1
}

$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "freehold-publish"
}

# Who are we?
$me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers
$owner = $me.login
Write-Host "authenticated as $owner"

# 1. create the repository
$body = @{
    name              = $Name
    description       = $Description
    private           = [bool]$Private
    has_issues        = $true
    has_wiki          = $false
    has_projects      = $false
    auto_init         = $false
    homepage          = "https://github.com/$owner/$Name"
} | ConvertTo-Json

try {
    $repo = Invoke-RestMethod -Uri "https://api.github.com/user/repos" `
        -Method Post -Headers $headers -Body $body -ContentType "application/json"
    Write-Host "created $($repo.full_name) ($(if ($Private) {'private'} else {'public'}))"
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 422) {
        Write-Host "$owner/$Name already exists; continuing"
        $repo = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$Name" -Headers $headers
    } else {
        throw
    }
}

# 2. topics, so the repository is findable
$topics = @{ names = @("backup", "encryption", "deduplication",
                       "zero-knowledge", "content-defined-chunking",
                       "restore", "python", "self-hosted",
                       "cloudflare-r2", "backblaze-b2") } | ConvertTo-Json
Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$Name/topics" `
    -Method Put -Headers $headers -Body $topics -ContentType "application/json" | Out-Null
Write-Host "topics set"

# 3. remote
$existing = git remote 2>$null
if ($existing -contains "origin") {
    git remote set-url origin "https://github.com/$owner/$Name.git"
    Write-Host "origin updated"
} else {
    git remote add origin "https://github.com/$owner/$Name.git"
    Write-Host "origin added"
}

# 4. push
Write-Host "pushing main..."
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error "push failed. If git asked for credentials, use your GitHub username and paste the token as the password."
    exit 1
}

Write-Host ""
Write-Host "done: https://github.com/$owner/$Name"
Write-Host "Check the licence shows as 'PolyForm Noncommercial 1.0.0' or 'non-OSI'."
