# Chop Tracker

Live two-week cumulative standings for a biweekly chopped league on Sleeper.

Sleeper's native Chopped mode only supports one elimination per week and can't
be configured for warm-up weeks or a two-week cadence. Its standings screen shows
season-long Points For, and its scoreboard shows a single week. Neither one is
the number that decides your chop. This computes that number.

## Format encoded

| Weeks | What happens | Teams after |
|-------|--------------|-------------|
| 1–2   | Warm-up, no chop, points don't carry | 8 |
| 3+4   | Lowest cumulative chopped | 7 |
| 5+6   | Lowest cumulative chopped | 6 |
| 7+8   | Lowest cumulative chopped | 5 |
| 9+10  | Lowest cumulative chopped | 4 |
| 11+12 | Lowest cumulative chopped | 3 |
| 13+14 | Lowest cumulative chopped | 2 |
| 15+16 | Championship, highest total wins | 1 |

Weeks 17–18 are unused — useful buffer if the NFL schedule shifts or you want
to push the final back.

## Setup

1. Get your league ID. Open the league on sleeper.com in a browser; the URL is
   `sleeper.com/leagues/<LEAGUE_ID>/...`. It's an 18-digit number.

2. Run it:

```bash
export SLEEPER_LEAGUE_ID=1234567890123456789
python3 chop_tracker.py
```

No dependencies — standard library only. No API key; Sleeper's read API is open.

## Commands

```bash
python3 chop_tracker.py                # live terminal board
python3 chop_tracker.py --markdown     # paste-ready block for league chat
python3 chop_tracker.py --history      # every completed window + who got chopped
python3 chop_tracker.py --json         # machine-readable
python3 chop_tracker.py --week 4       # pretend it's week 4 (test before the season)
```

## Automating it

Copy `chop-tracker.yml` to `.github/workflows/chop-tracker.yml` in a repo with
`chop_tracker.py` at the root. Add repo secrets:

- `SLEEPER_LEAGUE_ID` — required
- `WEBHOOK_URL` — optional Discord or Slack incoming webhook

It runs through the Sunday and Monday game windows, commits `STANDINGS.md`
to the repo, and posts the board to the webhook if one is set.

Sleeper has no write API, so nothing can post into Sleeper's own league chat.
If your league lives in Sleeper chat only, run `--markdown` and paste.

## How elimination is determined

There's no state file. Every run replays the season from week 1: for each
window where the current NFL week has passed the window's end, it sums both
weeks for the teams still alive, chops the lowest, and moves on. That means a
bad run or a lost file can't corrupt anything, and stat corrections that land
on Tuesday are picked up automatically on the next run.

A window is treated as final once Sleeper's `state/nfl` week advances past it,
which happens Tuesday morning. Anything before that is live and provisional.

**Tiebreak:** lowest two-week total, then lowest season-total points through
that window (Sleeper's own convention). If teams are still tied, the script
prints an `UNRESOLVED TIE` warning rather than guessing — you break it.

## Commissioner runbook, per chop

Sleeper won't do any of this automatically in a manual league:

1. **Release the chopped roster.** League Settings → Manage Rosters → drop the
   eliminated team's players so they hit waivers. Decide before the draft
   whether they go to waivers with a claim period or straight to free agency.
2. **Lock the chopped manager out.** There's no per-team lock. Practical option
   is removing them as a co-manager so the orphaned roster is commissioner-run.
   Set their FAAB to 0 as a belt-and-suspenders move.
3. **Ignore the W/L column.** There are no real matchups. Sleeper will still
   generate them; tell the league to disregard.

## Things to settle before the draft

- **Bye weeks.** A two-week window can hand one team four starters on bye while
  another has none. That's a much bigger swing than in a weekly chop format.
  Either accept it as variance or add a rule (e.g. the chopped team must also be
  below some floor, otherwise the second-lowest goes).
- **FAAB.** Six chopped rosters over the season is a lot of talent hitting the
  wire. A large season budget ($1000 is Sleeper's recommendation for chopped)
  makes bidding meaningful late.
- **Trades.** Sleeper disables trades by default in chopped formats. With only
  eight teams and elimination stakes, collusion risk is real — leaving them off
  is the safer call.
- **Roster size.** No playoffs means no reason to stash. Consider deeper starting
  lineups and a shallow bench so bye weeks hurt everyone more evenly.
