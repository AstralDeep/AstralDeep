[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedBranch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CanonicalExistingDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Container)) {
        throw "$Label is not an existing directory: $LiteralPath"
    }

    $resolved = (Resolve-Path -LiteralPath $LiteralPath).ProviderPath
    $full = [System.IO.Path]::GetFullPath($resolved)
    $volumeRoot = [System.IO.Path]::GetPathRoot($full)
    if ($full -ne $volumeRoot) {
        $separators = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $full = $full.TrimEnd($separators)
    }
    return $full
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Left,

        [Parameter(Mandatory = $true)]
        [string]$Right
    )

    $comparison = if (
        [System.IO.Path]::DirectorySeparatorChar -eq [char]'\'
    ) {
        [System.StringComparison]::OrdinalIgnoreCase
    }
    else {
        [System.StringComparison]::Ordinal
    }
    return [string]::Equals($Left, $Right, $comparison)
}

function Assert-RootIsNotReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Container)) {
        throw "$Label is not an existing directory: $LiteralPath"
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "$Label is a reparse point: $LiteralPath"
    }
}

function Invoke-GitReadOnly {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = @(& git -C $Root @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $detail = ($output | Out-String).Trim()
        if (-not $detail) {
            $detail = "git exited with code $exitCode"
        }
        throw "git $($Arguments -join ' ') failed: $detail"
    }
    return ($output -join "`n").Trim()
}

function Assert-NoReparsePoints {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $rootItem = Get-Item -LiteralPath $Root -Force
    if (
        ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "repository root is a reparse point: $Root"
    }

    # Get-ChildItem does not follow directory links unless -FollowSymlink is
    # requested. It therefore inventories the boundary without traversing it.
    $reparsePoints = @(
        Get-ChildItem -LiteralPath $Root -Force -Recurse -ErrorAction Stop |
            Where-Object {
                ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            }
    )
    if ($reparsePoints.Count -gt 0) {
        $paths = @($reparsePoints | ForEach-Object { $_.FullName } | Sort-Object)
        $shown = @($paths | Select-Object -First 10)
        $suffix = if ($paths.Count -gt $shown.Count) {
            " (and $($paths.Count - $shown.Count) more)"
        }
        else {
            ''
        }
        throw "reparse point(s) found beneath repository root: $($shown -join ', ')$suffix"
    }
}

try {
    Assert-RootIsNotReparsePoint `
        -LiteralPath $RepositoryRoot -Label 'repository root'
    Assert-RootIsNotReparsePoint `
        -LiteralPath $ExpectedRoot -Label 'expected root'

    $requestedRootParameters = @{
        LiteralPath = $RepositoryRoot
        Label = 'repository root'
    }
    $requestedRoot = Get-CanonicalExistingDirectory @requestedRootParameters
    $allowedRootParameters = @{
        LiteralPath = $ExpectedRoot
        Label = 'expected root'
    }
    $allowedRoot = Get-CanonicalExistingDirectory @allowedRootParameters

    if (-not (Test-SamePath -Left $requestedRoot -Right $allowedRoot)) {
        throw "exact-root mismatch: repository root '$requestedRoot' is not expected root '$allowedRoot'"
    }

    Assert-NoReparsePoints -Root $requestedRoot

    $gitRootRaw = Invoke-GitReadOnly -Root $requestedRoot `
        -Arguments @('rev-parse', '--show-toplevel')
    $gitRootParameters = @{
        LiteralPath = $gitRootRaw
        Label = 'Git worktree root'
    }
    $gitRoot = Get-CanonicalExistingDirectory @gitRootParameters
    if (-not (Test-SamePath -Left $gitRoot -Right $allowedRoot)) {
        throw "exact-root mismatch: Git worktree root '$gitRoot' is not expected root '$allowedRoot'"
    }

    $branch = Invoke-GitReadOnly -Root $requestedRoot `
        -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')
    if ($branch -cne $ExpectedBranch) {
        throw "expected-branch mismatch: found '$branch', expected '$ExpectedBranch'"
    }

    & git -C $requestedRoot diff --cached --quiet -- 2>$null
    $indexExitCode = $LASTEXITCODE
    if ($indexExitCode -eq 1) {
        throw 'Git index is not clean; staged changes are present'
    }
    if ($indexExitCode -ne 0) {
        throw "could not verify the Git index (git diff exited $indexExitCode)"
    }

    [pscustomobject]@{
        contract        = 'astral.migration-preflight/074-v1'
        repository_root = $gitRoot
        branch          = $branch
        index_clean     = $true
        reparse_points  = 0
    } | ConvertTo-Json -Compress
}
catch {
    [Console]::Error.WriteLine("preflight_074: $($_.Exception.Message)")
    exit 1
}
