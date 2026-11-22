"""Tip text → a strict schema and one class. Pure rules, no LLM: the same text always gets the same answer.

Order: UNSAFE (any option selling outside a bull call / bear put debit spread, futures, rollovers) →
a full trade (underlying + strike + CE/PE + entry price) → EXIT_SIGNAL → INCOMPLETE → CHATTER."""
from __future__ import annotations

import re
from datetime import date

from src.tip_desk import (CHATTER, DEFINED_SPREAD, EXIT_SIGNAL, INCOMPLETE, OPTION_BUY, UNSAFE_REJECT)

INDEX_ALIASES = (
    (r"BANK\s*NIFTY|NIFTY\s*BANK", "NIFTY BANK"),
    (r"FIN\s*NIFTY|NIFTY\s*FIN(?:\s*SERVICE)?", "NIFTY FIN SERVICE"),
    (r"MIDCP\s*NIFTY|MIDCAP\s*NIFTY|NIFTY\s*MID(?:\s*SELECT)?", "NIFTY MID SELECT"),
    (r"NIFTY\s*50|NIFTY", "NIFTY 50"),
    (r"SENSEX", "SENSEX"),
    (r"BANKEX", "BANKEX"),
)
NUM = r"\d+(?:\.\d+)?"
LEG_RE = re.compile(rf"\b(\d{{2,6}}(?:\.\d+)?)\s*(CE|PE|CALLS?|PUTS?)\b")
ENTRY_RE = re.compile(rf"(?:@|\bAT\b|\bABOVE\b|\bNEAR\b|\bAROUND\b|\bRANGE\b|\bCMP\b|\bBUY\b|\bBUYING\b)\s*"
                      rf"(?:RS\.?\s*|₹\s*)?({NUM})(?:\s*(?:-|TO|/)\s*({NUM}))?")
SL_RE = re.compile(rf"\b(?:SL|STOP\s*LOSS|STOPLOSS|STOP)\s*[:\-@]?\s*(?:RS\.?\s*)?({NUM})")
TGT_LIST_RE = re.compile(rf"\b(?:TGTS?|TARGETS?|TRGTS?|TP)\s*[:\-@]?\s*((?:{NUM}[\s,/\-&+]*)+)")
TGT_LABEL_RE = re.compile(rf"\bT([1-9])\s*[:\-@]?\s*({NUM})")
UNSAFE_RE = re.compile(r"\b(SELLERS?|WRITERS?|WRITE|WRITING|SHORT(?:ING)?|ROLL\s*OVER|ROLLOVER|ROLL\s*IT|"
                       r"FUT|FUTS|FUTURES?|STRADDLE|STRANGLE)\b")
SELL_RE = re.compile(r"\bSELL(?:ING)?\b")
BUY_RE = re.compile(r"\bBUY(?:ING)?\b")
OPTION_WORD_RE = re.compile(r"\b(CE|PE|CALLS?|PUTS?)\b")
EXIT_RE = re.compile(r"\b(BOOKING\s+NOW|BOOK\s+NOW|BOOK\s+(?:ALL|FULL|PROFITS?)|BOOKED\s+OUT|BOOK\s+OUT|"
                     r"EXIT(?:\s+(?:ALL|NOW|FULL))?|SQUARE\s*OFF|CLOSE\s+(?:ALL|NOW|POSITIONS?)|SL\s+HIT|GET\s+OUT)\b")
INCOMPLETE_RE = re.compile(r"\b(LIMIT\s+ORDERS?|WAITING|WAIT|ENTRY|BUY|BUYING|SL|TGT|TARGETS?|KEEP\s+READY|"
                           r"READY|STRIKE)\b")
SPREAD_WORD_RE = re.compile(r"\b(SPREAD|BULL\s+CALL|BEAR\s+PUT)\b")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                      "NOV", "DEC"), start=1)}
EXPIRY_DATE_RE = re.compile(r"\b(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\b")
EXPIRY_MONTH_RE = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+(?:EXPIRY|MONTHLY|SERIES)\b")
NEAREST_RE = re.compile(r"\b(TODAYS?|TODAY'?S|0\s*DTE|EXPIRY\s+DAY|THIS\s+WEEK|WEEKLY|CURRENT\s+WEEK)\b")
NEXT_RE = re.compile(r"\bNEXT\s+(?:WEEK|EXPIRY)\b")
STOPWORDS = {"BUY", "BUYING", "TODAYS", "TODAY", "TODAY'S", "EXPIRY", "WEEKLY", "INDEX", "OPTION", "OPTIONS",
             "TRADE", "CALL", "STOCK", "STOCKS", "THIS", "WEEK", "NEXT", "MONTHLY", "IN", "OF", "THE", "FOR", "A",
             "AND", "SPREAD", "BULL", "BEAR", "LEG", "LEGS", "MONTH", "SERIES", "0DTE", "ONLY", "NOW"}


def normalize(text: str) -> str:
    t = str(text or "").upper().replace("’", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"[^\w@₹.,:/&+'\-\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _f(v):
    return float(v) if v is not None else None


def _side_for(text: str, start: int, end: int, prev_end: int, next_start: int):
    before = text[prev_end:start]
    sides = [(m.end(), "SELL") for m in SELL_RE.finditer(before)] + [(m.end(), "BUY") for m in BUY_RE.finditer(before)]
    if sides:
        return max(sides)[1]
    after = text[end:next_start]
    sides = [(m.start(), "SELL") for m in SELL_RE.finditer(after)] + [(m.start(), "BUY") for m in BUY_RE.finditer(after)]
    return min(sides)[1] if sides else None


def _legs(text: str) -> list:
    found = list(LEG_RE.finditer(text))
    legs = []
    for i, m in enumerate(found):
        prev_end = found[i - 1].end() if i else 0
        next_start = found[i + 1].start() if i + 1 < len(found) else len(text)
        ot = "CE" if m.group(2).startswith(("CE", "CALL")) else "PE"
        legs.append({"strike": float(m.group(1)), "option_type": ot,
                     "side": _side_for(text, m.start(), m.end(), prev_end, next_start),
                     "start": m.start(), "end": m.end()})
    return legs


def _underlying(text: str, first_leg_start: int):
    head = text[:first_leg_start] if first_leg_start is not None else text
    best = None
    for pattern, canon in INDEX_ALIASES:
        for m in re.finditer(rf"\b(?:{pattern})\b", head):
            if best is None or m.start() > best[0]:
                best = (m.start(), canon)
    if best is not None:
        return best[1], True
    words = [w for w in re.findall(r"[A-Z][A-Z0-9&\-]*", head) if w not in STOPWORDS]
    if not words:
        return None, False
    return " ".join(words[-3:]), False


def _expiry_hint(text: str, today: date = None):
    m = EXPIRY_DATE_RE.search(text)
    if m:
        today = today or date.today()
        day, month = int(m.group(1)), MONTHS[m.group(2)[:3]]
        year = today.year + (1 if month < today.month - 1 else 0)
        try:
            return {"kind": "date", "value": date(year, month, day).isoformat()}
        except ValueError:
            return {"kind": "invalid", "value": m.group(0)}
    m = EXPIRY_MONTH_RE.search(text)
    if m:
        return {"kind": "month", "value": MONTHS[m.group(1)[:3]]}
    if NEXT_RE.search(text):
        return {"kind": "next", "value": None}
    if NEAREST_RE.search(text):
        return {"kind": "nearest", "value": None}
    return {"kind": "nearest", "value": None, "implied": True}


def _entry(text: str, after: int):
    m = ENTRY_RE.search(text, after)
    if not m:
        return None, None
    lo, hi = _f(m.group(1)), _f(m.group(2))
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    return lo, (hi if hi is not None else lo)


def _targets(text: str) -> list:
    labelled = sorted((int(k), float(v)) for k, v in TGT_LABEL_RE.findall(text))
    if labelled:
        return [v for _, v in labelled]
    m = TGT_LIST_RE.search(text)
    return [float(x) for x in re.findall(NUM, m.group(1))] if m else []


def _spread_structure(legs: list):
    """'bull_call_spread' / 'bear_put_spread', or (None, why) for any other two-leg shape."""
    if len(legs) != 2 or {legs[0]["side"], legs[1]["side"]} != {"BUY", "SELL"}:
        return None, "a two-leg tip must be one BUY and one SELL"
    buy = next(l for l in legs if l["side"] == "BUY")
    sell = next(l for l in legs if l["side"] == "SELL")
    if buy["option_type"] != sell["option_type"]:
        return None, "legs of different types (CE + PE) are not a debit spread"
    if buy["option_type"] == "CE" and buy["strike"] < sell["strike"]:
        return "bull_call_spread", None
    if buy["option_type"] == "PE" and buy["strike"] > sell["strike"]:
        return "bear_put_spread", None
    return None, "a credit spread (selling the nearer strike) — only bull call / bear put debit spreads are allowed"


def classify(text: str, today: date = None) -> dict:
    t = normalize(text)
    legs = _legs(t)
    out = {"kind": CHATTER, "reason": None, "text": t, "underlying": None, "underlying_known": False,
           "legs": [], "structure": None, "entry_low": None, "entry_high": None, "sl": None,
           "targets": [], "expiry_hint": None, "levels_only": False}
    has_sell = bool(SELL_RE.search(t))
    out["sl"] = _f(SL_RE.search(t).group(1)) if SL_RE.search(t) else None
    out["targets"] = _targets(t)

    unsafe = UNSAFE_RE.search(t)
    structure = None
    if len(legs) >= 2:
        if all(l["side"] != "SELL" for l in legs) and not has_sell:
            return dict(out, kind=INCOMPLETE, reason="more than one contract and no SELL leg — paste one tip at a time")
        structure, why = _spread_structure(legs)
        if structure is None:
            return dict(out, kind=UNSAFE_REJECT, reason=f"unsafe: {why}")
    elif SPREAD_WORD_RE.search(t) and not unsafe:
        return dict(out, kind=INCOMPLETE, reason="a spread tip needs both legs written out (BUY x CE SELL y CE)")
    if unsafe:
        return dict(out, kind=UNSAFE_REJECT, reason=f"unsafe: '{unsafe.group(0)}' (option selling / futures / rollover)")
    if has_sell and structure is None and (legs or OPTION_WORD_RE.search(t)):
        return dict(out, kind=UNSAFE_REJECT, reason="unsafe: option SELL outside a bull call / bear put debit spread")

    if legs:
        und, known = _underlying(t, legs[0]["start"])
        lo, hi = _entry(t, legs[-1]["end"] if structure else legs[0]["end"])
        if lo is None and structure is None:
            lo, hi = _entry(t, legs[0]["end"])
        clean = [{k: l[k] for k in ("side", "strike", "option_type")} for l in legs]
        for l in clean:
            l["side"] = l["side"] or "BUY"
        out.update(underlying=und, underlying_known=known, legs=clean, structure=structure or "long",
                   entry_low=lo, entry_high=hi, expiry_hint=_expiry_hint(t, today))
        if und is not None and lo is not None:
            return dict(out, kind=DEFINED_SPREAD if structure else OPTION_BUY)
        missing = "underlying" if und is None else "entry price"
        if not EXIT_RE.search(t):
            return dict(out, kind=INCOMPLETE, reason=f"no {missing} yet")
    if EXIT_RE.search(t):
        return dict(out, kind=EXIT_SIGNAL, reason=EXIT_RE.search(t).group(0).lower())
    if out["sl"] is not None or out["targets"]:
        return dict(out, kind=INCOMPLETE, reason="levels only (SL / targets) — joins the tip it follows",
                    levels_only=True)
    if INCOMPLETE_RE.search(t):
        return dict(out, kind=INCOMPLETE, reason="no strike / CE-PE / price yet")
    return dict(out, kind=CHATTER, reason="no trade content")
