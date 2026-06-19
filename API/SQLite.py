import sqlite3

from db.sqlite import fetchAll
from runtime.entrypoint import app

from quart import request, make_response, abort, Response
import os
import db.sqlite as db

realToken = os.getenv("JANE_FLASK_API_TOKEN")

def validateGetRequest(requestData) -> bool:
    if requestData is None:
        return False
    if requestData["determiningFactors"] is None:
        return False
    if requestData["table"] is None:
        return False
    if requestData["requestedValues"] is None:
        return False
    if requestData["request-type"] is None:
        return False
    if requestData["request-type"] not in ("multiple","single"):
        return False
    return True

def validatePostRequest(requestData) -> bool:
    if requestData is None:
        return False
    if requestData["table"] is None:
        return False
    if requestData["requestedValues"] is None:
        return False
    if requestData["determiningFactors"] is None:
        return False
    return True

def validatePutRequest(requestData) -> bool:
    if requestData is None:
        return False
    if requestData["table"] is None:
        return False
    if requestData["requestedValues"] is None:
        return False
    return True

def validateToken(requestToken) -> bool:
    if requestToken is None:
        return False
    if requestToken != realToken:
        return False
    if not requestToken:
        return False
    return True

async def makeSingleGetRequest(requestData):
    select_param = "*"
    if requestData["requestedValues"] == "all":
        select_param = "*"
    else:
        select_param = ",".join(requestData["requestedValues"])
    determiningFactors = requestData["determiningFactors"]
    valueTable = []
    factorTable = []
    for factor,value in determiningFactors.items():
        factorTable.append(factor+" = ?")
        valueTable.append(value)
    factorString = factorTable[0]
    if len(factorTable) > 1:
        factorString = " AND ".join(factorTable)

    valueTuple = tuple(valueTable)
    returnValue = await db.fetchOne(f"""
    SELECT {select_param}
    FROM {requestData["table"]}
    WHERE {factorString}
    """, params=valueTuple)
    print(returnValue)
    return returnValue

async def makeMultipleGetRequest(requestData):
    select_param = " * "
    if requestData["requestedValues"] == "all":
        select_param = " * "
    else:
        select_param = ",".join(requestData["requestedValues"])

    determiningFactors = requestData["determiningFactors"]

    if determiningFactors == "none":
        return await db.fetchAll(f"""
        SELECT {select_param}
        FROM {requestData["table"]}
        LIMIT {requestData["limit"]}
        """)
        return None
    valueTable = []
    factorTable = []
    for factor,value in determiningFactors.items():
        factorTable.append(factor+" = ?")
        valueTable.append(value)
    try:
        factorString = factorTable[0]
    except IndexError:
        abort(code=400)
    if len(factorTable) > 1:
        factorString = " AND ".join(factorTable)

    valueTuple = tuple(valueTable)

    return await db.fetchAll(f"""
    SELECT {select_param}
    FROM {requestData["table"]}
    WHERE {factorString}
    """, params=valueTuple)


async def makeGetRequest(requestData):
    request_type = requestData["request-type"]
    if request_type == "multiple":
        return await makeMultipleGetRequest(requestData)
    return await makeSingleGetRequest(requestData)

async def makePostRequest(requestData):

    determiningFactors = requestData["determiningFactors"]
    valueTable = []
    factorTable = []
    for factor, value in determiningFactors.items():
        factorTable.append(factor + " = ?")
        valueTable.append(value)
    factorString = factorTable[0]
    if len(factorTable) > 1:
        factorString = " AND ".join(factorTable)

    replacementValues = requestData["requestedValues"]
    identifierReplacementTable = []
    valReplacementTable = []
    for identifier, val in replacementValues.items():
        identifierReplacementTable.append(identifier + " = ?")
        valReplacementTable.append(val)
    identifierReplacementString = identifierReplacementTable[0]
    if len(identifierReplacementTable) > 1:
        identifierReplacementString = ",".join(identifierReplacementTable)


    finalTuple = tuple(valReplacementTable) + tuple(valueTable)
    await db.execute(f"""
    UPDATE {requestData["table"]}
    SET {identifierReplacementString}
    WHERE {factorString}
    """,params=finalTuple)


async def makePutRequest(requestData):
    identifierTable = []
    valueTable = []
    for identifier, val in requestData["requestedValues"].items():
        identifierTable.append(identifier)
        valueTable.append(val)
    valueString = ",".join(map(str,valueTable))
    identifierTableString = identifierTable[0]
    if len(identifierTable) > 1:
        identifierTableString = ",".join(identifierTable)

    placeholders = ",".join(["?"] * len(valueTable))
    valueTuple = tuple(valueTable)
    try:
        await db.execute(f"""
            INSERT INTO {requestData["table"]}
                ({identifierTableString})
                VALUES ({placeholders})
            """,params=valueTuple)
    except sqlite3.IntegrityError:
        abort(code=400)

def createReturnTableMultiple(SQLData):
    returnData = []
    for row in SQLData:
        returnData.append(dict(row))
    return returnData

def createReturnData(SQLData,requestType):
    if requestType == "multiple":
        return createReturnTableMultiple(SQLData)
    return SQLData

@app.get("/")
def index():
    return "Jane-Clanker API v0.1a"

@app.get("/SQL")
async def sql_get():
    token = request.headers.get("X-API-TOKEN")
    requestData = await request.get_json()
    if not validateToken(token):
        abort(code=403)
    if not validateGetRequest(requestData):
        abort(code=400)
    #we have now validated the request

    try:
        SQLData = await makeGetRequest(requestData)
    except Exception as e:
        print(e)
        abort(code=418)
    if SQLData is None:
        abort(code=400)
    returnData = createReturnData(SQLData,requestData["request-type"])
    response = await make_response(returnData)
    if not response:
        abort(code=418)
    return response

@app.post("/SQL")
async def sql_post():
    token = request.headers.get("X-API-TOKEN")
    requestData = await request.get_json()
    if not validateToken(token):
        abort(code=403)
    if not validatePostRequest(requestData):
        abort(code=400)
    #we have a valid request now
    try:
        await makePostRequest(requestData)
    except Exception as e:
        print(e)
        abort(code=418)
    response = Response(status=200)
    return response

@app.put("/SQL")
async def sql_put():
    token = request.headers.get("X-API-TOKEN")
    requestData = await request.get_json()
    if not validateToken(token):
        abort(code=403)
    if not validatePutRequest(requestData):
        abort(code=400)
    try:
        await makePutRequest(requestData)
    except Exception as e:
        print(e)
        abort(code=418)
    response = Response(status=200)
    return response
