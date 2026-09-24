#!/usr/bin/env pwsh
# Exercise the real updater in isolated fixtures, without selecting a live feature.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('agent-context-' + [guid]::NewGuid())
$shellPath = (Get-Process -Id $PID).Path
$savedFeature = $env:SPECIFY_FEATURE
$savedDirectory = $env:SPECIFY_FEATURE_DIRECTORY
$passed = 0

function Assert-That([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function New-Fixture([string]$Name) {
    $root = Join-Path $fixtureRoot $Name
    $scripts = Join-Path $root '.specify/scripts/powershell'
    $templates = Join-Path $root '.specify/templates'
    $feature = Join-Path $root 'specs/999-context-test'
    New-Item -ItemType Directory -Path $scripts, $templates, $feature -Force | Out-Null
    foreach ($file in @('common.ps1', 'update-agent-context.ps1')) {
        Copy-Item -LiteralPath (Join-Path $repoRoot ".specify/scripts/powershell/$file") -Destination $scripts
    }
    Copy-Item -LiteralPath (Join-Path $repoRoot '.specify/templates/agent-file-template.md') -Destination $templates
    @'
# Fixture plan
**Language/Version**: Python 3.11
**Primary Dependencies**: Example framework
**Storage**: Example database
**Project Type**: service
'@ | Set-Content -LiteralPath (Join-Path $feature 'plan.md') -Encoding utf8
    return $root
}

function Invoke-Updater([string]$Root, [string]$AgentType = '', [int]$ExpectedExit = 0) {
    Push-Location -LiteralPath $Root
    try {
        $arguments = @('-NoProfile', '-File', (Join-Path $Root '.specify/scripts/powershell/update-agent-context.ps1'))
        if ($AgentType) { $arguments += @('-AgentType', $AgentType) }
        $output = (& $shellPath @arguments 2>&1 | Out-String)
        Assert-That ($LASTEXITCODE -eq $ExpectedExit) "Unexpected updater exit $LASTEXITCODE for '$AgentType': $output"
    } finally {
        Pop-Location
    }
}

function Assert-SharedGuide([string]$Root) {
    $guide = Join-Path $Root 'AGENTS.md'
    Assert-That (Test-Path -LiteralPath $guide) 'Shared AGENTS.md was not created'
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $Root 'CLAUDE.md'))) 'Updater recreated CLAUDE.md'
    Assert-That ((Get-Content -LiteralPath $guide -Raw) -match 'Python 3.11') 'Plan data is missing'
}

try {
    $env:SPECIFY_FEATURE = '999-context-test'
    $env:SPECIFY_FEATURE_DIRECTORY = 'specs/999-context-test'

    # Claude and Codex, and every existing AGENTS.md alias, use the same guide.
    foreach ($agent in @('claude', 'codex', 'opencode', 'amp', 'kiro-cli', 'bob', '')) {
        $root = New-Fixture -Name $(if ($agent) { $agent } else { 'default' })
        Invoke-Updater -Root $root -AgentType $agent
        Assert-SharedGuide -Root $root
        $passed++
    }

    # Default routing must update AGENTS.md exactly once while retaining manual rules.
    foreach ($agent in @('claude', '')) {
        $root = New-Fixture -Name ('existing-' + $(if ($agent) { $agent } else { 'default' }))
        $guide = Join-Path $root 'AGENTS.md'
        @'
# Shared guide
Keep this manual rule.
## Active Technologies

- Prior technology

## Recent Changes

- prior: Existing change

## Manual guidance
Keep this trailing rule.
'@ | Set-Content -LiteralPath $guide -Encoding utf8
        Invoke-Updater -Root $root -AgentType $agent
        Assert-SharedGuide -Root $root
        $content = Get-Content -LiteralPath $guide -Raw
        Assert-That ($content.Contains('Keep this manual rule.')) 'Manual rule was lost'
        Assert-That ($content.Contains('Keep this trailing rule.')) 'Trailing rule was lost'
        Assert-That (([regex]::Matches($content, '(?m)^- 999-context-test:')).Count -eq 1) 'Shared guide was updated more than once'
        $passed++
    }

    # Other agent-specific routing still works alone and alongside the shared guide.
    $root = New-Fixture -Name 'gemini'
    Invoke-Updater -Root $root -AgentType 'gemini'
    $gemini = Join-Path $root 'GEMINI.md'
    Assert-That (Test-Path -LiteralPath $gemini) 'Gemini guide was not created'
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $root 'AGENTS.md'))) 'Specific Gemini update wrote the shared guide'
    Invoke-Updater -Root $root -AgentType 'codex'
    Invoke-Updater -Root $root
    Assert-SharedGuide -Root $root
    Assert-That ((Get-Content -LiteralPath $gemini -Raw) -match 'Example framework') 'Default routing missed Gemini'
    $passed++

    # A leftover legacy guide is not modified or selected as a current target.
    $root = New-Fixture -Name 'legacy'
    $legacy = Join-Path $root 'CLAUDE.md'
    Set-Content -LiteralPath $legacy -Value 'Legacy content remains user owned.' -Encoding utf8
    $legacyHash = (Get-FileHash -LiteralPath $legacy).Hash
    Invoke-Updater -Root $root
    Assert-That (Test-Path -LiteralPath (Join-Path $root 'AGENTS.md')) 'Legacy guide prevented shared guide creation'
    Assert-That ((Get-FileHash -LiteralPath $legacy).Hash -eq $legacyHash) 'Legacy guide was modified'
    $passed++

    foreach ($missing in @('specs/999-context-test/plan.md', '.specify/templates/agent-file-template.md')) {
        $root = New-Fixture -Name ('missing-' + [System.IO.Path]::GetFileName($missing))
        Remove-Item -LiteralPath (Join-Path $root $missing)
        Invoke-Updater -Root $root -AgentType 'claude' -ExpectedExit 1
        Assert-That (-not (Test-Path -LiteralPath (Join-Path $root 'AGENTS.md'))) 'Failed validation wrote a guide'
        $passed++
    }
    Write-Host "PASS: $passed agent context routing scenarios"
} finally {
    $env:SPECIFY_FEATURE = $savedFeature
    $env:SPECIFY_FEATURE_DIRECTORY = $savedDirectory
    # Resolve and bound the recursive cleanup to this run's generated temp directory.
    if (Test-Path -LiteralPath $fixtureRoot) {
        $resolvedRoot = (Resolve-Path -LiteralPath $fixtureRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).Path
        Assert-That ((Split-Path -Parent $resolvedRoot) -eq $tempRoot.TrimEnd('\', '/')) 'Fixture cleanup escaped the temp directory'
        Assert-That ((Split-Path -Leaf $resolvedRoot) -like 'agent-context-*') 'Unexpected fixture cleanup target'
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
