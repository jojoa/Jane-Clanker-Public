from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from db.sqlite import execute, executeReturnId, fetchAll, fetchOne

log = logging.getLogger(__name__)

RetryHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _nowUtc() -> datetime:
    return datetime.now(timezone.utc)


def _safeInt(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _jsonEncode(value: object) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        return "{}"


def _jsonDecode(value: object) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class RetryQueueCoordinator:
    def __init__(
        self,
        *,
        taskBudgeter: Any,
        pollIntervalSec: int = 6,
        initialDelaySec: int = 30,
    ) -> None:
        self.taskBudgeter = taskBudgeter
        self.pollIntervalSec = max(2, int(pollIntervalSec))
        self.initialDelaySec = max(0, int(initialDelaySec))
        self.processingLeaseSec = max(60, self.pollIntervalSec * 10)
        self.handlers: dict[str, RetryHandler] = {}
        self.workerTask: asyncio.Task | None = None
        self._workerLock = asyncio.Lock()
        self._lastRunAt: datetime | None = None

    def registerHandler(self, jobType: str, handler: RetryHandler) -> None:
        safeJobType = str(jobType or "").strip().lower()
        if not safeJobType:
            return
        self.handlers[safeJobType] = handler

    def hasHandler(self, jobType: str) -> bool:
        safeJobType = str(jobType or "").strip().lower()
        return safeJobType in self.handlers

    async def enqueue(
        self,
        *,
        jobType: str,
        payload: dict[str, Any],
        maxAttempts: int = 5,
        initialDelaySec: int = 0,
        source: str = "",
    ) -> int:
        safeJobType = str(jobType or "").strip().lower()
        if not safeJobType:
            return 0
        safeMaxAttempts = max(1, min(20, int(maxAttempts or 5)))
        runAt = _nowUtc() + timedelta(seconds=max(0, int(initialDelaySec or 0)))
        return await executeReturnId(
            """
            INSERT INTO retry_jobs (
                jobType, payloadJson, status, attempts, maxAttempts, nextAttemptAt, source
            )
            VALUES (?, ?, 'PENDING', 0, ?, ?, ?)
            """,
            (
                safeJobType,
                _jsonEncode(payload),
                safeMaxAttempts,
                runAt.isoformat(),
                str(source or "")[:80],
            ),
        )

    async def retryDeadJob(self, jobId: int) -> bool:
        row = await fetchOne(
            """
            SELECT jobId, status, maxAttempts
            FROM retry_jobs
            WHERE jobId = ?
            """,
            (int(jobId),),
        )
        if not row:
            return False
        await execute(
            """
            UPDATE retry_jobs
            SET status = 'PENDING',
                attempts = CASE
                    WHEN status = 'DEAD' THEN maxAttempts - 1
                    ELSE attempts
                END,
                nextAttemptAt = ?,
                updatedAt = ?
            WHERE jobId = ?
            """,
            (_nowUtc().isoformat(), _nowUtc().isoformat(), int(jobId)),
        )
        return True

    async def getStats(self) -> dict[str, Any]:
        rows = await fetchAll(
            """
            SELECT status, COUNT(*) AS count
            FROM retry_jobs
            GROUP BY status
            """
        )
        byStatus = {"PENDING": 0, "PROCESSING": 0, "FAILED": 0, "DEAD": 0, "DONE": 0}
        for row in rows:
            status = str(row.get("status") or "").strip().upper()
            byStatus[status] = int(row.get("count") or 0)
        return {
            "byStatus": byStatus,
            "handlers": sorted(self.handlers.keys()),
            "lastRunAt": self._lastRunAt.isoformat() if self._lastRunAt else "",
            "workerRunning": bool(self.workerTask and not self.workerTask.done()),
        }

    async def listDeadJobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        safeLimit = max(1, min(100, int(limit or 20)))
        return await fetchAll(
            """
            SELECT jobId, jobType, attempts, maxAttempts, lastError, source, updatedAt
            FROM retry_jobs
            WHERE status = 'DEAD'
            ORDER BY updatedAt DESC, jobId DESC
            LIMIT ?
            """,
            (safeLimit,),
        )

    async def _recoverStaleProcessingJobs(self) -> int:
        rows = await fetchAll(
            """
            SELECT jobId, updatedAt
            FROM retry_jobs
            WHERE status = 'PROCESSING'
            """
        )
        if not rows:
            return 0
        now = _nowUtc()
        cutoff = now - timedelta(seconds=max(60, int(self.processingLeaseSec or 60)))
        recovered = 0
        for row in rows:
            jobId = int(row.get("jobId") or 0)
            if jobId <= 0:
                continue
            rawUpdatedAt = str(row.get("updatedAt") or "").strip()
            try:
                updatedAt = datetime.fromisoformat(rawUpdatedAt)
                if updatedAt.tzinfo is None:
                    updatedAt = updatedAt.replace(tzinfo=timezone.utc)
                updatedAt = updatedAt.astimezone(timezone.utc)
            except ValueError:
                updatedAt = datetime.min.replace(tzinfo=timezone.utc)
            if updatedAt > cutoff:
                continue
            await execute(
                """
                UPDATE retry_jobs
                SET status = 'FAILED',
                    nextAttemptAt = ?,
                    lastError = ?,
                    updatedAt = ?
                WHERE jobId = ? AND status = 'PROCESSING'
                """,
                (
                    now.isoformat(),
                    "Recovered stale PROCESSING job after runtime interruption.",
                    now.isoformat(),
                    jobId,
                ),
            )
            recovered += 1
        if recovered:
            log.warning("Recovered %s stale retry queue job(s).", recovered)
        return recovered

    async def _loadDueJobs(self, *, limit: int = 5) -> list[dict[str, Any]]:
        safeLimit = max(1, min(25, int(limit or 5)))
        return await fetchAll(
            """
            SELECT jobId, jobType, payloadJson, status, attempts, maxAttempts, nextAttemptAt, source
            FROM retry_jobs
            WHERE status IN ('PENDING', 'FAILED')
              AND datetime(nextAttemptAt) <= datetime('now')
            ORDER BY datetime(nextAttemptAt) ASC, jobId ASC
            LIMIT ?
            """,
            (safeLimit,),
        )

    async def _markProcessing(self, jobId: int) -> None:
        await execute(
            """
            UPDATE retry_jobs
            SET status = 'PROCESSING',
                updatedAt = ?
            WHERE jobId = ?
            """,
            (_nowUtc().isoformat(), int(jobId)),
        )

    async def _markDone(self, jobId: int) -> None:
        await execute(
            """
            UPDATE retry_jobs
            SET status = 'DONE',
                updatedAt = ?
            WHERE jobId = ?
            """,
            (_nowUtc().isoformat(), int(jobId)),
        )

    async def _markFailed(self, job: dict[str, Any], error: Exception) -> None:
        jobId = int(job.get("jobId") or 0)
        attempts = int(job.get("attempts") or 0) + 1
        maxAttempts = max(1, int(job.get("maxAttempts") or 1))
        isDead = attempts >= maxAttempts
        delaySec = min(600, max(5, int(2 ** min(attempts, 8))))
        nextAttemptAt = (_nowUtc() + timedelta(seconds=delaySec)).isoformat()
        await execute(
            """
            UPDATE retry_jobs
            SET status = ?,
                attempts = ?,
                nextAttemptAt = ?,
                lastError = ?,
                updatedAt = ?
            WHERE jobId = ?
            """,
            (
                "DEAD" if isDead else "FAILED",
                attempts,
                nextAttemptAt,
                f"{error.__class__.__name__}: {str(error)[:380]}",
                _nowUtc().isoformat(),
                jobId,
            ),
        )

    async def processOneJob(self, job: dict[str, Any]) -> None:
        jobId = int(job.get("jobId") or 0)
        if jobId <= 0:
            return
        jobType = str(job.get("jobType") or "").strip().lower()
        handler = self.handlers.get(jobType)
        if handler is None:
            await self._markFailed(job, RuntimeError(f"no handler registered for {jobType}"))
            return

        await self._markProcessing(jobId)
        payload = _jsonDecode(job.get("payloadJson"))
        try:
            await handler(payload)
        except Exception as exc:
            await self._markFailed(job, exc)
            raise
        await self._markDone(jobId)

    async def runTick(self) -> None:
        async with self._workerLock:
            await self._recoverStaleProcessingJobs()
            jobs = await self._loadDueJobs(limit=6)
            if not jobs:
                self._lastRunAt = _nowUtc()
                return
            for job in jobs:
                try:
                    await self.taskBudgeter.runBackground(lambda job=job: self.processOneJob(job))
                except Exception:
                    jobType = str(job.get("jobType") or "").strip().lower()
                    extra = {"skipErrorMirrorDm": True} if jobType == "error-mirror-dm" else None
                    log.exception(
                        "Retry queue job failed (jobId=%s, jobType=%s).",
                        int(job.get("jobId") or 0),
                        jobType or "unknown",
                        extra=extra,
                    )
            self._lastRunAt = _nowUtc()

    async def _runLoop(self) -> None:
        if self.initialDelaySec > 0:
            await asyncio.sleep(self.initialDelaySec)
        while True:
            try:
                await self.runTick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Retry queue loop error.")
            await asyncio.sleep(self.pollIntervalSec)

    def start(self) -> None:
        if self.workerTask is not None and not self.workerTask.done():
            return
        self.workerTask = asyncio.create_task(self._runLoop())

    async def stop(self) -> None:
        if self.workerTask is None:
            return
        task = self.workerTask
        self.workerTask = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
