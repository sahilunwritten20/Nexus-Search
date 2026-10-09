# WP12-B9: release packaging. The review zip shipped untracked junk
# (nexus_search_frontier.db, __pycache__, .git internals) because it was
# built by zipping the working tree. Release artifacts come from
# `git archive` ONLY: tracked content, one clean zip, no local state.
# Usage:  powershell -File scripts/package.ps1 [-OutFile path.zip]
param(
    [string]$OutFile = ""
)
$ErrorActionPreference = "Stop"
# $PSScriptRoot = <repo>\scripts -> one level up is the repo root
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo
$rev = (git rev-parse --short HEAD).Trim()
if (-not $OutFile) { $OutFile = "nexus-search-$rev.zip" }
git archive --format=zip --prefix="nexus-search/" -o $OutFile HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
Write-Output "wrote $OutFile from HEAD $rev (tracked files only)"
