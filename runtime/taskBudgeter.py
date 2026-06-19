from __future__ import annotations

import asyncio
import contextvars
import heapq
import itertools
from collections import deque
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable

import config
from . import taskStats


@dataclass
class FeatureStats:
    name: str
    limit: int
    waiting: int
    inFlight: int
    completed: int
    failed: int
    avgLatencyMs: float
    avgQueueWaitMs: float


class _FeatureBudget:
    def __init__(self, name: str, limit: int):
        self.name = str(name)
        self.limit = max(1, int(limit))
        self._sequence = itertools.count()
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._waiting = 0
        self._inFlight = 0
        self._completed = 0
        self._failed = 0
        self._latencySamples = deque(maxlen=500)
        self._queueSamples = deque(maxlen=500)
        self._lock = asyncio.Lock()

    def _drainWaitersLocked(self) -> None:
        while self._inFlight < self.limit and self._waiters:
            _priority, _sequence, waiter = heapq.heappop(self._waiters)
            if waiter.cancelled() or waiter.done():
                continue
            self._inFlight += 1
            waiter.set_result(None)

    async def run(
        self,
        opFactory: Callable[[], Awaitable[Any]],
        *,
        priority: int = 0,
    ) -> Any:
        queuedAt = perf_counter()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        try:
            normalizedPriority = int(priority)
        except (TypeError, ValueError):
            normalizedPriority = 0

        async with self._lock:
            self._waiting += 1
            heapq.heappush(
                self._waiters,
                (normalizedPriority, next(self._sequence), waiter),
            )
            self._drainWaitersLocked()

        acquired = False
        try:
            await waiter
            acquired = True
        except BaseException:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
                if waiter.done() and not waiter.cancelled():
                    self._inFlight = max(0, self._inFlight - 1)
                else:
                    self._waiters = [
                        item for item in self._waiters if item[2] is not waiter
                    ]
                    heapq.heapify(self._waiters)
                self._drainWaitersLocked()
            raise

        startedAt = perf_counter()
        queueMs = (startedAt - queuedAt) * 1000.0
        failed = False
        try:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
                self._queueSamples.append(queueMs)
            return await opFactory()
        except Exception:
            failed = True
            raise
        finally:
            latencyMs = (perf_counter() - startedAt) * 1000.0
            async with self._lock:
                if acquired:
                    self._inFlight = max(0, self._inFlight - 1)
                self._completed += 1
                if failed:
                    self._failed += 1
                self._latencySamples.append(latencyMs)
                self._drainWaitersLocked()
            try:
                await taskStats.record(self.name, latencyMs)
            except Exception:
                pass

    async def snapshot(self) -> FeatureStats:
        async with self._lock:
            latencySamples = list(self._latencySamples)
            queueSamples = list(self._queueSamples)
            avgLatencyMs = (sum(latencySamples) / len(latencySamples)) if latencySamples else 0.0
            avgQueueMs = (sum(queueSamples) / len(queueSamples)) if queueSamples else 0.0
            return FeatureStats(
                name=self.name,
                limit=self.limit,
                waiting=self._waiting,
                inFlight=self._inFlight,
                completed=self._completed,
                failed=self._failed,
                avgLatencyMs=avgLatencyMs,
                avgQueueWaitMs=avgQueueMs,
            )


class AsyncTaskBudgeter:
    def __init__(self, limits: dict[str, int]):
        self._features: dict[str, _FeatureBudget] = {
            name: _FeatureBudget(name, limit)
            for name, limit in limits.items()
        }

    def hasFeature(self, feature: str) -> bool:
        return feature in self._features

    def _ensureFeature(self, feature: str) -> _FeatureBudget:
        budget = self._features.get(feature)
        if budget is not None:
            return budget
        budget = _FeatureBudget(feature, 1)
        self._features[feature] = budget
        return budget

    async def run(
        self,
        feature: str,
        opFactory: Callable[[], Awaitable[Any]],
        *,
        priority: int = 0,
    ) -> Any:
        return await self._ensureFeature(feature).run(opFactory, priority=priority)

    async def snapshot(self) -> dict[str, Any]:
        features: dict[str, dict[str, Any]] = {}
        waitingTotal = 0
        inFlightTotal = 0
        totalOps = 0
        weightedLatency = 0.0
        for name, feature in self._features.items():
            stats = await feature.snapshot()
            features[name] = {
                "limit": stats.limit,
                "waiting": stats.waiting,
                "inFlight": stats.inFlight,
                "pending": stats.waiting + stats.inFlight,
                "completed": stats.completed,
                "failed": stats.failed,
                "avgLatencyMs": round(stats.avgLatencyMs, 2),
                "avgQueueWaitMs": round(stats.avgQueueWaitMs, 2),
            }
            waitingTotal += stats.waiting
            inFlightTotal += stats.inFlight
            totalOps += stats.completed
            weightedLatency += stats.avgLatencyMs * stats.completed
        avgLatencyTotal = (weightedLatency / totalOps) if totalOps else 0.0
        return {
            "features": features,
            "totals": {
                "waiting": waitingTotal,
                "inFlight": inFlightTotal,
                "pending": waitingTotal + inFlightTotal,
                "completed": totalOps,
                "avgLatencyMs": round(avgLatencyTotal, 2),
            },
        }


def _limitFromConfig(key: str, default: int) -> int:
    try:
        value = int(getattr(config, key, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


_defaultBudgeter = AsyncTaskBudgeter(
    {
        "robloxApi": _limitFromConfig("runtimeBudgetRobloxConcurrency", 6),
        "sheetsIo": _limitFromConfig("runtimeBudgetSheetsConcurrency", 2),
        "sheetsInteractive": _limitFromConfig(
            "runtimeBudgetInteractiveSheetsConcurrency",
            _limitFromConfig("runtimeBudgetSheetsConcurrency", 2),
        ),
        "sheetsBackground": _limitFromConfig("runtimeBudgetBackgroundSheetsConcurrency", 1),
        "discordIo": _limitFromConfig("runtimeBudgetDiscordConcurrency", 6),
        "interactionAck": _limitFromConfig("runtimeBudgetInteractionAckConcurrency", 24),
        "backgroundJobs": _limitFromConfig("runtimeBudgetBackgroundConcurrency", 2),
    }
)


def getBudgeter() -> AsyncTaskBudgeter:
    return _defaultBudgeter


async def runBudgeted(
    feature: str,
    opFactory: Callable[[], Awaitable[Any]],
    *,
    priority: int = 0,
) -> Any:
    return await _defaultBudgeter.run(feature, opFactory, priority=priority)


_robloxPriority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "robloxApiPriority",
    default=0,
)
_discordPriority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "discordIoPriority",
    default=0,
)


def _lowPriorityRobloxPriority() -> int:
    try:
        value = int(getattr(config, "runtimeBudgetLowPriorityRobloxPriority", 50) or 50)
    except (TypeError, ValueError):
        value = 50
    return max(1, value)


def _lowestPriorityRobloxPriority() -> int:
    try:
        value = int(getattr(config, "runtimeBudgetLowestPriorityRobloxPriority", 1000) or 1000)
    except (TypeError, ValueError):
        value = 1000
    return max(_lowPriorityRobloxPriority() + 1, value)


def _interactiveDiscordPriority() -> int:
    try:
        value = int(getattr(config, "runtimeBudgetInteractiveDiscordPriority", -100) or -100)
    except (TypeError, ValueError):
        value = -100
    return min(-1, value)


def _lowPriorityDiscordPriority() -> int:
    try:
        value = int(getattr(config, "runtimeBudgetLowPriorityDiscordPriority", 50) or 50)
    except (TypeError, ValueError):
        value = 50
    return max(1, value)


def _lowestPriorityDiscordPriority() -> int:
    try:
        value = int(getattr(config, "runtimeBudgetLowestPriorityDiscordPriority", 1000) or 1000)
    except (TypeError, ValueError):
        value = 1000
    return max(_lowPriorityDiscordPriority() + 1, value)


async def runSheetsThread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await runInteractiveSheetsThread(fn, *args, **kwargs)


async def runInteractiveSheetsThread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await runThreaded("sheetsInteractive", fn, *args, **kwargs)


async def runBackgroundSheetsThread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await runThreaded("sheetsBackground", fn, *args, **kwargs)


async def runThreaded(
    feature: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return await runBudgeted(
        feature,
        lambda: asyncio.to_thread(fn, *args, **kwargs),
    )


async def runDiscord(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    return await runBudgeted("discordIo", opFactory, priority=_discordPriority.get())


async def runInteractiveDiscord(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    priority = _interactiveDiscordPriority()
    token = _discordPriority.set(priority)
    try:
        return await runBudgeted("discordIo", opFactory, priority=priority)
    finally:
        _discordPriority.reset(token)


async def runLowPriorityDiscord(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    priority = _lowPriorityDiscordPriority()
    token = _discordPriority.set(priority)
    try:
        return await runBudgeted("discordIo", opFactory, priority=priority)
    finally:
        _discordPriority.reset(token)


async def runLowestPriorityDiscord(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    priority = _lowestPriorityDiscordPriority()
    token = _discordPriority.set(priority)
    try:
        return await runBudgeted("discordIo", opFactory, priority=priority)
    finally:
        _discordPriority.reset(token)


async def runInteractionAck(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    return await runBudgeted("interactionAck", opFactory)


async def runRoblox(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    return await runBudgeted("robloxApi", opFactory, priority=_robloxPriority.get())


async def runLowPriorityRoblox(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    priority = _lowPriorityRobloxPriority()
    token = _robloxPriority.set(priority)
    try:
        return await runBudgeted("robloxApi", opFactory, priority=priority)
    finally:
        _robloxPriority.reset(token)


async def runLowestPriorityRoblox(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    priority = _lowestPriorityRobloxPriority()
    token = _robloxPriority.set(priority)
    try:
        return await runBudgeted("robloxApi", opFactory, priority=priority)
    finally:
        _robloxPriority.reset(token)


async def runBackground(opFactory: Callable[[], Awaitable[Any]]) -> Any:
    discordToken = _discordPriority.set(_lowPriorityDiscordPriority())
    robloxToken = _robloxPriority.set(_lowPriorityRobloxPriority())
    try:
        return await runBudgeted("backgroundJobs", opFactory)
    finally:
        _robloxPriority.reset(robloxToken)
        _discordPriority.reset(discordToken)
