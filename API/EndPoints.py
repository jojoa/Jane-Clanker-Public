import json
import os
import asyncio
from asyncio import AbstractEventLoop
from typing import Any

import discord
from quart import request, abort, Response

from features.staff.sessions.viewRuntime import requestSessionMessageUpdate
from runtime.entrypoint import app
from features.staff.sessions.service import attemptClockIn

realToken = os.getenv("JANE_FLASK_API_TOKEN")
apiBotClient: discord.Client | None = None


def setApiBotClient(botClient: discord.Client | None) -> None:
    global apiBotClient
    apiBotClient = botClient

def validateToken(requestToken) -> bool:
    if requestToken is None:
        return False
    if requestToken != realToken:
        return False
    if not requestToken:
        return False
    return True


def safelyGetOrientationId(requestData) -> int:
    if requestData is None:
        return 0
    if requestData["sessionId"] is None:
        return 0
    if type(requestData["sessionId"]) != int:
        return 0
    if requestData["sessionId"] < 0:
        return 0
    return requestData["sessionId"]

def safelyGetPassword(requestData) -> str:
    if requestData is None:
        return ""
    if requestData["sessionPassword"] is None:
        return ""
    if type(requestData["sessionPassword"]) != str:
        return ""
    return requestData["sessionPassword"]

def safelyGetUserId(requestData) -> int:
    if requestData is None:
        return 0
    if requestData["sessionUserId"] is None:
        return 0
    if requestData["sessionUserId"] <= 0:
        return 0
    if type(requestData["sessionUserId"]) != int:
        return 0
    return requestData["sessionUserId"]

def safelyGetBotClient(possibleBotClient) -> discord.Client | None:
    candidate = possibleBotClient or apiBotClient
    if candidate is None:
        return None
    return candidate

@app.post("/enterOrientation")
async def enterOrientation():
    token = request.headers.get("X-API-TOKEN")
    if not validateToken(token):
        abort(code=403)

    sessionId = safelyGetOrientationId(await request.get_json())
    if sessionId == 0:
        abort(code=400)
    password = safelyGetPassword(await request.get_json())
    if password == "":
        abort(code=400)
    userId = safelyGetUserId(await request.get_json())
    if userId == 0:
        abort(code=400)

    discordBotClient = safelyGetBotClient(None)
    if discordBotClient is None:
        abort(code=418)

    clockInResult = await attemptClockIn(sessionId, userId, password)

    resultText = str(clockInResult.get("status") or "").upper()

    response = Response(status=418,response=json.dumps(
        {
            "result": resultText,
            "attendees":clockInResult.get("attendeeCount")
        }
    ))

    if resultText == "ADDED":
        try:
            await requestSessionMessageUpdate(
                bot=discordBotClient,
                sessionId=sessionId,
                delaySec=0.5
            )
        except Exception as e:
            print(f"Error occured: {repr(e)}")

        response.status_code = 200
    return response
