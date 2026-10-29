# MANUAL OFFLINE TOOL — local-LLM benchmark, test 4 of 4 (2026-10-29). Not on any cron.
# Runs on a Windows laptop against a local Ollama (http://localhost:11434).
# Fetches live headlines from the same RSS feeds as config/rss_feeds.json (or reuses headlines.txt
# in the current folder), runs each model on the 5-key frame, and prints a table for HUMAN grading.
# There is no answer key in this script; the 2026-10-29 key is in docs/local_llm_benchmark_2026-10-29.md.
# To rerun the 2026-10-29 set:  Copy-Item headlines_2026-10-29.txt headlines.txt
# Usage (same window, scripts allowed once):
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\bench4_real_headlines.ps1 qwen2.5:14b | Tee-Object -Append results4.txt
# Two fixes are baked in (both found on 2026-10-29):
#   1. content = "$user" — Get-Content lines carry hidden PSPath/PSDrive properties, and
#      ConvertTo-Json on Windows PowerShell 5.1 tries to serialise them and effectively hangs.
#   2. a per-headline progress line, so a slow model is not mistaken for a hang.
$Models = $args
$Feeds = @(
  'https://www.moneycontrol.com/rss/marketreports.xml',
  'https://www.moneycontrol.com/rss/business.xml',
  'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms',
  'https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms',
  'https://pulse.zerodha.com/feed.php'
)
$Url = "http://localhost:11434/api/chat"
$Schema = @{
  type = "object"
  properties = @{
    target_entity = @{ type = "string" }
    event_classification = @{ type = "string" }
    directional_bias = @{ type = "number" }
    horizon_impact = @{ type = "string"; enum = @("SHORT","MEDIUM","LONG") }
    confidence_score = @{ type = "number" }
  }
  required = @("target_entity","event_classification","directional_bias","horizon_impact","confidence_score")
}
$Sys = 'Turn the headline into ONE JSON object with keys target_entity, event_classification, directional_bias (-1.0 bearish .. 1.0 bullish, for the target_entity), horizon_impact (SHORT|MEDIUM|LONG), confidence_score (0..1). If the headline has no market-relevant event, set confidence_score to 0. Output JSON only.'

$hfile = Join-Path (Get-Location) 'headlines.txt'
if (Test-Path $hfile) {
  $Heads = @(Get-Content $hfile -Encoding UTF8 | Where-Object { $_.Trim() })
  Write-Output "Using saved headlines.txt ($($Heads.Count) headlines)"
} else {
  $all = New-Object System.Collections.ArrayList
  foreach ($u in $Feeds) {
    try {
      $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 30 -UserAgent 'Mozilla/5.0'
      $text = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()).Trim()
      $x = [xml]$text
      $n = 0
      foreach ($it in $x.SelectNodes('//item')) {
        $t = $it.SelectSingleNode('title')
        if ($t) { [void]$all.Add(($t.InnerText -replace '\s+', ' ').Trim()); $n++ }
      }
      Write-Output "feed ok ($n items): $u"
    } catch { Write-Output "feed FAILED: $u ($($_.Exception.Message))" }
  }
  $Heads = @($all | Where-Object { $_.Length -gt 20 } | Select-Object -Unique | Get-Random -Count 25)
  $Heads | Set-Content $hfile -Encoding UTF8
  Write-Output "Saved $($Heads.Count) headlines to headlines.txt"
}

function Chat($model, $system, $user, $fmt) {
  $body = @{
    model = $model; stream = $false; think = $false
    messages = @(@{ role = "system"; content = "$system" }, @{ role = "user"; content = "$user" })
    options = @{ temperature = 0; num_predict = 250 }
  }
  if ($fmt) { $body.format = $fmt }
  $json = $body | ConvertTo-Json -Depth 10
  $r = Invoke-RestMethod -Uri $Url -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType "application/json" -TimeoutSec 900
  $r.message.content
}

$res = @{}
foreach ($m in $Models) {
  try { [void](Chat $m "x" "hi" $null) } catch { Write-Output "$m SKIPPED ($($_.Exception.Message))"; continue }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  for ($i = 0; $i -lt $Heads.Count; $i++) {
    try {
      Write-Host ("  {0} headline {1}/{2}" -f $m, ($i + 1), $Heads.Count)
      $d = (Chat $m $Sys $Heads[$i] $Schema) | ConvertFrom-Json
      $ent = "$($d.target_entity)"; if ($ent.Length -gt 14) { $ent = $ent.Substring(0, 14) }
      $res["$i|$m"] = ("{0:+0.0;-0.0;0.0}/{1:0.0} {2}" -f [double]$d.directional_bias, [double]$d.confidence_score, $ent)
    } catch { $res["$i|$m"] = "ERR" }
  }
  Write-Output ("{0} done in {1:N0} min" -f $m, $sw.Elapsed.TotalMinutes)
}

Write-Output ""
Write-Output ("Columns: " + ($Models -join ' | ') + "   (format: bias/confidence entity)")
for ($i = 0; $i -lt $Heads.Count; $i++) {
  Write-Output ("{0}. {1}" -f ($i + 1), $Heads[$i])
  Write-Output ("    " + (($Models | ForEach-Object { $res["$i|$_"] }) -join ' | '))
}
