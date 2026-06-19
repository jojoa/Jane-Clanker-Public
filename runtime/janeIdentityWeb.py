from __future__ import annotations

import errno
import hmac
import logging
from typing import Any

import discord
from aiohttp import web

from features.community.identity import service as janeIdentity
from runtime import taskBudgeter

log = logging.getLogger(__name__)


class JaneIdentityWebServer:
    def __init__(self, *, configModule: Any, botClient: Any):
        self.config = configModule
        self.botClient = botClient
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.started = False

    def isEnabled(self) -> bool:
        return bool(getattr(self.config, "janeIdentityWebEnabled", False))

    def _host(self) -> str:
        return str(getattr(self.config, "janeIdentityWebHost", "127.0.0.1") or "127.0.0.1").strip()

    def _port(self) -> int:
        try:
            value = int(getattr(self.config, "janeIdentityWebPort", 8791) or 8791)
        except (TypeError, ValueError):
            value = 8791
        return max(1, value)

    async def _health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True, "service": "jane-identity"})

    @staticmethod
    def _publicPage(title: str, body: str) -> web.Response:
        return web.Response(
            text=janeIdentity.htmlPage(title, body),
            content_type="text/html",
        )

    @staticmethod
    def _callbackHandoffPage() -> web.Response:
        return web.Response(
            text=(
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<meta http-equiv=\"refresh\" content=\"2; url=https://discord.gg/\">"
                "<title>Returning to Discord</title>"
                "<style>html,body{margin:0;width:100%;height:100%;background:#fff}</style>"
                "</head><body>"
                "<script>"
                "(function(){"
                "function closeTab(){try{window.open('', '_self');window.close();}catch(error){}}"
                "closeTab();"
                "window.setTimeout(closeTab,100);"
                "window.setTimeout(function(){window.location.replace('https://discord.gg/');},900);"
                "}());"
                "</script>"
                "</body></html>"
            ),
            content_type="text/html",
        )

    @staticmethod
    def _shortText(value: object, limit: int = 1000) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return "Jane could not finish this verification attempt."
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    @staticmethod
    def _positiveInt(value: object, default: int = 0) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(default)
        return parsed if parsed > 0 else int(default)

    async def _resolveDiscordUser(self, discordUserId: int) -> discord.User | None:
        safeDiscordUserId = self._positiveInt(discordUserId)
        if safeDiscordUserId <= 0:
            return None
        user = self.botClient.get_user(safeDiscordUserId)
        if user is not None:
            return user
        try:
            return await taskBudgeter.runDiscord(lambda: self.botClient.fetch_user(safeDiscordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.info("Could not resolve Discord user %s for Jane Identity failure DM.", safeDiscordUserId)
            return None

    async def _sendVerificationFailureDm(
        self,
        result: janeIdentity.IdentityLinkResult,
        *,
        cancelled: bool = False,
    ) -> None:
        user = await self._resolveDiscordUser(result.discord_user_id)
        if user is None:
            return

        title = "Jane Identity Verification Cancelled" if cancelled else "Jane Identity Verification Failed"
        description = (
            "Roblox authorization was cancelled before Jane could link your account."
            if cancelled
            else "Jane could not finish linking your Roblox account."
        )
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.orange() if cancelled else discord.Color.red(),
        )
        embed.add_field(
            name="What happened",
            value=self._shortText(result.error),
            inline=False,
        )
        guild = self.botClient.get_guild(self._positiveInt(result.guild_id))
        if guild is not None:
            embed.add_field(name="Server", value=str(guild.name), inline=False)
        embed.add_field(
            name="Next step",
            value="Run `/verify` again in Discord when you are ready to try again.",
            inline=False,
        )
        embed.set_footer(text="Jane Identity")

        try:
            await taskBudgeter.runDiscord(
                lambda: user.send(
                    content="Jane could not finish your Roblox verification.",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM Jane Identity verification failure to Discord user %s.", result.discord_user_id)

    async def _sendVerificationSuccessDm(
        self,
        result: janeIdentity.IdentityLinkResult,
        changes: dict[str, Any],
    ) -> None:
        user = await self._resolveDiscordUser(result.discord_user_id)
        if user is None:
            return

        guild = self.botClient.get_guild(self._positiveInt(result.guild_id))
        summary = janeIdentity.formatApplySummary(changes, guild=guild)
        hasIssues = bool(changes.get("permissionIssues"))
        embed = discord.Embed(
            title="Jane Identity Verification Complete",
            description=f"Linked Roblox account: `{result.roblox_username}`",
            color=discord.Color.orange() if hasIssues else discord.Color.green(),
        )
        if guild is not None:
            embed.add_field(name="Server", value=str(guild.name), inline=False)
        embed.add_field(
            name="Update summary",
            value=self._shortText(summary),
            inline=False,
        )
        embed.set_footer(text="Jane Identity")

        content = (
            "Jane linked your Roblox account, but some Discord updates need staff attention."
            if hasIssues
            else "Jane linked your Roblox account."
        )
        try:
            await taskBudgeter.runDiscord(
                lambda: user.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM Jane Identity verification summary to Discord user %s.", result.discord_user_id)

    async def _entry(self, request: web.Request) -> web.Response:
        del request
        return self._publicPage(
            "Jane Identity",
            (
                "<p class=\"summary\">Jane Identity links a Discord account to the Roblox account authorized by that user.</p>"
                "<ul class=\"result-list\">"
                "<li>Verification starts from Discord with <code>/verify</code>.</li>"
                "<li>Roblox sign-in and consent are handled by Roblox, not by Jane.</li>"
                "<li>Jane uses the result to apply configured Discord nicknames and roles.</li>"
                "</ul>"
                "<p class=\"muted\">This page is only the public entry point for Jane Clanker's private verification service.</p>"
            ),
        )

    async def _privacy(self, request: web.Request) -> web.Response:
        del request
        return self._publicPage(
            "Jane Identity Privacy Policy",
            (
                "<p>Jane Identity stores the minimum data needed to connect Discord members with their Roblox accounts.</p>"
                "<h2>Data Jane stores</h2>"
                "<ul>"
                "<li>Discord user ID.</li>"
                "<li>Discord server ID for the server where verification was started or configured.</li>"
                "<li>Roblox user ID and Roblox username returned by Roblox OAuth.</li>"
                "<li>Jane Identity role, nickname, and group-rule configuration set by server administrators.</li>"
                "<li>Operational timestamps used for maintenance, troubleshooting, and refresh behavior.</li>"
                "</ul>"
                "<h2>How Jane uses the data</h2>"
                "<ul>"
                "<li>To verify that a Discord member controls a Roblox account.</li>"
                "<li>To apply Discord nicknames and roles based on configured server rules.</li>"
                "<li>To provide authorized internal lookup access to sibling bots operated for the same communities.</li>"
                "</ul>"
                "<h2>Sharing and sale</h2>"
                "<p>Jane Identity is not sold as a public product. Jane does not sell Roblox or Discord identity link data.</p>"
                "<h2>Removing your link</h2>"
                "<p>Users can run <code>/unlink</code> in Discord to remove their stored Roblox identity link and start a fresh verification.</p>"
                "<h2>Security</h2>"
                "<p>Jane uses Roblox OAuth for account authorization. Jane does not ask users for Roblox passwords and does not store Roblox passwords.</p>"
            ),
        )

    async def _terms(self, request: web.Request) -> web.Response:
        del request
        return self._publicPage(
            "Jane Identity Terms of Service",
            (
                "<p>Jane Identity is a private verification feature for Discord communities that use Jane Clanker.</p>"
                "<h2>Use of the service</h2>"
                "<ul>"
                "<li>Users must authorize only Roblox accounts they control.</li>"
                "<li>Server administrators may configure Jane to update Discord nicknames and roles after verification.</li>"
                "<li>Users may remove their stored link with <code>/unlink</code> in Discord.</li>"
                "</ul>"
                "<h2>Availability</h2>"
                "<p>Jane Identity is provided for internal community use and may be changed, paused, or disabled as needed for maintenance or safety.</p>"
                "<h2>Limitations</h2>"
                "<p>Jane Identity does not replace Roblox, Discord, or their account security systems. Users remain responsible for their Roblox and Discord accounts.</p>"
                "<h2>Contact</h2>"
                "<p>For questions or removal requests, contact the Discord server staff responsible for the Jane Clanker instance where verification was used.</p>"
            ),
        )

    def _apiToken(self) -> str:
        return str(getattr(self.config, "janeIdentityApiToken", "") or "").strip()

    def _apiAuthorized(self, request: web.Request) -> bool:
        expected = self._apiToken()
        if not expected:
            return False

        authHeader = str(request.headers.get("Authorization") or "").strip()
        supplied = ""
        if authHeader.lower().startswith("bearer "):
            supplied = authHeader[7:].strip()
        if not supplied:
            supplied = str(request.headers.get("X-Jane-Identity-Token") or "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _apiAuthProblem(self, request: web.Request) -> web.Response | None:
        if not self._apiToken():
            return web.json_response(
                {"ok": False, "error": "Jane Identity API token is not configured."},
                status=503,
            )
        if not self._apiAuthorized(request):
            return web.json_response({"ok": False, "error": "Unauthorized."}, status=401)
        return None

    @staticmethod
    def _queryInt(request: web.Request, name: str, default: int = 0) -> int:
        try:
            value = int(str(request.query.get(name) or default).strip())
        except (TypeError, ValueError):
            value = int(default)
        return value if value > 0 else int(default)

    async def _apiIdentityByDiscord(self, request: web.Request) -> web.Response:
        authProblem = self._apiAuthProblem(request)
        if authProblem is not None:
            return authProblem
        try:
            discordId = int(str(request.match_info.get("discordId") or "").strip())
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid Discord user ID."}, status=400)
        identity = await janeIdentity.apiIdentityByDiscordId(
            discordId,
            guildId=self._queryInt(request, "guildId"),
        )
        if identity is None:
            return web.json_response(
                {"ok": True, "found": False, "discordId": str(discordId)},
                status=404,
            )
        return web.json_response({"ok": True, "found": True, **identity})

    async def _apiDiscordByRoblox(self, request: web.Request) -> web.Response:
        authProblem = self._apiAuthProblem(request)
        if authProblem is not None:
            return authProblem
        try:
            robloxId = int(str(request.match_info.get("robloxId") or "").strip())
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Invalid Roblox user ID."}, status=400)
        identities = await janeIdentity.apiDiscordIdentitiesByRobloxId(
            robloxId,
            guildId=self._queryInt(request, "guildId"),
        )
        if not identities:
            return web.json_response(
                {"ok": True, "found": False, "robloxId": str(robloxId), "discordIds": [], "identities": []},
                status=404,
            )
        discordIds = [str(row.get("discordId") or "") for row in identities if str(row.get("discordId") or "")]
        return web.json_response(
            {
                "ok": True,
                "found": True,
                "robloxId": str(robloxId),
                "discordIds": discordIds,
                "identities": identities,
            }
        )

    async def _robloxCallback(self, request: web.Request) -> web.Response:
        oauthError = str(request.query.get("error") or "").strip()
        if oauthError:
            description = str(request.query.get("error_description") or oauthError).strip()
            result = await janeIdentity.failRobloxOAuthAttempt(
                state=str(request.query.get("state") or "").strip(),
                error=description,
            )
            await self._sendVerificationFailureDm(result, cancelled=True)
            return self._callbackHandoffPage()

        code = str(request.query.get("code") or "").strip()
        state = str(request.query.get("state") or "").strip()
        result = await janeIdentity.completeRobloxOAuth(code=code, state=state)
        if not result.ok:
            await self._sendVerificationFailureDm(result)
            return self._callbackHandoffPage()

        changes = await janeIdentity.applyMemberVerification(self.botClient, result)
        await self._sendVerificationSuccessDm(result, changes)
        return self._callbackHandoffPage()

    async def start(self) -> None:
        if self.started:
            return
        if not self.isEnabled():
            log.info("Jane Identity web callback disabled.")
            return
        if not bool(getattr(self.config, "janeIdentityEnabled", True)):
            log.info("Jane Identity web server disabled.")
            return
        problem = janeIdentity.configurationProblem()
        if problem:
            log.warning("Jane Identity OAuth callback is not fully configured: %s", problem)

        self.app = web.Application(client_max_size=32 * 1024)
        self.app.router.add_get("/", self._entry)
        self.app.router.add_get("/privacy", self._privacy)
        self.app.router.add_get("/terms", self._terms)
        self.app.router.add_get("/identity/health", self._health)
        self.app.router.add_get(janeIdentity.callbackPath(), self._robloxCallback)
        self.app.router.add_get("/api/identity/discord/{discordId}", self._apiIdentityByDiscord)
        self.app.router.add_get("/api/identity/roblox/{robloxId}/discord", self._apiDiscordByRoblox)

        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=self._host(), port=self._port())
        try:
            await self.site.start()
        except OSError as exc:
            winError = int(getattr(exc, "winerror", 0) or 0)
            errNo = int(getattr(exc, "errno", 0) or 0)
            portInUseErrnos = {int(errno.EADDRINUSE), 48, 98, 10048}
            portInUse = winError == 10048 or errNo in portInUseErrnos
            try:
                await self.runner.cleanup()
            except Exception:
                pass
            self.site = None
            self.runner = None
            self.app = None
            if portInUse:
                log.warning(
                    "Jane Identity web callback not started: %s:%s is already in use.",
                    self._host(),
                    self._port(),
                )
                return
            raise
        self.started = True
        log.info("Jane Identity web callback started on http://%s:%s", self._host(), self._port())

    async def stop(self) -> None:
        if not self.started:
            return
        try:
            if self.site is not None:
                await self.site.stop()
        finally:
            self.site = None
        try:
            if self.runner is not None:
                await self.runner.cleanup()
        finally:
            self.runner = None
            self.app = None
            self.started = False
