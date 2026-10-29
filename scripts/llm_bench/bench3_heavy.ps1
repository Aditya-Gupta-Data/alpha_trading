# MANUAL OFFLINE TOOL — local-LLM benchmark, test 3 of 4 (2026-10-29). Not on any cron.
# WRITTEN BUT NEVER RUN as of 2026-10-29 — untested. Expect to debug it on first use.
# 31 points: kpi extraction (with an invented-number trap), hedged/negated headlines, numeric
# reasoning, paraphrase consistency, multi-edge extraction with verbatim quotes, 5-headline batch,
# needle-in-a-haystack at ~1,200 and ~3,000 words (num_ctx 8192).
# Usage:  powershell -ExecutionPolicy Bypass -File .\bench3_heavy.ps1 qwen3:8b
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
$Sys = 'Turn the headline into ONE JSON object with keys target_entity, event_classification, directional_bias (-1.0 bearish .. 1.0 bullish, for the target_entity), horizon_impact (SHORT|MEDIUM|LONG), confidence_score (0..1). If the headline has no market-relevant event, set confidence_score to 0. Output JSON only.'
function Near($a, $b) { try { [math]::Abs([double]$a - $b) -le [math]::Abs($b) * 0.005 } catch { $false } }
function J($t) { $t | ConvertFrom-Json }
function HasNum($t, $v) {
  foreach ($m in [regex]::Matches(($t -replace ',', ''), '-?\d+(\.\d+)?')) {
    if ([math]::Abs([double]$m.Value - $v) -le [math]::Abs($v) * 0.01) { return $true }
  }
  return $false
}
$Tests = New-Object System.Collections.ArrayList
function T($cat, $sys, $user, $fmt, $check, $src = $null, $max = 1) {
  [void]$Tests.Add(@{ Cat = $cat; Sys = $sys; User = $user; Fmt = $fmt; Check = $check; Src = $src; Max = $max })
}

# kpi
$kpiFmt = @{ type = "object"; properties = @{
  revenue_crore = @{ type = "number" }; ebitda_margin_pct = @{ type = "number" }
  new_guidance_low_pct = @{ type = "number" }; new_guidance_high_pct = @{ type = "number" }
  capex_crore = @{ type = "number" }; margin_guidance_given = @{ type = "boolean" } }
  required = @("revenue_crore","ebitda_margin_pct","new_guidance_low_pct","new_guidance_high_pct","capex_crore","margin_guidance_given") }
$kpiText = 'In Q2 we delivered revenue of Rs 5,400 crore, up 11% year on year. EBITDA margin stood at 18.2%, down 90 basis points on commodity costs. We are lowering our full-year revenue growth guidance to 9-10% from the earlier 12%. Management expects margins to recover in the second half but declined to give a number. Capex for the year is Rs 650 crore.'
T 'kpi' 'Extract from the excerpt using only what the text says. new_guidance_* is the NEW revenue growth guidance range. margin_guidance_given is true only if management gave a numeric margin outlook. JSON only.' $kpiText $kpiFmt {
  param($t, $s) $d = J $t; $p = 0
  if (Near $d.revenue_crore 5400) { $p++ }
  if (Near $d.ebitda_margin_pct 18.2) { $p++ }
  if (Near $d.new_guidance_low_pct 9) { $p++ }
  if (Near $d.new_guidance_high_pct 10) { $p++ }
  if (Near $d.capex_crore 650) { $p++ }
  if ($d.margin_guidance_given -eq $false) { $p++ }
  $p } $null 6

# hedge
function Hd($h, $ent, $sign) {
  T 'hedge' $Sys $h $Schema ({ param($t, $s) $d = J $t; ("$($d.target_entity)".ToLower() -match $ent) -and ((([double]$d.directional_bias) * $sign) -gt 0) }.GetNewClosure())
}
Hd 'RBI does not expect inflation to stay elevated, keeps door open for rate cuts' 'rbi|nifty|bank|market' 1
Hd 'Tata Steel profit falls less than feared, shares jump 5%' 'tata' 1
Hd 'No respite for IT stocks as US recession fears deepen' 'it|nifty|tcs|infosys' -1
Hd 'Adani Enterprises rejects short-seller claims; no evidence of wrongdoing found in SEBI probe' 'adani' 1
Hd 'Crude slides 3% as OPEC fails to agree on output cut' 'crude|oil|brent' -1
Hd 'Reliance Retail loss narrows to Rs 120 crore from Rs 340 crore' 'reliance' 1

# numeric
$ns = 'Answer with the final number and one short line of working.'
T 'numeric' $ns 'Revenue grew from Rs 800 crore to Rs 920 crore. What is the percentage growth?' $null { param($t, $s) HasNum $t 15 }
T 'numeric' $ns 'Net profit was Rs 120 crore on revenue of Rs 1,500 crore. What is the net margin in percent?' $null { param($t, $s) HasNum $t 8 }
T 'numeric' $ns 'A company has 40 crore shares and EPS of Rs 12.5. What is the net profit in crore rupees?' $null { param($t, $s) HasNum $t 500 }

# paraphrase
$pp = { param($t, $s) $d = J $t; ("$($d.target_entity)".ToLower() -match 'crude|oil|brent|opec') -and ([double]$d.directional_bias -gt 0) }
T 'paraphrase' $Sys 'Crude jumps 5% on OPEC output cut' $Schema $pp
T 'paraphrase' $Sys 'OPEC trims output; crude oil up 5 percent' $Schema $pp
T 'paraphrase' $Sys 'Oil prices surge five per cent as OPEC reduces supply' $Schema $pp

# edges
$preds = @("supplies_to","owns","regulates","competes_with","partners_with","invests_in")
$edgeFmt = @{ type = "object"; properties = @{ edges = @{ type = "array"; items = @{ type = "object"; properties = @{
  subject = @{ type = "string" }; predicate = @{ type = "string"; enum = $preds }; object = @{ type = "string" }; quote = @{ type = "string" } }
  required = @("subject","predicate","object","quote") } } }; required = @("edges") }
$para = 'Maruti Suzuki sources most of its steel from Tata Steel under a long-term contract. Tata Steel is regulated by SEBI as a listed entity, and Tata Sons owns 33% of Tata Steel. Analysts note that Hyundai competes with Maruti Suzuki in compact cars.'
T 'edges' 'List every relationship stated in the text as an edge. predicate must be from the allowed list. quote must be copied character for character from the text. JSON only.' $para $edgeFmt {
  param($t, $s) $d = J $t; $p = 0
  $gold = @( @('tata steel','supplies_to','maruti'), @('tata sons','owns','tata steel'), @('hyundai','competes_with','maruti') )
  foreach ($g in $gold) {
    foreach ($e in @($d.edges)) {
      if (("$($e.subject)".ToLower().Contains($g[0])) -and ($e.predicate -eq $g[1]) -and ("$($e.object)".ToLower().Contains($g[2]))) { $p++; break }
    }
  }
  $ok = (@($d.edges).Count -gt 0)
  foreach ($e in @($d.edges)) { if (("$($e.quote)".Length -lt 10) -or (-not $s.Contains("$($e.quote)"))) { $ok = $false } }
  if ($ok) { $p++ }
  $p } $para 4

# batch
$batchFmt = @{ type = "object"; properties = @{ items = @{ type = "array"; items = @{ type = "object"; properties = @{
  target_entity = @{ type = "string" }; directional_bias = @{ type = "number" } }; required = @("target_entity","directional_bias") } } }; required = @("items") }
$heads = @('Nifty crashes 3 percent as global selloff deepens','Crude oil jumps 4 percent on supply disruption fears','Infosys wins 1.5 billion dollar deal from European bank','HDFC Bank profit falls 12 percent, misses estimates','Gold hits record high as safe-haven demand surges')
$numbered = (0..4 | ForEach-Object { "$($_ + 1). $($heads[$_])" }) -join "`n"
T 'batch' 'For EACH numbered headline, in the same order, return target_entity and directional_bias (-1 bearish to 1 bullish). Return exactly one item per headline. JSON only.' $numbered $batchFmt {
  param($t, $s) $d = J $t; $gold = @(-1, 1, 1, -1, 1); $items = @($d.items); $p = 0
  for ($i = 0; $i -lt [math]::Min(5, $items.Count); $i++) { if (([double]$items[$i].directional_bias * $gold[$i]) -gt 0) { $p++ } }
  $p } $null 5

# needle
function Haystack($words, $depth) {
  $rand = New-Object System.Random 42
  $sectors = @('banking','auto','pharma','metal','FMCG','realty','energy','IT')
  $sb = New-Object System.Text.StringBuilder
  $needle = 'The board of Kesar Industries approved a share buyback at Rs 412 per share.'
  $pos = [int]($words * $depth / 100); $wc = 0; $done = $false; $i = 0
  while ($wc -lt $words) {
    if ((-not $done) -and ($wc -ge $pos)) { [void]$sb.Append($needle + ' '); $done = $true }
    $i++
    if ($i % 12 -eq 0) {
      $price = if (($i / 12) % 2 -eq 0) { 389 } else { 455 }
      $sent = "Meridian Textiles approved a share buyback at Rs $price per share. "
    } else {
      $s = $sectors[$rand.Next($sectors.Count)]; $dir = @('higher','lower')[$rand.Next(2)]
      $sent = "The $s index closed $($rand.Next(10,400)) points $dir on heavy volumes, while advances and declines were split $($rand.Next(2,9)) to 1. "
    }
    [void]$sb.Append($sent); $wc += 22
  }
  $sb.ToString()
}
$nChk = { param($t, $s) ($t -replace ',', '') -match '\b412\b' }
foreach ($cfg in @(@(1200,10), @(1200,80), @(3000,10), @(3000,80))) {
  $doc = Haystack $cfg[0] $cfg[1]
  T 'needle' 'Answer using only the document. Reply with the number only.' ("Document:`n" + $doc + "`n`nQuestion: At what price per share did Kesar Industries approve its buyback?") $null $nChk
}

function Chat($model, $system, $user, $fmt) {
  $body = @{
    model = $model; stream = $false; think = $false
    messages = @(@{ role = "system"; content = "$system" }, @{ role = "user"; content = "$user" })
    options = @{ temperature = 0; num_predict = 400; num_ctx = 8192 }
  }
  if ($fmt) { $body.format = $fmt }
  $json = $body | ConvertTo-Json -Depth 12
  $r = Invoke-RestMethod -Uri $Url -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType "application/json" -TimeoutSec 1800
  $r.message.content
}

$Cats = @('kpi','hedge','numeric','paraphrase','edges','batch','needle')
foreach ($m in $Models) {
  try { [void](Chat $m "x" "hi" $null) }
  catch { Write-Host ("{0,-16} SKIPPED ({1})" -f $m, $_.Exception.Message); continue }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $score = @{}; $max = @{}; foreach ($k in $Cats) { $score[$k] = 0; $max[$k] = 0 }
  foreach ($c in $Tests) {
    $max[$c.Cat] += $c.Max
    Write-Host ("  {0} {1}" -f $m, $c.Cat)
    try {
      $out = Chat $m $c.Sys $c.User $c.Fmt
      $score[$c.Cat] += [int](& $c.Check $out $c.Src)
    } catch { }
  }
  $line = ($Cats | ForEach-Object { "{0} {1}/{2}" -f $_, $score[$_], $max[$_] }) -join "  "
  $total = ($Cats | ForEach-Object { $score[$_] } | Measure-Object -Sum).Sum
  Write-Host ("{0,-16} TOTAL {1}/31  {2}  ({3:N0} min)" -f $m, $total, $line, $sw.Elapsed.TotalMinutes)
}
