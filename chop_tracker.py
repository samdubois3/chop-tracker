#!/usr/bin/env python3
"""
Chopped League live tracker for Sleeper.

Format this is built for:
  - 8 teams
  - Weeks 1-2 are a warm-up (no chop)
  - From week 3 on, teams are chopped every TWO weeks on the CUMULATIVE
    score of that two-week window (W3+W4, then W5+W6, etc.)
  - Final window is a two-week head-to-head between the last two teams

Sleeper's app can't show a rolling two-week total, so this does it.
Uses Sleeper's free read-only API (no token, no auth).
Docs: https://docs.sleeper.com

Usage:
    python chop_tracker.py                    # live board for the current window
    python chop_tracker.py --markdown         # markdown block to paste in league chat
    python chop_tracker.py --history          # every completed window + who got chopped
    python chop_tracker.py --json             # machine-readable
    python chop_tracker.py --week 4           # pretend it's week 4 (testing / backfill)

Env vars:
    SLEEPER_LEAGUE_ID   (required unless set in CONFIG below)
    WEBHOOK_URL         (optional; Discord or Slack incoming webhook)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "").strip()

# Weeks that don't count toward any chop.
WARMUP_WEEKS = (1, 2)

# Each tuple is one scoring window. Lowest CUMULATIVE score in the window is
# chopped. The last window is the championship (no chop, highest total wins).
WINDOWS = [
    (3, 4),
    (5, 6),
    (7, 8),
    (9, 10),
    (11, 12),
    (13, 14),
    (15, 16),  # championship: last 2 teams standing
]

# Tiebreak scope: warm-up points don't carry, so the season-total tiebreak
# starts at the first chop week rather than week 1.
SCORING_STARTS = WINDOWS[0][0]

API = "https://api.sleeper.app/v1"
USER_AGENT = "chop-tracker/1.0"

# ----------------------------------------------------------------------------
# API helpers
# ----------------------------------------------------------------------------


def api(path, retries=3):
    """GET a Sleeper API path and return parsed JSON."""
    url = f"{API}/{path.lstrip('/')}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Sleeper API failed for {url}: {last_err}")


def get_state():
    return api("state/nfl") or {}


def get_team_names(league_id):
    """roster_id -> display name. Prefers custom team name, falls back to handle."""
    rosters = api(f"league/{league_id}/rosters") or []
    users = api(f"league/{league_id}/users") or []
    by_user = {}
    for u in users:
        meta = u.get("metadata") or {}
        by_user[u["user_id"]] = (
            meta.get("team_name") or u.get("display_name") or u.get("username") or "?"
        )
    names = {}
    for r in rosters:
        rid = r["roster_id"]
        names[rid] = by_user.get(r.get("owner_id"), f"Roster {rid}")
    return names


class WeekCache:
    """Lazily fetches and caches per-week points so we never re-request a week."""

    def __init__(self, league_id):
        self.league_id = league_id
        self._cache = {}

    def points(self, week):
        if week not in self._cache:
            rows = api(f"league/{self.league_id}/matchups/{week}") or []
            pts = {}
            for row in rows:
                rid = row.get("roster_id")
                if rid is None:
                    continue
                # custom_points is a commissioner manual override; it wins.
                val = row.get("custom_points")
                if val is None:
                    val = row.get("points")
                pts[rid] = float(val or 0.0)
            self._cache[week] = pts
        return self._cache[week]

    def season_total_through(self, week, roster_id):
        """Cumulative points from the first chop week through `week`, inclusive.

        Excludes the warm-up weeks — those points don't carry forward, so they
        shouldn't decide a tiebreak either.
        """
        return sum(
            self.points(w).get(roster_id, 0.0)
            for w in range(SCORING_STARTS, week + 1)
        )


# ----------------------------------------------------------------------------
# Chop logic
# ----------------------------------------------------------------------------


def window_totals(cache, window, survivors):
    a, b = window
    pa, pb = cache.points(a), cache.points(b)
    return {rid: round(pa.get(rid, 0.0) + pb.get(rid, 0.0), 2) for rid in survivors}


def pick_chopped(cache, window, survivors, totals):
    """
    Lowest two-week total is chopped.
    Tiebreak 1: fewer cumulative points from week 3 through the end of the
                window (warm-up weeks excluded — they don't carry forward).
    Tiebreak 2: flagged for manual resolution (returns a warning).
    """
    low = min(totals.values())
    tied = sorted([rid for rid, v in totals.items() if abs(v - low) < 1e-9])
    if len(tied) == 1:
        return tied[0], None

    b = window[1]
    season = {rid: round(cache.season_total_through(b, rid), 2) for rid in tied}
    low_season = min(season.values())
    still_tied = sorted([rid for rid in tied if abs(season[rid] - low_season) < 1e-9])
    if len(still_tied) == 1:
        return (
            still_tied[0],
            f"Tie at {low} pts broken on cumulative points since week "
            f"{SCORING_STARTS}.",
        )
    return (
        still_tied[0],
        f"UNRESOLVED TIE at {low} pts between roster_ids {still_tied}. "
        f"Commissioner must break this manually (Sleeper's convention is "
        f"better draft position gets chopped).",
    )


def resolve(cache, league_id, current_week):
    """
    Replays every completed window from the start to figure out who's still alive.
    No state file needed — it's derived fresh each run, so it's self-healing.
    """
    names = get_team_names(league_id)
    survivors = set(names.keys())
    history = []
    active = None
    warnings = []

    for window in WINDOWS:
        a, b = window
        is_final = window is WINDOWS[-1]
        if current_week > b:
            totals = window_totals(cache, window, survivors)
            if is_final:
                champ = max(totals, key=lambda r: totals[r])
                history.append(
                    {"window": window, "totals": totals, "champion": champ, "chopped": None}
                )
                survivors = {champ}
                break
            chopped, warn = pick_chopped(cache, window, survivors, totals)
            if warn:
                warnings.append(f"W{a}-{b}: {warn}")
            history.append(
                {"window": window, "totals": totals, "chopped": chopped, "champion": None}
            )
            survivors = survivors - {chopped}
        else:
            active = window
            break

    return {
        "names": names,
        "survivors": survivors,
        "history": history,
        "active": active,
        "warnings": warnings,
    }


def build_board(cache, res, current_week):
    """The live board for the active window, sorted lowest-first (chop zone on top)."""
    window = res["active"]
    if window is None:
        return None
    a, b = window
    survivors = res["survivors"]
    pa, pb = cache.points(a), cache.points(b)
    totals = window_totals(cache, window, survivors)

    order = sorted(survivors, key=lambda r: (totals[r], res["names"].get(r, "")))
    lowest = totals[order[0]]
    # The number everyone is chasing: the second-lowest total. Clear it and
    # you're out of the chop zone.
    safety_line = totals[order[1]] if len(order) > 1 else None

    rows = []
    for i, rid in enumerate(order):
        rows.append(
            {
                "rank": i + 1,
                "roster_id": rid,
                "team": res["names"].get(rid, f"Roster {rid}"),
                "week_a": round(pa.get(rid, 0.0), 2),
                "week_b": round(pb.get(rid, 0.0), 2),
                "total": totals[rid],
                "above_chop": round(totals[rid] - lowest, 2),
                "needs": round(safety_line - totals[rid], 2)
                if (i == 0 and safety_line is not None)
                else None,
            }
        )

    is_final = window is WINDOWS[-1]
    return {
        "window": window,
        "is_final": is_final,
        "current_week": current_week,
        "week_a_done": current_week > a,
        "safety_line": safety_line,
        "rows": rows,
    }


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

C = {
    "red": "\033[91m",
    "yellow": "\033[93m",
    "green": "\033[92m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "off": "\033[0m",
}


def zone(rank, n):
    if rank == 1:
        return "CHOP", "red"
    if rank <= min(3, max(2, n // 3)):
        return "DANGER", "yellow"
    return "SAFE", "green"


def render_terminal(board, res, color=True):
    def c(s, key):
        return f"{C[key]}{s}{C['off']}" if color else s

    a, b = board["window"]
    n = len(board["rows"])
    out = []
    if board["is_final"]:
        head = f"CHAMPIONSHIP — Weeks {a}+{b} (highest total wins)"
    else:
        head = f"CHOP WINDOW — Weeks {a}+{b}  ({n} teams alive, 1 gets chopped)"
    out.append(c(head, "bold"))
    status = "final" if board["current_week"] > b else f"live, currently week {board['current_week']}"
    out.append(c(f"{status} · {datetime.now(timezone.utc).astimezone():%a %b %d %I:%M %p %Z}", "dim"))
    out.append("")

    out.append(
        f"{'':>2}  {'TEAM':<22} {'W'+str(a):>7} {'W'+str(b):>7} {'TOTAL':>8} {'+/- CHOP':>9}  ZONE"
    )
    out.append("-" * 70)
    for r in board["rows"]:
        z, col = zone(r["rank"], n)
        if board["is_final"]:
            z, col = ("LEADER", "green") if r["rank"] == n else ("TRAILING", "yellow")
        line = (
            f"{r['rank']:>2}  {r['team'][:22]:<22} {r['week_a']:>7.2f} {r['week_b']:>7.2f} "
            f"{r['total']:>8.2f} {('+' + format(r['above_chop'], '.2f')) if r['above_chop'] else '—':>9}  "
        )
        out.append(line + c(z, col))
    out.append("")

    low = board["rows"][0]
    if board["is_final"]:
        gap = board["rows"][-1]["total"] - low["total"]
        out.append(f"Margin: {gap:.2f} pts")
    elif low["needs"] is not None:
        out.append(
            c(
                f"SCORE TO BEAT: {low['team']} needs {low['needs']:.2f} more pts "
                f"to clear the chop line ({board['safety_line']:.2f}).",
                "bold",
            )
        )
    if not board["week_a_done"]:
        out.append(c(f"Week {b} hasn't been played yet — totals are week {a} only.", "dim"))

    if res["history"]:
        out.append("")
        chopped = [
            f"W{h['window'][0]}-{h['window'][1]}: {res['names'].get(h['chopped'], '?')}"
            for h in res["history"]
            if h.get("chopped")
        ]
        out.append(c("Chopped so far — " + ("; ".join(chopped) if chopped else "nobody"), "dim"))
    for w in res["warnings"]:
        out.append(c("! " + w, "yellow"))
    return "\n".join(out)


def render_markdown(board, res):
    a, b = board["window"]
    n = len(board["rows"])
    lines = []
    if board["is_final"]:
        lines.append(f"**CHAMPIONSHIP — Weeks {a}+{b}**")
    else:
        lines.append(f"**CHOP WINDOW — Weeks {a}+{b}** · {n} alive · lowest total goes home")
    lines.append("")
    lines.append(f"| # | Team | W{a} | W{b} | Total | +/- Chop |")
    lines.append("|---|------|----:|----:|------:|---------:|")
    for r in board["rows"]:
        marker = " 🪓" if r["rank"] == 1 and not board["is_final"] else ""
        lines.append(
            f"| {r['rank']} | {r['team']}{marker} | {r['week_a']:.2f} | {r['week_b']:.2f} "
            f"| **{r['total']:.2f}** | {'+' + format(r['above_chop'], '.2f') if r['above_chop'] else '—'} |"
        )
    lines.append("")
    low = board["rows"][0]
    if not board["is_final"] and low["needs"] is not None:
        lines.append(
            f"**Score to beat:** {low['team']} needs **{low['needs']:.2f}** more points "
            f"to clear {board['safety_line']:.2f} and get out of the chop zone."
        )
    chopped = [
        f"W{h['window'][0]}-{h['window'][1]}: {res['names'].get(h['chopped'], '?')}"
        for h in res["history"]
        if h.get("chopped")
    ]
    if chopped:
        lines.append("")
        lines.append("_Chopped: " + " · ".join(chopped) + "_")
    return "\n".join(lines)


def warmup_data(cache, res, current_week):
    """Weeks 1-2 structured payload. Nobody can be chopped; points don't carry."""
    a, b = WARMUP_WEEKS
    pa, pb = cache.points(a), cache.points(b)
    order = sorted(
        res["names"].items(),
        key=lambda kv: (-(pa.get(kv[0], 0.0) + pb.get(kv[0], 0.0)), kv[1]),
    )
    rows = []
    for i, (rid, name) in enumerate(order, 1):
        wa, wb = round(pa.get(rid, 0.0), 2), round(pb.get(rid, 0.0), 2)
        rows.append(
            {
                "rank": i,
                "roster_id": rid,
                "team": name,
                "week_a": wa,
                "week_b": wb,
                "total": round(wa + wb, 2),
            }
        )
    return {
        "phase": "warmup",
        "window": (a, b),
        "current_week": current_week,
        "week_a_done": current_week > a,
        "next_chop_window": WINDOWS[0],
        "rows": rows,
    }


def render_warmup(data):
    """Terminal view for the warm-up weeks."""
    a, b = data["window"]
    out = [
        f"WARM-UP — Weeks {a}+{b} (no chops; these points do NOT carry forward)",
        f"Currently week {data['current_week']}. First chop window is Weeks "
        f"{data['next_chop_window'][0]}+{data['next_chop_window'][1]}.",
        "",
        f"{'':>2}  {'TEAM':<22} {'W'+str(a):>7} {'W'+str(b):>7} {'TOTAL':>8}",
        "-" * 52,
    ]
    for r in data["rows"]:
        out.append(
            f"{r['rank']:>2}  {r['team'][:22]:<22} {r['week_a']:>7.2f} "
            f"{r['week_b']:>7.2f} {r['total']:>8.2f}"
        )
    if not data["week_a_done"]:
        out.append("")
        out.append(f"Week {a} is not final yet — totals are provisional.")
    return "\n".join(out)


def render_warmup_markdown(data):
    """Markdown view for the warm-up weeks, so --markdown works in W1-2 too."""
    a, b = data["window"]
    na, nb = data["next_chop_window"]
    lines = [
        f"**WARM-UP — Weeks {a}+{b}** · no chops · these points do NOT carry forward",
        "",
        f"| # | Team | W{a} | W{b} | Total |",
        "|---|------|----:|----:|------:|",
    ]
    for r in data["rows"]:
        lines.append(
            f"| {r['rank']} | {r['team']} | {r['week_a']:.2f} | "
            f"{r['week_b']:.2f} | **{r['total']:.2f}** |"
        )
    lines.append("")
    lines.append(
        f"_First chop window is Weeks {na}+{nb} — lowest two-week total goes home._"
    )
    return "\n".join(lines)


def render_history(res):
    out = []
    for h in res["history"]:
        a, b = h["window"]
        out.append(f"Weeks {a}+{b}")
        for rid, total in sorted(h["totals"].items(), key=lambda kv: kv[1]):
            tag = ""
            if rid == h.get("chopped"):
                tag = "  <- CHOPPED"
            if rid == h.get("champion"):
                tag = "  <- CHAMPION"
            out.append(f"   {res['names'].get(rid, rid):<24} {total:>8.2f}{tag}")
        out.append("")
    return "\n".join(out) if out else "No completed windows yet."


def post_webhook(url, text):
    payload = {"content": text} if "discord" in url else {"text": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


# ----------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Sleeper biweekly chopped-league tracker")
    p.add_argument("--league", default=LEAGUE_ID, help="Sleeper league ID")
    p.add_argument("--week", type=int, help="Override current NFL week")
    p.add_argument("--markdown", action="store_true", help="Markdown table for league chat")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--history", action="store_true", help="Show all completed windows")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--webhook", default=os.environ.get("WEBHOOK_URL", ""))
    args = p.parse_args()

    if not args.league:
        sys.exit("No league ID. Set SLEEPER_LEAGUE_ID or pass --league.")

    state = get_state()
    current_week = args.week or state.get("week") or 1

    cache = WeekCache(args.league)
    res = resolve(cache, args.league, current_week)

    if args.history:
        print(render_history(res))
        return

    if current_week < WINDOWS[0][0]:
        data = warmup_data(cache, res, current_week)
        if args.json:
            print(json.dumps({"board": data, "history": []}, indent=2, default=str))
            return
        text = render_warmup_markdown(data) if args.markdown else render_warmup(data)
        print(text)
        if args.webhook and args.markdown:
            try:
                post_webhook(args.webhook, render_warmup_markdown(data))
            except Exception as e:  # noqa: BLE001
                print(f"\n(webhook failed: {e})", file=sys.stderr)
        return

    board = build_board(cache, res, current_week)
    if board is None:
        champ = res["history"][-1].get("champion") if res["history"] else None
        if champ:
            print(f"Season complete. Champion: {res['names'].get(champ, champ)}")
        else:
            print(f"Week {current_week}: no chop window active.")
        return

    if args.json:
        print(json.dumps({"board": board, "history": res["history"]}, indent=2, default=str))
        return

    text = render_markdown(board, res) if args.markdown else render_terminal(
        board, res, color=not args.no_color
    )
    print(text)

    if args.webhook:
        try:
            post_webhook(args.webhook, render_markdown(board, res))
        except Exception as e:  # noqa: BLE001
            print(f"\n(webhook failed: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
