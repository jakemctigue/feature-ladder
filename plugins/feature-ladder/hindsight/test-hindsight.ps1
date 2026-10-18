# test-hindsight.ps1 - feature-ladder Hindsight self-test
# Does what the plugin does: POST to the Hindsight MCP endpoint with a Bearer key
# and X-Bank-Id header, then initialize -> tools/list -> sync_retain -> recall.
# The API key is read from your environment (or ~/.hindsight/openhands.json as a
# fallback) and is never printed.
#
# Run:  powershell -ExecutionPolicy Bypass -File test-hindsight.ps1

$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

function Get-Var($n) {
  $v = [Environment]::GetEnvironmentVariable($n, 'Process')
  if (-not $v) { $v = [Environment]::GetEnvironmentVariable($n, 'User') }
  return $v
}

$key = Get-Var 'HINDSIGHT_API_KEY'; $keySource = 'HINDSIGHT_API_KEY env var'
if (-not $key) {
  $f = Join-Path $env:USERPROFILE '.hindsight\openhands.json'
  if (Test-Path $f) { $key = (Get-Content $f -Raw | ConvertFrom-Json).hindsightApiToken; $keySource = 'openhands.json FALLBACK (plugin will NOT see this key)' }
}
if (-not $key) { 'RESULT: FAIL - no API key found in env or openhands.json'; exit 1 }
$bank = Get-Var 'HINDSIGHT_BANK_ID'; if (-not $bank) { $bank = 'feature-ladder' }
$url  = Get-Var 'HINDSIGHT_MCP_URL'; if (-not $url) { $url = 'https://api.hindsight.vectorize.io/mcp/' }
"KEY SOURCE: $keySource"
"TARGET:     $url   bank=$bank"

$script:sid = $null
$script:rid = 0
function Invoke-Mcp([string]$method, $params, [switch]$Notify) {
  $h = @{ 'Authorization' = "Bearer $key"; 'X-Bank-Id' = $bank; 'Accept' = 'application/json, text/event-stream' }
  if ($script:sid) { $h['Mcp-Session-Id'] = $script:sid }
  $body = [ordered]@{ jsonrpc = '2.0'; method = $method }
  if ($null -ne $params) { $body.params = $params }
  if (-not $Notify) { $script:rid++; $body.id = $script:rid }
  $json = $body | ConvertTo-Json -Depth 20 -Compress
  try {
    $r = Invoke-WebRequest -Uri $url -Method Post -Headers $h -ContentType 'application/json' `
         -Body ([Text.Encoding]::UTF8.GetBytes($json)) -UseBasicParsing -TimeoutSec 180
  } catch {
    $code = $null; try { $code = [int]$_.Exception.Response.StatusCode } catch {}
    throw "HTTP error on $method (status $code): $($_.Exception.Message)"
  }
  if ($r.Headers.ContainsKey('Mcp-Session-Id')) { $script:sid = @($r.Headers['Mcp-Session-Id'])[0] }
  $c = $r.Content; if ($c -is [byte[]]) { $c = [Text.Encoding]::UTF8.GetString($c) }
  if ($Notify -or -not $c) { return $null }
  $data = @(($c -split "`n") | Where-Object { $_ -like 'data:*' })
  if ($data.Count -gt 0) { $c = $data[-1].Substring(5).Trim() }
  $o = $c | ConvertFrom-Json
  if ($o.error) { throw "JSON-RPC error on ${method}: $($o.error.message)" }
  return $o.result
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$tag = 'project:feature-ladder-selftest'
try {
  $init = Invoke-Mcp 'initialize' @{ protocolVersion = '2025-06-18'; capabilities = @{}; clientInfo = @{ name = 'feature-ladder-selftest'; version = '1.0' } }
  "STEP initialize:   OK  server=$($init.serverInfo.name) $($init.serverInfo.version)  session=$(if ($script:sid) {'yes'} else {'stateless'})"
  try { Invoke-Mcp 'notifications/initialized' $null -Notify | Out-Null } catch {}

  $names = @((Invoke-Mcp 'tools/list' @{}).tools | ForEach-Object { $_.name })
  $hasSync = $names -contains 'sync_retain'
  "STEP tools/list:   OK  $($names.Count) tools  retain=$($names -contains 'retain') sync_retain=$hasSync recall=$($names -contains 'recall') reflect=$($names -contains 'reflect')"

  $content = "feature-ladder self-test $stamp`: the feature-ladder Claude Code plugin successfully wrote this memory to Hindsight bank '$bank'."
  $args1 = @{ content = $content; context = 'feature-ladder'; tags = @($tag, 'test'); metadata = @{ source = 'feature-ladder'; rung = 'selftest'; stamp = $stamp } }
  $retTool = $(if ($hasSync) { 'sync_retain' } else { 'retain' })
  $ret = Invoke-Mcp 'tools/call' @{ name = $retTool; arguments = $args1 }
  $retText = (@($ret.content) | ForEach-Object { $_.text }) -join ' '
  "STEP ${retTool}: " + $(if ($ret.isError) { "FAIL  $retText" } else { 'OK' })
  if ($ret.isError) { 'RESULT: FAIL at retain'; exit 1 }

  $found = $false; $recText = ''
  for ($i = 1; $i -le $(if ($hasSync) { 1 } else { 4 }); $i++) {
    if (-not $hasSync) { Start-Sleep -Seconds 15 }
    $rec = Invoke-Mcp 'tools/call' @{ name = 'recall'; arguments = @{ query = "feature-ladder self-test $stamp"; tags = @($tag); tags_match = 'any'; budget = 'low'; max_tokens = 1000 } }
    $recText = (@($rec.content) | ForEach-Object { $_.text }) -join "`n"
    if ($rec.isError) { "STEP recall:       FAIL  $recText"; 'RESULT: FAIL at recall'; exit 1 }
    if ($recText -match [regex]::Escape($stamp) -or $recText -match 'self-test') { $found = $true; break }
  }
  "STEP recall:       " + $(if ($found) { 'OK  self-test memory came back' } else { 'NOT FOUND' })
  "RECALL EXCERPT:    " + $recText.Substring(0, [Math]::Min(400, $recText.Length)).Replace("`n", ' ')
  'RESULT: ' + $(if ($found) { 'PASS' } else { 'FAIL - retain worked but recall did not return the memory' })
} catch {
  "RESULT: FAIL - $($_.Exception.Message)"; exit 1
}
