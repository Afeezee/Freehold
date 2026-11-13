# Creates the GitHub repository and pushes this project to it.
#
# Read this before running it. It does four things, all to YOUR account:
#   1. creates a repository called Freehold -- PRIVATE unless you say
#      otherwise, because going public is easy to undo badly
#   2. sets its description, homepage and topics
#   3. adds it as the 'origin' remote here
#   4. pushes the main branch
#
# Needs a classic personal access token with the 'repo' scope in
# $env:FREEHOLD_GITHUB_TOKEN  (the same one the backup backend uses).
#
#   .\tools\publish_repo.ps1                 # private (the default)
#   .\tools\publish_repo.ps1 -Public         # public, asks to confirm
#   .\tools\publish_repo.ps1 -Name MyBackup  # a different repo name
#
# To open it up later, once you are happy with it:
#   .\tools\publish_repo.ps1 -GoPublic

param(
    [string]$Name = "Freehold",
    [switch]$Public,
    [switch]$GoPublic,
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

# -GoPublic: flip an existing repository, after one last look
if ($GoPublic) {
    Write-Host ""
    Write-Host "About to make https://github.com/$owner/$Name visible to everyone."
    Write-Host "Worth checking first:"
    Write-Host "  git ls-files | Select-String 'key|manifest|\.log'    # nothing private tracked"
    Write-Host "  git log --oneline                                    # commit messages read OK"
    Write-Host ""
    $answer = Read-Host "Type the repository name to confirm"
    if ($answer -ne $Name) { Write-Host "cancelled"; exit 1 }
    Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$Name" `
        -Method Patch -Headers $headers `
        -Body (@{ private = $false } | ConvertTo-Json) `
        -ContentType "application/json" | Out-Null
    Write-Host "now public: https://github.com/$owner/$Name"
    exit 0
}

$isPrivate = -not $Public
if ($Public) {
    $answer = Read-Host "Create $owner/$Name as PUBLIC? Type yes to confirm"
    if ($answer -ne "yes") { Write-Host "cancelled"; exit 1 }
}

# A last look at what would be published
$risky = git ls-files | Select-String -Pattern '(^|/)(key\.json|manifest\.db|agent\.json)$|\.log$'
if ($risky) {
    Write-Host "refusing to push: these tracked files look private:" -ForegroundColor Red
    $risky | ForEach-Object { Write-Host "  $_" }
    exit 1
}

# 1. create the repository
$body = @{
    name              = $Name
    description       = $Description
    private           = $isPrivate
    has_issues        = $true
    has_wiki          = $false
    has_projects      = $false
    auto_init         = $false
    homepage          = "https://github.com/$owner/$Name"
} | ConvertTo-Json

try {
    $repo = Invoke-RestMethod -Uri "https://api.github.com/user/repos" `
        -Method Post -Headers $headers -Body $body -ContentType "application/json"
    Write-Host "created $($repo.full_name) ($(if ($isPrivate) {'private'} else {'public'}))"
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
if ($isPrivate) {
    Write-Host "It is PRIVATE. Only you can see it."
    Write-Host "When you are ready to open it up:  .\tools\publish_repo.ps1 -GoPublic"
} else {
    Write-Host "It is PUBLIC."
}
Write-Host "Licence: AGPL-3.0. GitHub should show it on the repository page."
