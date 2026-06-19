from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import discord

from runtime import helpMenu as runtimeHelpMenu
from runtime import taskStats


async def handleJaneHelp(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False
    if not message.guild or not isinstance(message.author, discord.Member):
        return False

    token = router.firstLowerToken(message.content or "")
    if token != ":)help":
        return False

    await router._deleteSourceIfManageable(message)

    sections = router.helpCommands.buildHelpSections(
        router.botClient.tree,
        guild=message.guild,
    )
    if bool(getattr(router.config, "temporaryCommandLockEnabled", False)):
        allowedIds = sorted(router.permissions.getTemporaryCommandAllowedUserIds())
        restrictionText = (
            "Temporary command lock is ON. Most commands are restricted to: "
            + (", ".join(f"`{userId}`" for userId in allowedIds) if allowedIds else "`(none configured)`")
        )
        if sections:
            overviewSection = dict(sections[0])
            overviewItems = list(overviewSection.get("items") or [])
            overviewItems.insert(
                0,
                {
                    "name": "Temporary Command Lock",
                    "description": restrictionText,
                    "permission": "Applies bot-wide until the rollout lock is disabled.",
                },
            )
            overviewSection["items"] = overviewItems
            sections[0] = overviewSection

    view = runtimeHelpMenu.HelpMenuView(
        openerId=int(message.author.id),
        helpCommandsModule=router.helpCommands,
        sections=sections,
        currentSectionKey="overview",
    )
    await message.channel.send(
        embed=view.buildEmbed(),
        view=view,
        allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
    )
    return True


async def handleJaneRuntime(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "?janeruntime":
        return False

    if not message.guild or not isinstance(message.author, discord.Member):
        return False

    member = message.author
    allowed = (
        member.id == message.guild.owner_id
        or member.guild_permissions.manage_guild
        or member.guild_permissions.administrator
        or router.hasCohostPermission(member)
    )
    if not allowed:
        try:
            await message.channel.send("You do not have permission to use this command.")
        except Exception:
            pass
        return True

    await router._deleteSourceIfManageable(message)

    now = datetime.now(timezone.utc)
    uptime = router.formatUptime(now - router.botStartedAt)
    startedAt = router.discordTimestamp(router.botStartedAt, "s")
    loop = asyncio.get_running_loop()

    def taskState(task: asyncio.Task | None) -> str:
        if task is None:
            return "not started"
        if task.cancelled():
            return "cancelled"
        if task.done():
            return "done"
        return "running"

    embed = discord.Embed(
        title="Jane Runtime",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    embed.add_field(name="Ping", value=f"{round(router.botClient.latency * 1000)} ms", inline=True)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Started", value=startedAt, inline=False)
    embed.add_field(name="Guilds", value=str(len(router.botClient.guilds)), inline=True)
    embed.add_field(name="Users Cached", value=str(len(router.botClient.users)), inline=True)
    embed.add_field(name="Cogs", value=str(len(router.botClient.cogs)), inline=True)
    nowUtc = datetime.now(timezone.utc)
    weeklyHour, weeklyMinute, weeklyWeekday = router.orbatWeeklyScheduleConfig()
    nextWeekly = router.maintenance.nextWeeklyRunAfter(
        nowUtc,
        weeklyHour,
        weeklyMinute,
        weeklyWeekday,
    )
    autoRecruitmentPayout = bool(router.maintenance.automaticRecruitmentPayoutEnabled())
    nextPayoutText = (
        router.discordTimestamp(router.maintenance.nextRecruitmentPayoutRun(nowUtc), "s")
        if autoRecruitmentPayout
        else "manual-only (disabled)"
    )
    embed.add_field(
        name="Background Tasks",
        value=(
            f"startupMaintenance: `{taskState(router.maintenance.startupMaintenanceTask)}`\n"
            f"globalOrbatUpdate: `{taskState(router.maintenance.globalOrbatUpdateTask)}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Next Scheduled Checks",
        value=(
            f"weeklyOrbat: {router.discordTimestamp(nextWeekly, 's')}\n"
            f"recruitmentPayout: {nextPayoutText}"
        ),
        inline=False,
    )
    gitStats = {}
    if router.gitUpdateCoordinator is not None:
        try:
            gitStats = dict(router.gitUpdateCoordinator.getStats())
        except Exception:
            gitStats = {}
    if gitStats:
        lastCheckAt = str(gitStats.get("lastCheckAt") or "").strip()
        lastError = str(gitStats.get("lastError") or "").strip()
        if len(lastError) > 180:
            lastError = lastError[:177] + "..."
        gitLines = [
            f"enabled: `{bool(gitStats.get('enabled'))}`",
            f"worker: `{'running' if bool(gitStats.get('workerRunning')) else 'stopped'}`",
            f"lastCheck: {router._formatIsoTimestampOrNever(lastCheckAt)}",
            f"last updated: {router._formatIsoTimestampOrNever(gitStats.get('lastUpdateAt'))}",
            f"result: `{str(gitStats.get('lastResult') or 'idle')}`",
            f"behind: `{int(gitStats.get('lastBehindCount') or 0)}`",
        ]
        if lastError:
            gitLines.append(f"error: `{lastError}`")
        embed.add_field(
            name="Git Updater",
            value="\n".join(gitLines),
            inline=False,
        )
    budgetSnapshot = await router.taskBudgeter.getBudgeter().snapshot()
    budgetTotals = budgetSnapshot.get("totals", {}) if isinstance(budgetSnapshot, dict) else {}
    featureStats = budgetSnapshot.get("features", {}) if isinstance(budgetSnapshot, dict) else {}
    queueTelemetry = router.sessionViews.getRuntimeQueueTelemetry()
    pendingBackgroundTasks = (
        int(queueTelemetry.get("bgQueueUpdateActiveTasks", 0))
        + int(queueTelemetry.get("sessionUpdateActiveTasks", 0))
        + int(queueTelemetry.get("bgQueueRepostActiveTasks", 0))
    )
    embed.add_field(
        name="Background Job Telemetry",
        value=(
            f"queueDepth: `{int(budgetTotals.get('waiting', 0))}`\n"
            f"pendingTasks: `{int(budgetTotals.get('pending', 0)) + pendingBackgroundTasks}`\n"
            f"avgOpLatency: `{float(budgetTotals.get('avgLatencyMs', 0.0)):.2f} ms`"
        ),
        inline=False,
    )
    if isinstance(featureStats, dict) and featureStats:
        lines: list[str] = []
        for featureName in sorted(featureStats.keys()):
            stats = featureStats.get(featureName)
            if not isinstance(stats, dict):
                continue
            lines.append(
                f"{featureName}: q={int(stats.get('waiting', 0))} "
                f"in={int(stats.get('inFlight', 0))} "
                f"lat={float(stats.get('avgLatencyMs', 0.0)):.1f}ms"
            )
        if lines:
            embed.add_field(
                name="Budgeted Features",
                value="\n".join(lines[:8]),
                inline=False,
            )
    taskAverageRows = await taskStats.snapshot()
    if taskAverageRows:
        topRows = sorted(
            taskAverageRows,
            key=lambda row: int(row.get("amount", 0) or 0),
            reverse=True,
        )[:6]
        lines = [
            f"{row.get('name')}: avg={float(row.get('timeMs', 0.0) or 0.0):.1f}ms n={int(row.get('amount', 0) or 0)}"
            for row in topRows
        ]
        statsPath = str(getattr(router.config, "runtimeTaskStatsPath", "runtime/data/task-stats.json") or "").strip()
        if statsPath:
            lines.append(f"file: `{statsPath}`")
        embed.add_field(
            name="Task Averages",
            value="\n".join(lines[:7]),
            inline=False,
        )
    if isinstance(router.maintenance.lastConfigSanitySummary, dict):
        warningCount = int(router.maintenance.lastConfigSanitySummary.get("warningCount", 0) or 0)
        errorCount = int(router.maintenance.lastConfigSanitySummary.get("errorCount", 0) or 0)
        embed.add_field(
            name="Config Sanity",
            value=f"errors: `{errorCount}` | warnings: `{warningCount}`",
            inline=True,
        )
    embed.add_field(
        name="Runtime",
        value=f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} | discord.py {discord.__version__}",
        inline=False,
    )
    processResources = router.getProcessResourceSnapshot(now)
    embed.add_field(
        name="Process Resources",
        value=(
            f"pid: `{processResources['pid']}`\n"
            f"cpu(avg): `{processResources['cpuPercent']}`\n"
            f"ram(rss): `{processResources['rss']}`\n"
            f"threads: `{processResources['threads']}`"
        ),
        inline=False,
    )
    dbSizeMb = 0.0
    try:
        dbSizeMb = router._dbPath().stat().st_size / (1024 * 1024)
    except Exception:
        dbSizeMb = 0.0
    embed.add_field(name="DB Size", value=f"{dbSizeMb:.2f} MB", inline=True)
    embed.add_field(
        name="Loop Time",
        value=f"{loop.time():.2f}",
        inline=True,
    )

    sentViaWebhook = await router.sendRuntimeWebhookMessage(message, embed)
    if not sentViaWebhook:
        await message.channel.send(embed=embed)
    return True


def _canUseViewAllChannels(router: Any, member: discord.Member) -> bool:
    return bool(
        member.guild_permissions.administrator
        or router._headDeveloperAllowed(int(member.id))
    )


def _sortedGuildChannels(guild: discord.Guild) -> list[discord.abc.GuildChannel]:
    return sorted(
        list(guild.channels),
        key=lambda channel: (
            0 if isinstance(channel, discord.CategoryChannel) else 1,
            int(getattr(channel, "position", 0) or 0),
            str(getattr(channel, "name", "") or "").lower(),
            int(getattr(channel, "id", 0) or 0),
        ),
    )


async def handleViewAllChannels(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!viewallchannels":
        return False

    if not message.guild or not isinstance(message.author, discord.Member):
        return False

    guild = message.guild
    member = message.author
    if not _canUseViewAllChannels(router, member):
        try:
            await message.channel.send(
                "Administrator or Jane developer access required.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass
        return True

    await router._deleteSourceIfManageable(message)

    if not message.role_mentions:
        await message.channel.send(
            "Usage: `!viewallchannels @role`",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    role = message.role_mentions[0]
    if role.is_default():
        await message.channel.send(
            "I will not apply this to `@everyone`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    me = guild.me
    if me is None:
        await message.channel.send(
            "I could not find my server member state.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    if not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
        await message.channel.send(
            "I need Manage Roles permission to edit channel role overwrites.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    status = await message.channel.send(
        f"Opening visible-channel access for `{role.name}`...",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    changed = 0
    skipped = 0
    failed = 0
    channels = _sortedGuildChannels(guild)
    reason = f"View all channels command by {member} ({member.id})"

    for index, channel in enumerate(channels, start=1):
        overwrite = channel.overwrites_for(role)
        if overwrite.view_channel is True:
            skipped += 1
            continue

        overwrite.view_channel = True
        try:
            await router.taskBudgeter.runLowPriorityDiscord(
                lambda channel=channel, overwrite=overwrite: channel.set_permissions(
                    role,
                    overwrite=overwrite,
                    reason=reason,
                )
            )
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        except Exception:
            failed += 1

        if index % 10 == 0:
            try:
                await router.taskBudgeter.runLowPriorityDiscord(
                    lambda: status.edit(
                        content=(
                            f"Opening visible-channel access for `{role.name}`... "
                            f"`{index}/{len(channels)}` checked."
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                )
            except Exception:
                pass
        await asyncio.sleep(0.25)

    try:
        await router.taskBudgeter.runLowPriorityDiscord(
            lambda: status.edit(
                content=(
                    f"Done. `{role.name}` can now view more channels.\n"
                    f"Updated: `{changed}` | Already allowed: `{skipped}` | Failed: `{failed}`"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        )
    except Exception:
        pass
    return True


async def handleJaneTerminal(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!janeterminal":
        return False

    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    allowedUserId = router._janeTerminalAllowedUserId()
    if allowedUserId <= 0 or int(message.author.id) != allowedUserId:
        await router._deleteSourceIfManageable(message)
        return True

    await router._deleteSourceIfManageable(message)

    terminalContent = router._buildJaneTerminalContent()
    sentViaWebhook = await router.sendTerminalWebhookMessage(message, terminalContent)
    if not sentViaWebhook:
        await message.channel.send(terminalContent)
    return True


async def handleShutdown(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!shutdown":
        return False

    if not router._shutdownAllowed(int(message.author.id)):
        return True

    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    await router._deleteSourceIfManageable(message)

    try:
        await message.channel.send(
            "Shutting down Jane.",
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
    except Exception:
        pass

    await router.botClient.close()
    return True


async def handleAllowServer(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!allowserver":
        return False

    if not router._shutdownAllowed(int(message.author.id)):
        return True

    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    await router._deleteSourceIfManageable(message)

    status = str(router.allowGuildForCommands(int(message.guild.id)) or "invalid").strip().lower()
    if status == "already":
        response = "This server is already in Jane's allowed guild list."
    elif status == "runtime-only":
        response = "Added this server for the current runtime, but Jane could not persist it into settings/core.py."
    elif status == "added":
        response = "Added this server to Jane's allowed guild list."
    else:
        response = "Jane could not add this server to the allowed guild list."

    try:
        await message.channel.send(
            response,
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
    except Exception:
        pass
    return True


async def handleMirrorTrainingHistory(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!mirrortraininghistory":
        return False

    if not router._shutdownAllowed(int(message.author.id)):
        return True

    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    if router.trainingLogCoordinator is None:
        try:
            await message.channel.send(
                "Training history mirror is unavailable on this build.",
                allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
            )
        except Exception:
            pass
        return True

    await router._deleteSourceIfManageable(message)

    try:
        await message.channel.send(
            "Running the training history mirror now.",
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
    except Exception:
        pass

    try:
        succeeded, response = await router.trainingLogCoordinator.runManualMirrorBackfillOnce(
            userId=int(message.author.id),
        )
    except Exception as exc:
        response = f"Training history mirror failed: `{exc.__class__.__name__}`"
        succeeded = False

    try:
        await message.channel.send(
            response,
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
    except Exception:
        pass
    return True
