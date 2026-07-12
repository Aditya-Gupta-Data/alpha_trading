"""
src/discovery/inspect.py — the owner's window into one pattern
==============================================================

Owner concern #2 (2026-07-11): "I'm skeptical about what it finds or how
it triggers trades." This CLI reconstructs EVERYTHING about a discovered
pattern in plain English — its frozen definition, where it came from, its
full lifecycle audit trail, and every shadow fire with its host trade and
result — so skepticism can be exercised on facts instead of trust.

Read-only over brain_map.db. Match by pattern-id prefix, the auto: tag,
or any word from its tags/description.

    python3 -m src.discovery.inspect                 # list all patterns
    python3 -m src.discovery.inspect a1b2c3          # id prefix
    python3 -m src.discovery.inspect auto:a1b2c3d4   # minted tag
    python3 -m src.discovery.inspect expiry_week     # tag/description word
"""

import json
import sys

from src.validation import registry as rg
from src.validation import trial


def find_patterns(conn, needle: str = "") -> list:
    """All candidate_patterns rows matching the needle (id prefix, auto:
    tag, or substring of definition/description). Empty needle = all."""
    rg.ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM candidate_patterns ORDER BY discovered_at").fetchall()
    needle = (needle or "").strip()
    if needle.startswith("auto:"):
        needle = needle[5:]
    if not needle:
        return list(rows)
    n = needle.lower()
    return [r for r in rows
            if r["pattern_id"].startswith(n)
            or n in (r["definition"] or "").lower()
            or n in (r["description"] or "").lower()]


def shadow_history(conn, pattern_id: str) -> list:
    trial.ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM shadow_trades WHERE pattern_id = ? "
        "ORDER BY fire_date", (pattern_id,)).fetchall()


def render(conn, row) -> str:
    """One pattern, fully reconstructed, phone-readable."""
    pid = row["pattern_id"]
    try:
        definition = json.loads(row["definition"])
    except (ValueError, TypeError):
        definition = {}
    lines = [f"🔍 **{rg.mint_tag(pid)}**  ({row['kind']}, {row['status']})",
             f"definition: {' + '.join(definition.get('tags') or [])}",
             f"described: {row['description'] or '(none)'}",
             f"mined by: {row['mining_run'] or '(manual)'} "
             f"window {row['discovery_window'] or '?'}",
             f"registered: {row['discovered_at']}"]
    if row["insample_stats"]:
        try:
            ins = json.loads(row["insample_stats"])
            lines.append(f"in-sample: {ins.get('wins')}/{ins.get('n')} "
                         f"({(ins.get('win_rate') or 0):.0%} vs base "
                         f"{(ins.get('expected_rate') or 0):.0%}, "
                         f"p={ins.get('p_value'):.4f}) "
                         f"[{ins.get('corpus')}] — DISCOVERY data, "
                         "counts for nothing")
        except (ValueError, TypeError):
            pass
    if row["oos_stats"]:
        try:
            oos = json.loads(row["oos_stats"])
            real = oos.get("real") or {}
            lines.append(f"out-of-sample: {real.get('wins', 0)}/"
                         f"{real.get('n', 0)} real — the number that matters")
        except (ValueError, TypeError):
            pass

    lines.append("\n**Lifecycle:**")
    for a in rg.audit_trail(conn, pid):
        lines.append(f"• {a['at'][:16]}  {a['from_status'] or '∅'} → "
                     f"{a['to_status']}: {(a['reason'] or '')[:70]}")

    fires = shadow_history(conn, pid)
    lines.append(f"\n**Shadow fires ({len(fires)}):**")
    if not fires:
        lines.append("• none yet — it hasn't matched a live entry")
    for f in fires[-15:]:
        if f["resolved"]:
            r = f["r_multiple"]
            lines.append(f"• {f['fire_date']} {f['ticker']}: "
                         f"{f['result'].upper()} "
                         f"({r:+.2f}R)" + (f"  [host {f['host_ref']}]"
                                           if f["host_ref"] else ""))
        else:
            lines.append(f"• {f['fire_date']} {f['ticker']}: open"
                         + (f"  [host {f['host_ref']}]"
                            if f["host_ref"] else ""))
    return "\n".join(lines)


def render_list(rows) -> str:
    if not rows:
        return ("(no patterns registered yet — the miners haven't found "
                "anything that cleared the floors. Honest silence.)")
    lines = [f"{len(rows)} pattern(s):"]
    for r in rows:
        lines.append(f"• {rg.mint_tag(r['pattern_id'])} [{r['status']:>13}] "
                     f"{(r['description'] or r['kind'])[:70]}")
    return "\n".join(lines)


def main(argv=None) -> str:
    from src import brain_map
    argv = argv if argv is not None else sys.argv[1:]
    needle = " ".join(argv)
    conn = brain_map.connect()
    try:
        rows = find_patterns(conn, needle)
        if needle and len(rows) == 1:
            out = render(conn, rows[0])
        else:
            out = render_list(rows)
    finally:
        conn.close()
    print(out)
    return out


if __name__ == "__main__":
    main()
