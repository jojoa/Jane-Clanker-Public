# Jane Identity

Jane Identity is Jane's private Discord-to-Roblox linker. It is intentionally not a RoVer-style dashboard.

The user flow is:

1. A user runs `/verify` in Discord.
2. If Jane already has a linked account, Jane refreshes the member nickname and roles immediately.
3. If Jane does not have a linked account, Jane sends an ephemeral button.
4. The button opens Roblox OAuth directly.
5. Roblox redirects back to Jane's callback URL.
6. Jane stores the Discord ID, Roblox user ID, and Roblox username in `roblox_identity_links`.
7. Jane can update the member nickname and configured verification roles.

Users do not sign into Discord on a website. Discord identity comes from the slash command interaction. The only browser step is Roblox sign-in and consent.

## Config

Required:

- `JANE_IDENTITY_WEB_ENABLED=true`
- `JANE_IDENTITY_PUBLIC_BASE_URL=https://your-public-host`
- `ROBLOX_OAUTH_CLIENT_ID=...`
- `ROBLOX_OAUTH_CLIENT_SECRET=...`

Optional:

- `JANE_IDENTITY_WEB_HOST=127.0.0.1`
- `JANE_IDENTITY_WEB_PORT=8791`
- `JANE_IDENTITY_REDIRECT_URI=https://your-public-host/identity/roblox/callback`
- `JANE_IDENTITY_API_TOKEN=...`

The Roblox OAuth app redirect URI must match Jane's redirect URI exactly. By default that is:

`{JANE_IDENTITY_PUBLIC_BASE_URL}/identity/roblox/callback`

Jane also serves the public pages Roblox asks for during OAuth app setup:

- Entry Link: `{JANE_IDENTITY_PUBLIC_BASE_URL}/`
- Privacy Terms URL: `{JANE_IDENTITY_PUBLIC_BASE_URL}/privacy`
- Terms of Service URL: `{JANE_IDENTITY_PUBLIC_BASE_URL}/terms`

## Guided Tunnel Setup

For production without buying a domain, use Tailscale Funnel. The Jane host needs Tailscale installed, logged in, and allowed to use Funnel in the tailnet. When the helper runs on the Jane host, it can detect the host's public Tailscale HTTPS URL automatically:

```powershell
.\.venv\Scripts\python.exe tools\setup_jane_identity_tunnel.py
```

If auto-detection fails, pass the URL manually:

```powershell
.\.venv\Scripts\python.exe tools\setup_jane_identity_tunnel.py --domain https://your-jane-host.your-tailnet.ts.net
```

The helper:

- updates `localOnly/credentials/jane-runtime-secrets.env`
- generates `JANE_IDENTITY_API_TOKEN` if one does not exist
- writes `localOnly/identity/john-identity.env` for the future John handoff
- writes `localOnly/tailscale/run-jane-identity-funnel.ps1`
- prints the Tailscale Funnel command and Roblox redirect URI

The generated Tailscale Funnel command points the public HTTPS URL at Jane's local callback server:

```powershell
tailscale funnel --bg --yes --https=443 http://127.0.0.1:8791
```

The Tailscale login is separate from `JANE_IDENTITY_API_TOKEN`, which is the bearer token John uses when calling Jane's identity API.

The same setup can be triggered from Jane's DMs by an authorized JaneSecrets user:

```text
!janesecrets setup-identity-tailscale
```

Jane detects the Tailscale URL from the host automatically. You can still override it by adding the URL after the command. Jane starts her local Identity web server, runs the Tailscale Funnel command, and sends a spoiler-wrapped John `.env` handoff that deletes after 10 minutes.

To start Funnel again later:

```text
!janesecrets start-identity-tailscale
```

Cloudflare is still supported if a real domain is added later:

```powershell
.\.venv\Scripts\python.exe tools\setup_jane_identity_tunnel.py --domain identity.example.com --tunnel-provider cloudflare
```

Use `--prompt-roblox` if you want the helper to ask for missing Roblox OAuth client credentials. The Roblox app redirect URI must be exactly the redirect URI printed by the helper.

## Role Rules

`janeIdentityVerifiedRoleIds` is a list of Discord roles Jane grants to anyone who verifies.

`janeIdentityGroupRoleRules` can grant roles based on Roblox group rank:

```python
janeIdentityGroupRoleRules = [
    {
        "groupId": 36000077,
        "roleId": 123456789012345678,
        "minRank": 10,
        "maxRank": 255,
        "removeWhenUnmatched": True,
    },
]
```

Server administrators can also configure this through `/rover`.

The `/rover` hub can:

- connect or remove Roblox groups for the server
- open a role-rules panel with a Roblox group dropdown
- add rank-to-role rules by rank number and Discord role
- remove rank-to-role rules by rule ID
- open a name-rules panel with a Roblox group dropdown
- add rank-to-nickname rules by rank number
- remove rank-to-nickname rules by rule ID
- set or clear the server's unverified role
- start a manual bulk update for the server

The role-rules panel fetches rank names for the selected Roblox group only. Use the dropdown to switch groups. Use **Add Role Rule** from that panel with:

- the rank number shown for the selected group
- the Discord role mention or ID
- optional `yes/no` for removing the Discord role when the member no longer has that Roblox rank; blank defaults to `yes`

The name-rules panel uses the same selected-group/rank-number flow. Use **Add Name Rule** from that panel with:

- the rank number shown for the selected group
- optional nickname prefix
- optional nickname suffix
- optional priority

Nickname rules format users as:

`prefix robloxusername suffix`

Use the brackets in the configured prefix/suffix if you want bracketed names, such as `[HR]`.

## Join-Time And Bulk Updates

When a member joins a server, Jane checks her stored identity links. If the member is already linked, Jane applies the configured nickname, verified roles, rank roles, and removes the unverified role. If the member is not linked, Jane applies the configured unverified role and removes Jane-managed verified/rank roles.

The `/rover` hub's **Bulk Update** button performs the same sweep for current server members. It uses Jane's stored identity table directly and does not call RoVer for unlinked members. Roblox group lookups still happen for linked users when rank-role or nickname rules need current group ranks.

Bulk update behavior is intentionally manual and throttled:

- `janeIdentityBulkUpdateFetchMembers = True`
- `janeIdentityBulkUpdateMaxMembers = 5000`
- `janeIdentityBulkUpdateDelaySec = 0.20`

This gives administrators a way to refresh everyone without running a constant background poller.

## Scheduled Background Refresh

Jane also runs a slow scheduled refresh at midnight Central time. Each run sorts linked members by Discord ID and refreshes one shard out of seven, so a full stored-link refresh is spread across a week instead of hitting Roblox every day for everyone.

The scheduled refresh is designed to be lower priority than normal Jane work:

- it uses stored Jane links only
- it does not call RoVer for missing links
- Roblox calls run through Jane's lowest-priority Roblox queue
- between members, Jane waits while shared task budgets are busy

Defaults:

- `janeIdentityScheduledRefreshEnabled = True`
- `janeIdentityScheduledRefreshShardCount = 7`
- `janeIdentityScheduledRefreshHourCentral = 0`
- `janeIdentityScheduledRefreshMinuteCentral = 0`
- `janeIdentityScheduledRefreshFetchMembers = True`
- `janeIdentityScheduledRefreshMaxMembers = 5000`
- `janeIdentityScheduledRefreshDelaySec = 0.50`
- `janeIdentityScheduledRefreshPauseWhenBusy = True`
- `janeIdentityScheduledRefreshBusyPollSec = 30`

## Lookup Behavior

`robloxUsers.fetchRobloxUser` now prefers stored Jane/internal links before trying RoVer. RoVer remains as a fallback for users who have not linked through Jane yet.

## Identity API

Jane exposes a small read-only API for sibling bots like John:

- `GET /api/identity/discord/{discordId}`
- `GET /api/identity/roblox/{robloxId}/discord`

Both endpoints require:

```http
Authorization: Bearer JANE_IDENTITY_API_TOKEN
```

Discord lookups return Jane's linked Roblox user ID and username. Roblox reverse lookups return the linked Discord IDs Jane knows about. A missing identity returns `404` with `found: false`, so callers can fall back to RoVer during the transition.
