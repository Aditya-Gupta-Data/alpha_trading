# MANUAL OFFLINE TOOL — local-LLM benchmark, test 1 of 4 (2026-10-29). Not on any cron.
# Runs on a Windows laptop against a local Ollama (http://localhost:11434).
# 6 synthetic headlines -> strict 5-key JSON (schema-constrained), plus 3 one-word options facts.
# Usage:  powershell -ExecutionPolicy Bypass -File .\bench1_basic.ps1 qwen3:8b qwen2.5:14b
# Write-up and results: docs/local_llm_benchmark_2026-10-29.md
$Models = $args
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
$System = "Turn the headline into ONE JSON object with keys target_entity, event_classification, directional_bias (-1.0 bearish .. 1.0 bullish, for the target_entity), horizon_impact (SHORT|MEDIUM|LONG), confidence_score (0..1). Output JSON only."

$JsonCases = @(
  @{ T = "Crude spikes 4% as OPEC cuts supply"; E = @("crude","oil","brent"); S = 1 },
  @{ T = "HDFC Bank Q2 profit falls 12%, misses estimates on higher provisions"; E = @("hdfc"); S = -1 },
  @{ T = "RBI cuts repo rate by 25 bps in surprise move"; E = @("nifty","bank","rbi","market"); S = 1 },
  @{ T = "Rupee slumps to record low of 89 against the dollar"; E = @("usdinr","inr","rupee"); S = 1 },
  @{ T = "Infosys wins `$1.5 billion multi-year deal from European bank"; E = @("infosys","infy"); S = 1 },
  @{ T = "Nifty falls 2% as FIIs dump Indian equities for fifth straight day"; E = @("nifty"); S = -1 }
)
$FactCases = @(
  @{ Q = "A short iron condor is held to expiry. Where is the maximum loss reached: at the short strikes, or at or beyond the long (wing) strikes? One word: SHORT or WING."; A = "wing" },
  @{ Q = "For an option seller, does a fall in implied volatility after earnings (IV crush) usually help or hurt the seller? One word: HELP or HURT."; A = "help" },
  @{ Q = "Can a European-style index option such as Nifty be exercised early? One word: YES or NO."; A = "no" }
)

function Chat($model, $system, $user, $fmt) {
  $body = @{
    model = $model; stream = $false; think = $false
    messages = @(@{ role = "system"; content = $system }, @{ role = "user"; content = $user })
    options = @{ temperature = 0; num_predict = 300 }
  }
  if ($fmt) { $body.format = $fmt }
  $json = $body | ConvertTo-Json -Depth 10
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $r = Invoke-RestMethod -Uri $Url -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType "application/json" -TimeoutSec 600
  [pscustomobject]@{
    Text = $r.message.content
    Secs = $sw.Elapsed.TotalSeconds
    Tps  = $r.eval_count / [math]::Max($r.eval_duration / 1e9, 1e-9)
  }
}

foreach ($m in $Models) {
  try { [void](Chat $m "x" "hi" $null) }
  catch { Write-Host ("{0,-18} SKIPPED ({1})" -f $m, $_.Exception.Message); continue }
  $okJson = 0; $okEnt = 0; $okSign = 0; $facts = 0; $secs = 0.0; $tps = 0.0
  foreach ($c in $JsonCases) {
    try {
      $o = Chat $m $System $c.T $Schema
      $secs += $o.Secs; $tps += $o.Tps
      $d = $o.Text | ConvertFrom-Json
      $okJson++
      $ent = "$($d.target_entity)".ToLower()
      if (@($c.E | Where-Object { $ent.Contains($_) }).Count -gt 0) { $okEnt++ }
      $b = [double]$d.directional_bias
      if ($b -ne 0 -and (($b -gt 0) -eq ($c.S -gt 0))) { $okSign++ }
    } catch { Write-Host "   ! $m : $($c.T.Substring(0,25))... failed" }
  }
  foreach ($f in $FactCases) {
    try {
      $o = Chat $m "Answer with one word only." $f.Q $null
      $secs += $o.Secs
      if ($o.Text.Trim().ToLower().TrimEnd('.').StartsWith($f.A)) { $facts++ }
    } catch { Write-Host "   ! $m : fact question failed" }
  }
  $n = $JsonCases.Count
  Write-Host ("{0,-18} json {1}/{2}  entity {3}/{2}  bias-sign {4}/{2}  facts {5}/3  ~{6:N1} tok/s  total {7:N0}s" -f $m, $okJson, $n, $okEnt, $okSign, $facts, ($tps / $n), $secs)
}
