# MANUAL OFFLINE TOOL — laptop side of scripts/backtest_filings.py. Not on any cron; run by hand (or a one-off task).
# Reads items.jsonl + system.txt from -Dir, asks local Ollama (think=false, temp 0), appends {id, raw, secs} to
# <model>.jsonl. Cooling: a wait whenever the ACPI sensor reads >= -HotC until <= -CoolC. Resumable: ids already in the output are skipped. Progress lines go to progress.txt.
param([string]$Dir = "C:\Users\HP\backtest", [string]$Model = "qwen3:8b", [int]$HotC = 84, [int]$CoolC = 76)
Set-Location $Dir
$utf8 = New-Object Text.UTF8Encoding($false)
$system = [IO.File]::ReadAllText("$Dir\system.txt", $utf8)
$items = @(Get-Content "$Dir\items.jsonl" -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
$out = "$Dir\" + ($Model -replace '[:.]', '_') + ".jsonl"
$have = @{}
if (Test-Path $out) { Get-Content $out -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $have[[string]($_ | ConvertFrom-Json).id] = 1 } }
ollama ps | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ -and $_ -ne $Model } | ForEach-Object { ollama stop $_ }
"start $Model $(Get-Date -Format s) todo $($items.Count - $have.Count)" | Out-File "$Dir\progress.txt" -Append
$n = 0
function Get-TempC { try { [math]::Round((Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | Select-Object -First 1).CurrentTemperature / 10 - 273.15, 1) } catch { 0 } }
foreach ($it in $items) {
  if ($have.ContainsKey([string]$it.id)) { continue }
  if ((Get-TempC) -ge $HotC) {
    "$Model too hot $(Get-TempC) C, waiting for $CoolC C at $(Get-Date -Format s)" | Out-File "$Dir\progress.txt" -Append
    while ((Get-TempC) -gt $CoolC) { Start-Sleep -Seconds 20 }
  }
  $body = @{ model = $Model; stream = $false; think = $false; format = "json"; keep_alive = "30m"
    options = @{ temperature = 0; num_predict = 900 }
    messages = @(@{ role = "system"; content = "$system" }, @{ role = "user"; content = "$($it.text)" }) } | ConvertTo-Json -Depth 6
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $raw = ""
  try { $r = Invoke-RestMethod -Uri http://127.0.0.1:11434/api/chat -Method Post -Body ($utf8.GetBytes($body)) -ContentType "application/json; charset=utf-8" -TimeoutSec 900; $raw = $r.message.content } catch { $raw = "" }
  if ($raw -eq "") { "$Model $($it.id) FAILED $(Get-Date -Format s)" | Out-File "$Dir\progress.txt" -Append; continue }
  $line = (@{ id = [string]$it.id; raw = $raw; secs = [math]::Round($sw.Elapsed.TotalSeconds, 1) } | ConvertTo-Json -Compress)
  [IO.File]::AppendAllText($out, $line + "`n", $utf8)
  $n++
  if ($n % 25 -eq 0) { "$Model done $n $(Get-Date -Format s)" | Out-File "$Dir\progress.txt" -Append }
}
"ALLDONE $Model $n $(Get-Date -Format s)" | Out-File "$Dir\progress.txt" -Append
