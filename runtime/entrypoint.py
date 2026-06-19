from __future__ import annotations

import asyncio
import codecs
import logging
import os
import threading
import time
from typing import Any
from hypercorn.asyncio import serve
from hypercorn.config import Config


import discord
from dotenv import find_dotenv, load_dotenv

from quart import Quart, request, Response, abort
app = Quart("JaneAPI")

shutdown_api_event = asyncio.Event()

from API.SQLite import *
from API.EndPoints import *

from runtime import (
    errorLogging as runtimeErrorLogging,
    loggingConsole as runtimeLoggingConsole,
)


def hasUtf8Bom(filepath: str) -> bool:
    with open(filepath, "rb") as handle:
        return handle.read(3).startswith(codecs.BOM_UTF8)


def discordStartupRetryConfig(configModule: Any) -> tuple[int, float, float]:
    try:
        maxAttempts = int(getattr(configModule, "discordStartupMaxAttempts", 6) or 6)
    except (TypeError, ValueError):
        maxAttempts = 6
    try:
        baseDelaySec = float(getattr(configModule, "discordStartupRetryBaseSec", 15) or 15)
    except (TypeError, ValueError):
        baseDelaySec = 15.0
    try:
        maxDelaySec = float(getattr(configModule, "discordStartupRetryMaxDelaySec", 120) or 120)
    except (TypeError, ValueError):
        maxDelaySec = 120.0
    return max(1, maxAttempts), max(0.0, baseDelaySec), max(0.0, maxDelaySec)


def isRetryableDiscordStartupError(exc: BaseException) -> bool:
    if isinstance(exc, discord.DiscordServerError):
        return True
    if isinstance(exc, discord.HTTPException):
        try:
            status = int(getattr(exc, "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        return status in {500, 502, 503, 504}
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    moduleName = exc.__class__.__module__
    return moduleName.startswith("aiohttp.")

async def run_api():
    config = Config()
    config.bind = ["0.0.0.0:24003"]
    await serve(app, config, shutdown_trigger=shutdown_api_event.wait)



async def runBotWithStartupRetry(
    botClient: Any,
    token: str,
    *,
    configModule: Any,
) -> None:
    maxAttempts, baseDelaySec, maxDelaySec = discordStartupRetryConfig(configModule)
    attempt = 1
    while True:
        try:
            await botClient.start(token)
            return
        except discord.LoginFailure:
            raise
        except discord.PrivilegedIntentsRequired:
            raise
        except Exception as exc:
            if not isRetryableDiscordStartupError(exc) or attempt >= maxAttempts:
                raise

            delaySec = min(maxDelaySec, baseDelaySec * (2 ** (attempt - 1)))

            logging.warning(
                "Discord startup failed with retryable %s on attempt %d/%d; retrying in %.1fs.",
                exc.__class__.__name__,
                attempt,
                maxAttempts,
                delaySec,
            )

            if botClient.is_closed():
                botClient.clear()
            attempt += 1
            await asyncio.sleep(delaySec)


def runMain(
    *,
    botClient: Any,
    configModule: Any,
    singleInstanceLock: Any,
    processControlModule: Any | None,
    scriptPath: str,
) -> None:
    runtimeLoggingConsole.configureConsoleLogging(level=logging.INFO)
    generalErrorLogPath = runtimeErrorLogging.configureGeneralErrorLogging(configModule=configModule)
    runtimeErrorLogging.installGlobalExceptionHooks()
    logging.info("General error log enabled: %s", generalErrorLogPath)

    lockAcquired, lockOwnerPid = singleInstanceLock.acquire()
    if not lockAcquired:
        raise RuntimeError(
            f"Another Jane process is already running for this repo (pid={int(lockOwnerPid or 0) or 'unknown'})."
        )

    restartRequested = False
    try:
        envPath = find_dotenv(usecwd=True)
        loadedEnvironmentVariables = load_dotenv(envPath, verbose=True, override=True)
        if not loadedEnvironmentVariables:
            raise RuntimeError(".env file not correctly loaded.")
        if hasUtf8Bom(envPath):
            raise RuntimeError(".env file has a UTF-8 BOM.")
        logging.info("Loaded Environment Variables: %s", loadedEnvironmentVariables)
        logging.info("Current .env file: %s", envPath)

        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not set.")

        if processControlModule is not None:
            processControlModule.clearRestartRequest()
        apiBotClientSetter = globals().get("setApiBotClient")
        if callable(apiBotClientSetter):
            apiBotClientSetter(botClient)

        async def _run():

            APITask = asyncio.create_task(run_api())
            BOTTask = asyncio.create_task(
                runBotWithStartupRetry(botClient, token, configModule=configModule)
            )
            botClient.shutdown_api_event = shutdown_api_event
            await asyncio.gather(APITask, BOTTask)

        asyncio.run(_run())
        restartRequested = bool(
            processControlModule is not None and processControlModule.restartRequested()
        )
    finally:
        singleInstanceLock.release()

    if restartRequested and processControlModule is not None:
        logging.warning("Runtime restart requested; relaunching Jane.")
        processControlModule.relaunchCurrentProcess(scriptPath=scriptPath)
