# ALTDATA_PLAYBOOK.md — finding leading indicators in Indian public data

*Lead Architect directive, 2026-11-03. Standing playbook for every new alt-data
scraper. Built on `src/ingestion/alt_data/` (`BaseAltDataScraper`, cron #39).*

## Ground rules (apply to every pillar)

1. **Capture-only.** A scraper writes rows to `data/lake/alt_data/<dataset>.csv`
   and nothing else (outage lines go to `logs/alt_data.jsonl`). Nothing on the
   trade path imports it.
2. **No execution authority.** An alt-data series becomes an input to sizing
   or entry only after a Dept 5 statistical test passes, forward-tested from
   its own date (Rule 5). A good chart is not a passed test.
3. **Abstain, never invent.** A missing month is a missing row, never a
   carried-forward or interpolated value (Rule 7).
4. **Record the publication date, not just the period.** A series published
   on the 10th describes last month but was only *knowable* on the 10th.
   Every row carries both, or the backtest will use data before it was public.
5. **Public, lawful sources only.** Official portals, published releases and
   public pages. No login walls, no CAPTCHA bypass, no ToS-forbidden scraping;
   throttle hard (government portals rate-limit and ban IPs).
6. **Placement.** Scraping should move to the Mini PC (architect intent
   2026-11-03, not done yet) so the VM's IP is never at risk. Today cron #39 is
   in `setup_cron.sh` and is replicated to the Mini PC; avoid adding a third
   node.
7. **Every new scraper:** subclass `BaseAltDataScraper`, hermetic fixture
   tests in `tests/`, a `MODULES.md` row in the same commit.

## Pillar 1 — Reverse-engineer the P&L

For a sector, list the **top 3 COGS drivers and top 3 revenue drivers**, then
find a public registry or price series that moves *before* the company reports.

| Sector | Driver | Public proxy |
|---|---|---|
| Paints | Crude-linked raw materials (monomers, solvents), TiO2 | Brent (already in the macro lake), TiO2 import prices, USD/INR |
| Autos | Unit volume | Vahan registrations (**live**, `vahan_registrations.csv`) |
| Capital goods / infra | Order inflow | GeM tender awards (**live**, `gem_tenders.csv`), Parivesh clearances |
| Shipping / exporters | Freight cost | Container and dry-bulk freight indices |

**Architect note (pushback):** the directive's example, "Baltic Dry Index for
paint margins", is the wrong mapping. The BDI prices dry-bulk carriers (iron
ore, coal, grain). Paint margins track crude derivatives and TiO2. Use the BDI
for metals, coal and grain-linked names. Every mapping row needs a stated
causal path before a scraper is built.

## Pillar 2 — India's DPI exhaust (mandatory government filings)

Data the state *requires* firms to file, published in aggregate:

- **GSTN E-Way Bills** — freight velocity. Aggregate monthly counts are
  published by GSTN / the Finance Ministry. Per-taxpayer data is *not* public;
  do not try to get it.
- **Parivesh** (MoEFCC) — environmental and forest clearances, a lead on
  CapEx cycles in cement, metals, power and mining, by project and proponent.
- **RERA** (state portals) — new project registrations, a lead on real-estate
  demand. Each state has its own portal: one parser per state, start with
  Maharashtra.
- **EPFO** — net new payroll subscribers (monthly release), a formal
  employment proxy. Released with a lag, and the figures are revised.

## Pillar 3 — Intermediary and API layer

Digital proxies for demand:

- App store download and rank velocity (consumer platforms, brokers, quick
  commerce).
- Airfare levels on business routes (corporate travel demand).
- IT hiring and bench signals (job-posting counts by employer).

**Caution:** most of these sit on private platforms. Check terms of service
before building; prefer official APIs or published aggregates. Where only a
paid feed is lawful, it becomes a budget decision, not a scraper.

## Pillar 4 — Semantic exhaust (count, don't score)

Read earnings-call transcripts and annual reports and **count fixed friction
nouns** ("inventory buildup", "freight delays", "channel destocking", "price
cuts", "capacity utilisation") per document. Report counts and the change
quarter on quarter. **No subjective sentiment score.**

- A fixed, versioned vocabulary file; a change to the list is a new series.
- Plain string or regex counting where it is enough. The LLM is used only to
  normalise phrasing variants into the fixed list, and every LLM hit must cite
  the exact source sentence (drop hits that do not, as the annual-report
  analyzer does).
- **Where it runs:** the job runs on the **Mini PC**. The HP laptop
  (`qwen2.5:7b` under Ollama) may serve only as the *inference backend*, with
  the FreeLLMAPI router as fallback (decision #173: no cron, no database, no
  book on the laptop). That backend is not wired as of 2026-11-03.

## Upcoming targets (build order)

1. **GSTN E-Way Bills** (Pillar 2). Monthly aggregate counts and value, by
   state where published. First step: confirm the exact public source and its
   release lag before writing a parser.
2. **Parivesh CapEx clearances** (Pillar 2). Project name, proponent, sector,
   state, capacity and cost where stated, clearance date. Map proponents to
   NSE tickers with an explicit map file; leave a proponent unmapped rather
   than guess the ticker.

After these: RERA (Maharashtra), EPFO payroll, then Pillar 4 counting on the
darling list's transcripts. `feature/altdata-expansion` (4 scrapers, in
progress as of 2026-11-03) gets reviewed against these ground rules.
