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
from datetime import datetime, timedelta, timezone

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

# ---------------------------------------------------------------------------
# ZONE BANDS
#
# "Cushion" = your two-week total minus the lowest total in the window.
#
# The bands are expressed as a FRACTION OF THE FIELD'S CURRENT SPREAD (highest
# total minus lowest), not as fixed point values. That matters: halfway through
# a window only one week has been played, so the field is bunched inside ~45
# points, while by the end it's spread over ~100. A fixed 60-point band would
# label the entire league "in danger" every single week A.
#
# Scaling to the spread also means these numbers stay correct if your scoring
# settings change or league scoring inflates over the years.
#
# The fractions themselves encode how much football is left: wide early (a lead
# means little with three quarters still to play), tight late.
#
#   phase -> (danger_fraction, watch_fraction, danger_floor, watch_floor)
#
# The fractions scale the bands to how spread out your league actually is.
# The floors are absolute point values that the bands can never fall below,
# derived from how much scoring variance is still to come: two fantasy teams'
# scores differ by roughly 30 points a week on average, so with a full week
# left a 12-point cushion is not safety, however tightly bunched the field is.
# Without the floors a packed league would light up green in week A, which is
# exactly backwards - packed means one bad Sunday reorders everything.
ZONE_BANDS = {
    "A_PRE":  None,                       # nothing kicked off - labels suppressed
    "A_THU":  (0.55, 0.85, 25.0, 50.0),
    "A_SUN":  (0.50, 0.80, 22.0, 44.0),
    "A_LATE": (0.45, 0.75, 20.0, 40.0),
    "B_PRE":  (0.40, 0.70, 18.0, 36.0),
    "B_THU":  (0.35, 0.65, 16.0, 32.0),
    "B_SUN":  (0.25, 0.50, 12.0, 24.0),
    "B_LATE": (0.12, 0.25,  6.0, 12.0),   # Monday of week B: little left to swing
}

# Don't label anyone until most of the field has actually played. After
# Thursday night only a couple of teams have points and the rest sit on zero,
# which would hand six people a DANGER badge for no reason.
MIN_SCORED_FRACTION = 0.75

# label key -> (text, emoji, terminal colour)
ZONE_LABELS = {
    "guillotine": ("IN LINE FOR THE GUILLOTINE", "\U0001FA93", "red"),
    "danger":     ("DANGER ZONE",                "\U0001F630", "yellow"),
    "watch":      ("LOOKING OVER YOUR SHOULDER", "\U0001F440", "yellow"),
    "safe":       ("LOOKING SAFE",               "\U0001F60E", "green"),
    "early":      ("TOO EARLY TO CALL",          "\U0001F550", "dim"),
    "chopped":    ("CHOPPED",                    "\U0001FA93", "red"),
    "survived":   ("SURVIVED",                   "\u2705",     "green"),
    "leader":     ("LEADER",                     "\U0001F3C6", "green"),
    "trailing":   ("TRAILING",                   "\u23F3",     "yellow"),
}

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


def get_league_meta(league_id):
    d = api(f"league/{league_id}") or {}
    return {
        "league_name": d.get("name") or "Chopped League",
        "season": d.get("season"),
        "total_rosters": d.get("total_rosters"),
    }


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


def _eastern_now():
    """Current time in US Eastern. Falls back to a fixed UTC offset if the
    platform has no tz database, so this can never take the board down."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        # Rough fallback: EDT for most of the NFL season, EST after early Nov.
        now = datetime.now(timezone.utc)
        offset = -5 if (now.month >= 11 or now.month < 3) else -4
        return now + timedelta(hours=offset)


def week_bucket(now=None):
    """Where we are inside one NFL week, in Eastern time.

    PRE  - nothing has kicked off (Tue -> Thu 8:15pm)
    THU  - Thursday night is done (Thu 8:15pm -> Sun 1pm)
    SUN  - the Sunday slate is underway (Sun 1pm -> Sun 11:30pm)
    LATE - only Monday night is left (Sun 11:30pm -> Tue)

    This is wall-clock inference, so odd scheduling (London 9:30am kickoffs,
    December Saturday games, Christmas) can put us in the neighbouring bucket
    for a few hours. It only ever shifts a flavour label, never a chop.
    """
    now = now or _eastern_now()
    dow = now.weekday()  # Mon=0 .. Sun=6
    mins = now.hour * 60 + now.minute
    if dow in (1, 2):                      # Tue, Wed
        return "PRE"
    if dow == 3:                           # Thu
        return "PRE" if mins < 20 * 60 + 15 else "THU"
    if dow in (4, 5):                      # Fri, Sat
        return "THU"
    if dow == 6:                           # Sun
        if mins < 13 * 60:
            return "THU"
        return "SUN" if mins < 23 * 60 + 30 else "LATE"
    return "LATE"                          # Mon


def window_phase(current_week, window, now=None):
    """Combine which half of the window we're in with the intra-week bucket."""
    a, b = window
    half = "B" if current_week >= b else "A"
    return f"{half}_{week_bucket(now)}"


def absolute_bands(phase, spread):
    """Turn the phase's spread-fractions into real point values.

    Takes whichever is larger: the band scaled to the league's own spread, or
    the absolute floor for how much football is still to be played.
    """
    cfg = ZONE_BANDS.get(phase)
    if cfg is None or spread <= 0:
        return None
    d_frac, w_frac, d_floor, w_floor = cfg
    return (
        round(max(spread * d_frac, d_floor), 1),
        round(max(spread * w_frac, w_floor), 1),
    )


def zone_for(rank, cushion, bands, labels_live):
    """Pick a zone label key for one team."""
    if not labels_live or bands is None:
        return "early"
    if rank == 1:
        return "guillotine"
    danger, watch = bands
    if cushion <= danger:
        return "danger"
    if cushion <= watch:
        return "watch"
    return "safe"


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
    is_final_window = window is WINDOWS[-1]
    phase = window_phase(current_week, window)
    highest = totals[order[-1]]
    spread = round(highest - lowest, 2)
    bands = absolute_bands(phase, spread)
    # Labels only go live once most of the field has actually played. Before
    # that the cushions are an artefact of the schedule, not of anyone's team.
    scored = sum(1 for v in totals.values() if v > 0)
    labels_live = bool(totals) and scored >= max(2, int(len(totals) * MIN_SCORED_FRACTION))
    window_over = current_week > b
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
        cushion = round(totals[rid] - lowest, 2)
        if is_final_window:
            key = "leader" if i == len(order) - 1 else "trailing"
        elif window_over:
            key = "chopped" if i == 0 else "survived"
        else:
            key = zone_for(i + 1, cushion, bands, labels_live)
        text, emoji, colour = ZONE_LABELS[key]
        rows[-1]["zone"] = {
            "key": key,
            "label": text,
            "emoji": emoji,
            "color": colour,
        }

    return {
        "phase": "championship" if is_final_window else "chop",
        "window": window,
        "is_final": is_final_window,
        "current_week": current_week,
        "week_a_done": current_week > a,
        "window_over": window_over,
        "clock_phase": phase,
        "bands": list(bands) if bands else None,
        "spread": spread,
        "teams_scored": scored,
        "labels_live": labels_live,
        "safety_line": safety_line,
        "lowest": lowest,
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
        f"{'':>2}  {'TEAM':<20} {'W'+str(a):>7} {'W'+str(b):>7} {'TOTAL':>8} "
        f"{'CUSHION':>8}  ZONE"
    )
    out.append("-" * 86)
    for r in board["rows"]:
        z = r["zone"]
        line = (
            f"{r['rank']:>2}  {r['team'][:20]:<20} {r['week_a']:>7.2f} {r['week_b']:>7.2f} "
            f"{r['total']:>8.2f} {('+' + format(r['above_chop'], '.2f')) if r['above_chop'] else '—':>8}  "
        )
        out.append(line + c(f"{z['emoji']} {z['label']}", z["color"]))
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
    if board["bands"] and board["labels_live"]:
        d, w = board["bands"]
        out.append(
            c(
                f"Cushion bands right now: under {d} = danger, under {w} = "
                f"looking over your shoulder. They tighten as the window closes.",
                "dim",
            )
        )
    elif not board["labels_live"]:
        out.append(c(f"Only {board['teams_scored']} of {len(board['rows'])} teams have "
                     f"points on the board — zones stay dark until most of the "
                     f"field has played.", "dim"))

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
    lines.append(f"| # | Team | W{a} | W{b} | Total | Cushion |")
    lines.append("|---|------|----:|----:|------:|--------:|")
    for r in board["rows"]:
        z = r["zone"]
        lines.append(
            f"| {r['rank']} | {z['emoji']} {r['team']} | {r['week_a']:.2f} | {r['week_b']:.2f} "
            f"| **{r['total']:.2f}** | {'+' + format(r['above_chop'], '.2f') if r['above_chop'] else '—'} |"
        )
    lines.append("")
    low = board["rows"][0]
    if not board["is_final"] and low["needs"] is not None:
        z = low["zone"]
        lines.append(
            f"{z['emoji']} **{low['team']} is {z['label'].lower()}** — needs "
            f"**{low['needs']:.2f}** more points to climb out of last."
        )
    if board["bands"] and board["labels_live"]:
        d, w = board["bands"]
        lines.append("")
        lines.append(
            f"_Cushion under {d} pts = danger \u00b7 under {w} pts = looking over "
            f"your shoulder. Bands tighten as the window closes._"
        )
    elif not board["labels_live"]:
        lines.append("")
        lines.append(
            f"_Only {board['teams_scored']} of {len(board['rows'])} teams have played — "
            f"zones stay dark until most of the field is in._"
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
        f"_Form guide only. Nobody can be chopped in the warm-up and these points "
        f"do not carry. First chop window is Weeks {na}+{nb}._"
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


def meta_block(league_id):
    m = get_league_meta(league_id)
    m["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return m


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
            print(
                json.dumps(
                    {"meta": meta_block(args.league), "board": data, "history": []},
                    indent=2,
                    default=str,
                )
            )
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
        hist = [
            {
                **h,
                "chopped_name": res["names"].get(h.get("chopped")),
                "champion_name": res["names"].get(h.get("champion")),
            }
            for h in res["history"]
        ]
        print(
            json.dumps(
                {"meta": meta_block(args.league), "board": board, "history": hist},
                indent=2,
                default=str,
            )
        )
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
