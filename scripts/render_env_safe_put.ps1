<#
.SYNOPSIS
  Safely update Render service env vars without wiping secrets.

.DESCRIPTION
  Render's PUT /env-vars replaces the ENTIRE list. A short body drops keys
  (GEMINI_API_KEY, FACT_MEMORY_MASTER_KEY, DATABASE_URL, …).

  This helper NEVER does a blind full PUT. It:
    1) GET ?limit=100 (paginated) current env vars
    2) Merges your patch (single key or JSON object of key=value)
    3) Refuses if the result would drop any protected key that was present
    4) Only then PUT the merged list

  Prefer single-key updates via -Key/-Value when possible.

.EXAMPLE
  $env:RENDER_API_KEY = '…'
  .\scripts\render_env_safe_put.ps1 -ServiceId srv-… -Key SOURCE_FILE_RETENTION -Value s3

.EXAMPLE
  .\scripts\render_env_safe_put.ps1 -ServiceId srv-… -PatchJson '{"SOURCE_FILE_RETENTION":"s3"}'
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ServiceId,
  [string]$Key,
  [string]$Value,
  [string]$PatchJson,
  [string]$ApiKey = $env:RENDER_API_KEY,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not $ApiKey) {
  throw 'RENDER_API_KEY is required (env or -ApiKey).'
}

$ProtectedKeys = @(
  'GEMINI_API_KEY',
  'FACT_MEMORY_MASTER_KEY',
  'DATABASE_URL',
  'REDIS_URL',
  'SESSION_SECRET',
  'S3_ACCESS_KEY_ID',
  'S3_SECRET_ACCESS_KEY',
  'WORKER_SERVICE_TOKEN',
  'ANALYSIS_API_KEYS'
)

$headers = @{
  Authorization = "Bearer $ApiKey"
  Accept        = 'application/json'
  'Content-Type' = 'application/json'
}

function Get-AllEnvVars([string]$sid) {
  $all = @()
  $cursor = $null
  do {
    $url = "https://api.render.com/v1/services/$sid/env-vars?limit=100"
    if ($cursor) { $url += "&cursor=$([uri]::EscapeDataString($cursor))" }
    $resp = Invoke-RestMethod -Uri $url -Headers $headers -Method Get
    $rows = @($resp)
    $cursor = $null
    foreach ($row in $rows) {
      $ev = if ($row.envVar) { $row.envVar } else { $row }
      if ($ev.key) {
        $all += [pscustomobject]@{ key = [string]$ev.key; value = [string]$ev.value }
      }
      if ($row.cursor) { $cursor = [string]$row.cursor }
    }
    if ($rows.Count -gt 0 -and $rows[-1].cursor) { $cursor = [string]$rows[-1].cursor }
    if ($rows.Count -lt 100) { $cursor = $null }
  } while ($cursor)
  return $all
}

$patch = @{}
if ($Key) {
  if ($null -eq $Value) { throw '-Value is required with -Key.' }
  $patch[$Key] = $Value
}
elseif ($PatchJson) {
  $obj = $PatchJson | ConvertFrom-Json
  foreach ($p in $obj.PSObject.Properties) { $patch[$p.Name] = [string]$p.Value }
}
else {
  throw 'Provide -Key/-Value or -PatchJson.'
}

$current = @(Get-AllEnvVars $ServiceId)
if ($current.Count -eq 0) {
  throw "GET returned 0 env vars for $ServiceId - refusing PUT (would wipe). Check service id / pagination."
}

$beforeKeys = @($current | ForEach-Object { $_.key } | Sort-Object -Unique)
Write-Host ("Loaded {0} env keys from {1}" -f $beforeKeys.Count, $ServiceId)

$merged = @{}
foreach ($ev in $current) { $merged[$ev.key] = $ev.value }
foreach ($k in $patch.Keys) { $merged[$k] = $patch[$k] }

$afterKeys = @($merged.Keys | Sort-Object)
$droppedProtected = @()
foreach ($pk in $ProtectedKeys) {
  if ($beforeKeys -contains $pk -and -not ($afterKeys -contains $pk)) {
    $droppedProtected += $pk
  }
}
if ($droppedProtected.Count -gt 0) {
  throw ("Refusing PUT: would drop protected keys: {0}" -f ($droppedProtected -join ', '))
}

# Also refuse massive shrinkage (accidental short body).
if ($afterKeys.Count -lt [Math]::Max(5, [int]($beforeKeys.Count * 0.5))) {
  throw ("Refusing PUT: key count {0} -> {1} looks like a wipe." -f $beforeKeys.Count, $afterKeys.Count)
}

$body = @()
foreach ($k in ($merged.Keys | Sort-Object)) {
  $body += @{ key = $k; value = $merged[$k] }
}

Write-Host ("Merge patch keys: {0}" -f (($patch.Keys | Sort-Object) -join ', '))
Write-Host ("Result key count: {0} (was {1})" -f $afterKeys.Count, $beforeKeys.Count)
# Never print values.
Write-Host 'Protected key presence after merge:'
foreach ($pk in $ProtectedKeys) {
  $present = $afterKeys -contains $pk
  Write-Host ("  {0}: {1}" -f $pk, $(if ($present) { 'PRESENT' } else { 'absent' }))
}

if ($DryRun) {
  Write-Host 'DryRun — no PUT sent.'
  return
}

$json = $body | ConvertTo-Json -Depth 5 -Compress
# ConvertTo-Json may wrap a single object; API wants an array.
if ($body.Count -eq 1) { $json = "[$json]" }

Invoke-RestMethod -Uri "https://api.render.com/v1/services/$ServiceId/env-vars" `
  -Headers $headers -Method Put -Body $json | Out-Null
Write-Host 'PUT ok (merged). Re-verify with GET limit=100 before assuming success.'
