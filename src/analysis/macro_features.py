"""
src/analysis/macro_features.py — THE macro featurizer (spec §2, one code path)
===============================================================================

The single featurizer for the Macro Regime & Pattern Engine
(docs/macro_regime_engine_spec.md). Historical shock fingerprints (episode
trajectories) and tonight's current-state read both come THROUGH THIS FILE —
`trajectory()` and `feature_vector()` share `_vector_at()`, so there is no
train/serve skew by construction (spec §3 step 1).

House laws honored here:
  * NULL-honest (spec law 3): a missing session is a hole, never silently
    interpolated. `align` may forward-fill at most `ffill_limit` consecutive
    sessions (weekend/holiday gaps) and FLAGS every filled cell; every
    feature vector names the series it could not genuinely see at as-of.
  * Abstention beats hallucination (law 1): insufficient history -> None,
    never a guessed z-score; a clash cannot be declared on missing evidence.
  * Pure functions: zero network, zero writes, stdlib only. Reading the lake
    CSVs is the only I/O.

Lake contract (defines what the M1 macro_lake clerk must write):
  data/lake/macro/<KEY>.csv with header `date,value` — ISO date, float value,
  EMPTY value field for a NULL-honest hole. Append-only, one row per session.

The owner's "Dollar-vs-Crude clash" is the versioned definition in
`clash_condition`: corr60(DXY, Brent) < -0.4 AND |zdelta20| > 1 for BOTH.
"""
import csv
import math
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path

# The canonical series roster (spec §1). Friendly keys — the whole firm
# speaks these names; the lake clerk maps sources (FRED ids, NSE archives)
# onto them at write time.
SERIES = ("DXY", "BRENT", "USDINR", "US10Y", "INDIAVIX", "NIFTY")

# The versioned clash definition (spec §2, owner's north-star sentence).
CLASH_CORR_WINDOW = 60
CLASH_Z_WINDOW = 20
CLASH_CORR_FLOOR = -0.4   # corr60 must be strictly BELOW this
CLASH_Z_MIN = 1.0         # |zdelta20| must be strictly ABOVE this, both legs

Z_BASELINE_SESSIONS = 252  # one trading year of % changes backs every z

_NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan"}


def _default_lake_dir():
    return Path(__file__).resolve().parents[2] / "data" / "lake" / "macro"


def read_series(key, lake_dir=None):
    """Read data/lake/macro/<KEY>.csv -> sorted list of (iso_date, float|None).

    Missing file -> honest empty list (the caller sees a hole, not a crash).
    An empty/non-numeric value field is a NULL-honest hole (None). Rows with
    unparseable dates are skipped. Duplicate dates: last row wins
    (append-only clerk re-runs)."""
    lake = Path(lake_dir) if lake_dir is not None else _default_lake_dir()
    path = lake / f"{key}.csv"
    if not path.exists():
        return []
    by_date = {}
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            raw_date = row[0].strip()
            try:
                d = date.fromisoformat(raw_date)
            except ValueError:
                continue  # header or junk row
            raw_val = row[1].strip() if len(row) > 1 else ""
            if raw_val.lower() in _NULL_TOKENS:
                val = None
            else:
                try:
                    val = float(raw_val)
                    if math.isnan(val):
                        val = None
                except ValueError:
                    val = None
            by_date[d.isoformat()] = val
    return sorted(by_date.items())


def align(series_dict, ffill_limit=2):
    """Date-align {name: [(iso_date, val|None), ...]} on the union calendar.

    Returns (dates, matrix, fill_flags):
      dates      — sorted union of all observed dates,
      matrix     — {name: [float|None]} aligned to `dates`,
      fill_flags — {name: [bool]}, True exactly where a cell was forward-
                   filled (never silent — spec law 3).

    A hole (absent date or None value) is forward-filled from the last
    genuine value for at most `ffill_limit` CONSECUTIVE sessions (weekend/
    holiday gaps); beyond the cap the gap stays None until a genuine value
    resets it. Leading holes are never filled."""
    dates = sorted({d for rows in series_dict.values() for d, _ in rows})
    matrix, fill_flags = {}, {}
    for name, rows in series_dict.items():
        lookup = dict(rows)
        vals, flags = [], []
        last, run = None, 0
        for d in dates:
            v = lookup.get(d)
            if v is not None:
                vals.append(v)
                flags.append(False)
                last, run = v, 0
            else:
                run += 1
                if last is not None and run <= ffill_limit:
                    vals.append(last)
                    flags.append(True)
                else:
                    vals.append(None)
                    flags.append(False)
        matrix[name] = vals
        fill_flags[name] = flags
    return dates, matrix, fill_flags


def zdelta(values, window, baseline=Z_BASELINE_SESSIONS):
    """Z-score of the `window`-session % change at the END of `values`,
    versus the trailing `baseline` (252) usable observations of that same
    % change (current included).

    None when: the current change is undefined (holes at either end),
    fewer than `baseline` usable changes exist ("insufficient_history"),
    or the baseline is degenerate (zero std). Never a guess."""
    n = len(values)
    if window <= 0 or n < window + 1:
        return None
    changes = []
    for t in range(window, n):
        v0, v1 = values[t - window], values[t]
        if v0 is None or v1 is None or v0 == 0:
            changes.append(None)
        else:
            changes.append((v1 - v0) / v0)
    current = changes[-1]
    if current is None:
        return None
    usable = [c for c in changes if c is not None]
    if len(usable) < baseline:
        return None
    tail = usable[-baseline:]
    mean = math.fsum(tail) / baseline
    var = math.fsum((c - mean) ** 2 for c in tail) / baseline
    if var == 0.0:
        return None
    return (current - mean) / math.sqrt(var)


def _pearson(xs, ys):
    """Pearson r of two equal-length, hole-free windows. None on zero
    variance (a constant series has no correlation to state)."""
    n = len(xs)
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    cov = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.fsum((x - mx) ** 2 for x in xs)
    vy = math.fsum((y - my) ** 2 for y in ys)
    if vx == 0.0 or vy == 0.0:
        return None
    return cov / math.sqrt(vx * vy)


def _corr_at(a, b, window):
    """Pearson r of the trailing `window` of two aligned value lists.
    None until the window fills or if any hole sits inside it."""
    if len(a) < window or window < 2:
        return None
    wa, wb = a[-window:], b[-window:]
    if any(x is None for x in wa) or any(y is None for y in wb):
        return None
    return _pearson(wa, wb)


def corr_state(a, b, window=60):
    """Rolling Pearson correlation of two aligned value lists.

    Returns a list the same length as the inputs; entry t is the correlation
    over sessions [t-window+1 .. t]. None until the window fills, and None
    for any window containing a hole (holes reduce confidence, they do not
    get papered over)."""
    if len(a) != len(b):
        raise ValueError("corr_state needs date-aligned inputs of equal "
                         f"length (got {len(a)} vs {len(b)}) — align() first")
    return [_corr_at(a[:t + 1], b[:t + 1], window) for t in range(len(a))]


def clash_condition(corr60, z20_a, z20_b):
    """THE versioned clash test (spec §2): corr60 strictly below -0.4 AND
    |z20| strictly above 1 for BOTH legs. Any missing component -> False
    (a clash is never declared on evidence we do not have)."""
    if corr60 is None or z20_a is None or z20_b is None:
        return False
    return (corr60 < CLASH_CORR_FLOOR
            and abs(z20_a) > CLASH_Z_MIN
            and abs(z20_b) > CLASH_Z_MIN)


def _pair_stats(a_rows, b_rows, as_of):
    """(corr60, z20_a, z20_b) for two raw (date, value) series as of a date.
    Pair-aligned on their union calendar — the one canonical definition of
    the pair state, used by clash() and feature_vector() alike."""
    dates, matrix, _ = align({"a": a_rows, "b": b_rows})
    end = bisect_right(dates, as_of)
    if end == 0:
        return None, None, None
    a = matrix["a"][:end]
    b = matrix["b"][:end]
    return (_corr_at(a, b, CLASH_CORR_WINDOW),
            zdelta(a, CLASH_Z_WINDOW),
            zdelta(b, CLASH_Z_WINDOW))


def clash(dxy, brent, as_of):
    """True iff the Dollar-vs-Crude clash condition holds at `as_of`.
    `dxy`/`brent` are raw (iso_date, value) series (read_series shape)."""
    corr, z_a, z_b = _pair_stats(dxy, brent, as_of)
    return clash_condition(corr, z_a, z_b)


def _vector_at(raw, as_of):
    """The single featurizer core. `raw` = {name: read_series rows}.

    Per-series z-scores run on each series' OWN session calendar (a series
    z needs no cross-series alignment); pair stats run on the pair's union
    calendar via _pair_stats. `holes` names every canonical series with no
    genuine (non-None) observation dated exactly `as_of` — missing file,
    stale last row, or a NULL row all count."""
    series_block, holes = {}, []
    for name in SERIES:
        rows = raw.get(name) or []
        vals = [v for d, v in rows if d <= as_of]
        if not any(d == as_of and v is not None for d, v in rows):
            holes.append(name)
        series_block[name] = {"z20": zdelta(vals, 20),
                              "z60": zdelta(vals, 60)}
    corr, z_a, z_b = _pair_stats(raw.get("DXY") or [],
                                 raw.get("BRENT") or [], as_of)
    return {
        "as_of": as_of,
        "series": series_block,
        "pairs": {
            "dxy_brent_corr60": corr,
            "dxy_brent_clash": clash_condition(corr, z_a, z_b),
        },
        "holes": holes,
    }


def _load_lake(lake_dir=None):
    return {name: read_series(name, lake_dir) for name in SERIES}


def feature_vector(as_of_date, lake_dir=None):
    """Today's (or any date's) macro state, straight off the lake:
    {series: {z20, z60}, pairs: {dxy_brent_corr60, dxy_brent_clash},
     holes: [series with no genuine value at as_of]}."""
    return _vector_at(_load_lake(lake_dir), as_of_date)


def trajectory(anchor_date, t_minus=20, t_plus=120, lake_dir=None):
    """Fingerprint matrix for an episode anchor (spec §2): the feature
    vector sampled at every session offset T-t_minus .. T+t_plus.

    The session calendar is the union of all lake dates; an anchor that is
    not itself a session (weekend shock) snaps to the last session <= it.
    Offsets outside available history are honest None rows — the
    fingerprint records what it could NOT see (spec law 3).

    Returns {"anchor": anchor_date, "anchor_session": iso|None,
             "rows": [{"offset": int, "date": iso|None, "vector": dict|None}]}.
    Same featurizer core as feature_vector() — no train/serve skew."""
    raw = _load_lake(lake_dir)
    calendar = sorted({d for rows in raw.values() for d, _ in rows})
    pos = bisect_right(calendar, anchor_date) - 1  # last session <= anchor
    anchor_session = calendar[pos] if pos >= 0 else None
    rows = []
    for offset in range(-t_minus, t_plus + 1):
        idx = pos + offset
        if anchor_session is None or idx < 0 or idx >= len(calendar):
            rows.append({"offset": offset, "date": None, "vector": None})
        else:
            d = calendar[idx]
            rows.append({"offset": offset, "date": d,
                         "vector": _vector_at(raw, d)})
    return {"anchor": anchor_date, "anchor_session": anchor_session,
            "rows": rows}
