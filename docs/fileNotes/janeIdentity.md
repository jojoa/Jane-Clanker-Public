# Jane Identity Files

Jane Identity is split between:

- [`cogs/community/identityCog.py`](../../cogs/community/identityCog.py)
- [`features/community/identity/service.py`](../../features/community/identity/service.py)

The feature runbook lives at:

- [`docs/features/janeIdentity.md`](../features/janeIdentity.md)

## What It Handles

Jane Identity links a Discord user to a Roblox identity and then optionally applies server-specific role and nickname rules.

It supports:

- `/verify`
- `/unlink`
- `/rover`
- `/whois`
- Roblox OAuth link attempts
- relay polling for website-hosted callbacks
- connected Roblox group config
- group rank to Discord role rules
- username or display-name formatting rules
- scheduled refreshes for already-linked members

## File Split

### `identityCog.py`

This is the Discord UI layer.

It owns:

- slash commands
- modals
- dropdowns
- admin setup panels
- verification success and failure DMs
- background refresh task startup
- relay polling task startup

If a button, modal, slash command, or permission response is wrong, start here.

### `service.py`

This is the state and Roblox/OAuth layer.

It owns:

- link-attempt creation and expiry
- OAuth PKCE values
- redirect and callback URL helpers
- token exchange
- stored identity rows
- group, role, and name rules
- applying roles and nicknames to a Discord member
- API helpers used by other Jane systems
- small HTML responses for the local identity web flow

If a linked account is stored wrong, a callback fails, or roles are applied incorrectly, start here.

## Normal Link Flow

1. `/verify` asks `service.py` to create a link attempt.
2. Jane gives the user a Roblox OAuth URL.
3. Roblox redirects to Jane's configured public callback.
4. The callback is completed locally or through the relay.
5. `service.py` stores the verified identity.
6. The cog applies the member state and DMs the result when needed.

## The Relay

The relay exists because Jane may not be reachable directly from the public internet.

The public website receives the Roblox callback, stores a small pending callback payload, and Jane polls it. The relay should not become the source of truth. It is just a transport bridge back to Jane.

## Things To Be Careful About

- OAuth `state` values are security-sensitive. Do not make them predictable.
- Keep callback URLs consistent with the Roblox developer settings.
- Do not put relay tokens or OAuth secrets in public docs or config defaults.
- Role changes require a live guild member. A stored identity row alone is not enough.
- Nickname updates can fail because of Discord role hierarchy. Treat that as a normal partial failure.
- Bulk refreshes should stay rate-limit friendly.
- Service helpers are used by BG Intelligence and other systems, so avoid changing return shapes casually.

## Good Small Edits

- improve one panel or modal label
- add a clearer failure message
- add a new role-rule validation check
- tighten relay error reporting
- add a focused test around rule matching or nickname formatting
