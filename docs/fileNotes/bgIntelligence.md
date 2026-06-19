# BG Intelligence Files

The BG Intelligence stack is split across a few large files:

- [`features/staff/bgIntelligence/service.py`](../../features/staff/bgIntelligence/service.py)
- [`features/staff/bgIntelligence/rendering.py`](../../features/staff/bgIntelligence/rendering.py)
- [`features/staff/bgIntelligence/scoring.py`](../../features/staff/bgIntelligence/scoring.py)
- [`cogs/staff/bgIntelligenceCog.py`](../../cogs/staff/bgIntelligenceCog.py)

If Jane is doing a `/bg-intel` scan, these files are probably involved.

## What Each File Does

### `service.py`

This is the scan engine.

It gathers Roblox profile data, groups, badges, favorite games, inventory and gamepass information, external source results, prior report history, direct rule matches, and alt-link evidence. It then builds a `BgIntelligenceReport` object that the rest of the system can score, render, and save.

The main entry points are:

- `buildReport(...)`
- `buildReportForDiscordId(...)`
- `buildReportForRobloxIdentity(...)`
- `recordReport(...)`
- `recordAltLink(...)`

Most scan tuning belongs here or in the lower-level Roblox helper modules. If a result is wrong before it reaches the embed, start here.

### `scoring.py`

This turns a completed report into a risk score.

It should be mostly pure logic:

- inspect report fields
- add weighted signals
- calculate confidence
- choose a risk band

Try to keep network calls, DB writes, and Discord calls out of this file. Scoring is much easier to test when it stays boring.

### `rendering.py`

This turns a report into Discord output.

It builds:

- overview embeds
- detail section embeds
- decision summaries
- text-file exports
- badge timeline graph files

Rendering should not decide whether something is risky. It can summarize why a signal exists, but the risk decision should already be present in the report or score object.

### `bgIntelligenceCog.py`

This is the Discord-facing command and view layer.

It owns:

- slash command handling
- progress updates
- section dropdowns
- rerun and summary buttons
- dispute controls
- report-channel posting

If a button or interaction breaks, start here. If the scan result itself is wrong, start in `service.py`.

## Data Flow

The normal path is:

1. Discord command starts in `BgIntelligenceCog`.
2. The cog asks `service.py` to build a report.
3. `service.py` gathers data and records timing/progress.
4. `scoring.py` scores the report.
5. `rendering.py` builds public and detailed output.
6. The cog updates the Discord message and optional report channel post.
7. `recordReport(...)` stores enough data for future prior-report checks.

## Common Pitfalls

- Do not make rendering do new network or DB work.
- Do not make scoring depend on live Discord objects.
- Keep progress callbacks lightweight. They run while staff are waiting on a scan.
- Be careful with field names in `BgIntelligenceReport`. Rendering and scoring both rely on that shape.
- When adding a new scan source, decide whether it needs a status, an error string, a summary dict, or all three.
- Inventory visual matching can become slow quickly. Add early filters in the scan layer before comparing lots of candidates.

## Good Small Edits

- add a new summary field to the report and render it in one place
- tune scoring weights in `scoring.py`
- improve wording in a renderer without changing scan behavior
- add timing around a slow scan step
- add a focused unit test for a scoring or rendering edge case
