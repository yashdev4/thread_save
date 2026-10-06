# Mirror the deployed connector's archive into a local folder (read-only copy).
#
# The deployed server runs on Render and cannot write to this computer. It pushes its
# vault to the GitHub repo in THREADVAULT_GH_REPO; this script clones/pulls that repo.
# Local-connector chats stay in vault_local; deployed-connector chats land here.
#
# Usage:  powershell -File scripts\pull_remote_vault.ps1 [-Dest vault_rich_fixture] [-Every 60]
#   -Every N   keep pulling every N seconds (0 = pull once and exit)
param(
    [string]$Repo = "",
    [string]$Dest = "vault_rich_fixture",
    [int]$Every = 0
)

if (-not $Repo) {
    $Repo = if ($env:THREADVAULT_GH_REPO) { $env:THREADVAULT_GH_REPO } else { "https://github.com/yashdev4/thread_vault.git" }
}

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root $Dest

function Sync-Mirror {
    if (-not (Test-Path (Join-Path $target ".git"))) {
        if ((Test-Path $target) -and (Get-ChildItem $target -Force | Select-Object -First 1)) {
            Write-Error "$target exists and is not a mirror of $Repo; refusing to overwrite it."
            exit 1
        }
        git clone --quiet $Repo $target
    } else {
        # Fast-forward only: the mirror is read-only, local edits are never merged
        git -C $target pull --ff-only --quiet
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "git exited with $LASTEXITCODE (repo private and not signed in, or local edits in the mirror?)"
        return
    }
    $pages = Get-ChildItem $target -Recurse -Filter *_p??.md -File |
        Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
    Write-Host ("{0} mirror up to date: {1} page(s) in {2}" -f (Get-Date -Format HH:mm:ss), $pages.Count, $target)
}

Sync-Mirror
while ($Every -gt 0) {
    Start-Sleep -Seconds $Every
    Sync-Mirror
}
