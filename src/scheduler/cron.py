"""Built-in cron/interval scheduler for periodic Claude jobs."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import structlog

from src.claude.session import SessionStatus
from src.config.settings import CronJobSettings, Settings

logger = structlog.get_logger()

_INTERVAL_TOKEN_RE = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)


class ScheduleExpression(Protocol):
    """Schedule that can compute the next run after a given datetime."""

    def next_after(self, after: datetime) -> datetime:
        """Return the first run strictly after *after*."""


@dataclass(frozen=True)
class IntervalSchedule:
    """Simple fixed interval schedule."""

    delta: timedelta

    def next_after(self, after: datetime) -> datetime:
        return after + self.delta


@dataclass(frozen=True)
class CronExpressionSchedule:
    """Five-field cron schedule."""

    minute_values: frozenset[int]
    hour_values: frozenset[int]
    day_values: frozenset[int]
    month_values: frozenset[int]
    weekday_values: frozenset[int]
    day_is_wildcard: bool
    weekday_is_wildcard: bool

    def next_after(self, after: datetime) -> datetime:
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=366)

        while candidate <= limit:
            if self._matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)

        raise ValueError("No matching cron occurrence found within one year")

    def _matches(self, value: datetime) -> bool:
        if value.minute not in self.minute_values:
            return False
        if value.hour not in self.hour_values:
            return False
        if value.month not in self.month_values:
            return False

        cron_weekday = (value.weekday() + 1) % 7
        day_match = value.day in self.day_values
        weekday_match = cron_weekday in self.weekday_values

        if self.day_is_wildcard and self.weekday_is_wildcard:
            return True
        if self.day_is_wildcard:
            return weekday_match
        if self.weekday_is_wildcard:
            return day_match
        return day_match or weekday_match


@dataclass
class CronJobRuntime:
    """Runtime representation of a configured cron job."""

    config: CronJobSettings
    schedule: ScheduleExpression
    next_run_at: datetime | None = None
    running: bool = False


def parse_schedule(expression: str) -> ScheduleExpression:
    """Parse either a cron expression or a simple interval expression."""
    expr = expression.strip()
    lowered = expr.lower()

    if lowered.startswith(("every ", "interval ", "@every ")):
        return _parse_interval_schedule(expr)

    parts = expr.split()
    if len(parts) == 5 and all(parts):
        try:
            return _parse_cron_schedule(parts)
        except ValueError:
            # Fall through to interval parsing so we can still surface a
            # meaningful error for interval-like inputs.
            pass

    return _parse_interval_schedule(expr)


def _parse_interval_schedule(expression: str) -> IntervalSchedule:
    expr = expression.strip().lower()
    for prefix in ("@every ", "every ", "interval "):
        if expr.startswith(prefix):
            expr = expr.removeprefix(prefix)
            break

    matches = list(_INTERVAL_TOKEN_RE.finditer(expr))
    if not matches:
        raise ValueError(f"Invalid interval schedule: {expression!r}")

    compact = re.sub(r"\s+", "", expr)
    if "".join(match.group(0).replace(" ", "") for match in matches) != compact:
        raise ValueError(f"Invalid interval schedule: {expression!r}")

    total = timedelta()
    for match in matches:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if amount <= 0:
            raise ValueError(f"Interval must be positive: {expression!r}")
        if unit == "s":
            total += timedelta(seconds=amount)
        elif unit == "m":
            total += timedelta(minutes=amount)
        elif unit == "h":
            total += timedelta(hours=amount)
        elif unit == "d":
            total += timedelta(days=amount)
        else:
            raise ValueError(f"Unsupported interval unit: {unit!r}")

    return IntervalSchedule(delta=total)


def _parse_cron_schedule(parts: list[str]) -> CronExpressionSchedule:
    minute_values, _ = _parse_cron_field(parts[0], 0, 59)
    hour_values, _ = _parse_cron_field(parts[1], 0, 23)
    day_values, day_wildcard = _parse_cron_field(parts[2], 1, 31)
    month_values, _ = _parse_cron_field(parts[3], 1, 12)
    weekday_values, weekday_wildcard = _parse_cron_field(parts[4], 0, 7, allow_weekday=True)

    return CronExpressionSchedule(
        minute_values=frozenset(minute_values),
        hour_values=frozenset(hour_values),
        day_values=frozenset(day_values),
        month_values=frozenset(month_values),
        weekday_values=frozenset(weekday_values),
        day_is_wildcard=day_wildcard,
        weekday_is_wildcard=weekday_wildcard,
    )


def _parse_cron_field(
    field: str,
    minimum: int,
    maximum: int,
    *,
    allow_weekday: bool = False,
) -> tuple[set[int], bool]:
    token = field.strip()
    if token in {"*", "?"}:
        return set(range(minimum, maximum + 1)), True

    values: set[int] = set()
    wildcard = False

    for segment in token.split(","):
        segment = segment.strip()
        if not segment:
            raise ValueError(f"Invalid cron field: {field!r}")

        base = segment
        step = 1
        if "/" in segment:
            base, step_text = segment.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError(f"Invalid cron step: {segment!r}")

        if base in {"*", "?"}:
            start, end = minimum, maximum
            wildcard = True
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = int(start_text)
            end = int(end_text)
        else:
            start = end = int(base)

        if allow_weekday:
            start = 0 if start == 7 else start
            end = 0 if end == 7 else end

        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Cron field out of range: {field!r}")

        for value in range(start, end + 1, step):
            normalized = 0 if allow_weekday and value == 7 else value
            if normalized < minimum or normalized > maximum:
                raise ValueError(f"Cron field out of range: {field!r}")
            values.add(normalized)

    if allow_weekday and 7 in values:
        values.remove(7)
        values.add(0)

    return values, wildcard


@dataclass
class _PreparedJob:
    runtime: CronJobRuntime
    next_run_at: datetime | None = None
    running: bool = False


class CronScheduler:
    """Built-in asyncio scheduler for periodic Claude jobs."""

    def __init__(
        self,
        bot: Any,
        settings: Settings,
        session_manager: Any,
        claude_bridge: Any,
        streamer: Any,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_manager = session_manager
        self.claude_bridge = claude_bridge
        self.streamer = streamer
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._running_tasks: set[asyncio.Task] = set()
        self._jobs: list[_PreparedJob] = []
        for job in settings.cron_jobs:
            if not job.enabled:
                continue
            try:
                schedule = parse_schedule(job.schedule)
            except Exception as exc:
                logger.error(
                    "cron_job_invalid",
                    job=job.name,
                    schedule=job.schedule,
                    error=str(exc),
                )
                continue
            self._jobs.append(
                _PreparedJob(
                    runtime=CronJobRuntime(
                        config=job,
                        schedule=schedule,
                    )
                )
            )

    @property
    def enabled(self) -> bool:
        return self.settings.cron_scheduler_enabled and bool(self._jobs)

    async def start(self) -> None:
        """Start the background scheduler loop."""
        if not self.enabled:
            logger.info(
                "cron_scheduler_disabled",
                enabled=self.settings.cron_scheduler_enabled,
                jobs=len(self._jobs),
            )
            return
        if self._task is not None:
            return

        now = datetime.now()
        for job in self._jobs:
            job.next_run_at = job.runtime.schedule.next_after(now)

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("cron_scheduler_started", jobs=len(self._jobs))

    async def stop(self) -> None:
        """Stop the scheduler and cancel any active runs."""
        if self._stop_event is not None:
            self._stop_event.set()

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        running_tasks = list(self._running_tasks)
        self._running_tasks.clear()
        for task in running_tasks:
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        logger.info("cron_scheduler_stopped")

    async def run_job(
        self,
        job: CronJobSettings | CronJobRuntime,
        *,
        now: datetime | None = None,
    ) -> None:
        """Execute a single configured job immediately."""
        runtime = self._resolve_runtime(job)
        current_time = now or datetime.now()
        chat_id = runtime.runtime.config.chat_id or self.settings.get_default_chat_id()
        if chat_id is None:
            raise ValueError("cron job requires telegram.chat_id or a per-job chat_id")

        session = self.session_manager.get_session(runtime.runtime.config.topic_id)
        project_path = self._resolve_project_path(runtime.runtime.config, session)
        project_name = self._resolve_project_name(runtime.runtime.config, session, project_path)

        if session is None:
            session = self.session_manager.create_session(
                runtime.runtime.config.topic_id,
                project_path,
                project_name,
            )

        self.session_manager.set_status(runtime.runtime.config.topic_id, SessionStatus.WORKING)

        state = None
        error_text: str | None = None
        usage_data: dict[str, Any] | None = None

        try:
            state = await self.streamer.create_log_message(
                chat_id=chat_id,
                topic_id=runtime.runtime.config.topic_id,
            )

            async for event in self.claude_bridge.send_message(
                message=runtime.runtime.config.prompt,
                topic_id=runtime.runtime.config.topic_id,
                project_path=project_path,
                session_id=getattr(session, "session_id", None),
            ):
                event_name = _event_name(event)
                metadata = getattr(event, "metadata", None) or {}
                content = getattr(event, "content", "") or ""

                if event_name == "INIT":
                    session_id = metadata.get("session_id")
                    if session_id:
                        self.session_manager.update_session_id(
                            runtime.runtime.config.topic_id,
                            session_id,
                        )
                    continue

                if event_name == "TEXT":
                    await self.streamer.stream_response(state, content)
                    continue

                if event_name == "USAGE":
                    usage_data = metadata.get("usage") or usage_data
                    session_id = metadata.get("session_id")
                    if session_id:
                        self.session_manager.update_session_id(
                            runtime.runtime.config.topic_id,
                            session_id,
                        )
                    result_text = metadata.get("result")
                    if result_text and hasattr(self.streamer, "stream_response"):
                        await self.streamer.stream_response(state, str(result_text))
                    continue

                if event_name == "COMPLETE":
                    usage_data = metadata.get("usage") or usage_data
                    session_id = metadata.get("session_id")
                    if session_id:
                        self.session_manager.update_session_id(
                            runtime.runtime.config.topic_id,
                            session_id,
                        )
                    continue

                if event_name == "ERROR":
                    error_text = content or "Scheduled job failed."
                    await self.streamer.stream_response(state, f"❌ {error_text}")

            if usage_data and isinstance(usage_data, dict) and hasattr(self.session_manager, "add_usage"):
                self.session_manager.add_usage(
                    runtime.runtime.config.topic_id,
                    usage_data.get("input_tokens", 0),
                    usage_data.get("output_tokens", 0),
                    cache_read_tokens=usage_data.get("cache_read_tokens", 0),
                    cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
                    cost_usd=usage_data.get("cost_usd", 0.0),
                )

            if not error_text:
                self.session_manager.set_status(runtime.runtime.config.topic_id, SessionStatus.DONE)
        except Exception as exc:
            error_text = str(exc)
            self.session_manager.set_status(runtime.runtime.config.topic_id, SessionStatus.ERROR)
            logger.error(
                "cron_job_failed",
                job=runtime.runtime.config.name,
                topic_id=runtime.runtime.config.topic_id,
                error=error_text,
                elapsed=str(datetime.now() - current_time),
            )
            if state is not None:
                try:
                    await self.streamer.stream_response(state, f"❌ {error_text}")
                except Exception:
                    pass
        finally:
            if state is not None:
                cumulative = (
                    self.session_manager.get_usage(runtime.runtime.config.topic_id)
                    if hasattr(self.session_manager, "get_usage")
                    else None
                )
                try:
                    await self.streamer.finalize(
                        state,
                        show_token_usage=getattr(self.settings.display, "show_token_usage", True),
                        show_context_usage=getattr(self.settings.display, "show_context_usage", False),
                        usage=usage_data,
                        cumulative=cumulative,
                        send_empty_completion=not error_text,
                    )
                except Exception as exc:
                    logger.debug("cron_job_finalize_failed", error=str(exc))

        logger.info(
            "cron_job_finished",
            job=runtime.runtime.config.name,
            topic_id=runtime.runtime.config.topic_id,
            project=project_name,
            error=bool(error_text),
        )

    async def _run_loop(self) -> None:
        assert self._stop_event is not None

        try:
            while not self._stop_event.is_set():
                now = datetime.now()
                next_due: datetime | None = None

                for job in self._jobs:
                    if job.next_run_at is None:
                        job.next_run_at = job.runtime.schedule.next_after(now)

                    if job.next_run_at <= now:
                        if job.running:
                            job.next_run_at = job.runtime.schedule.next_after(now)
                            continue

                        job.running = True
                        job.next_run_at = job.runtime.schedule.next_after(now)
                        task = asyncio.create_task(self._run_job_task(job))
                        self._running_tasks.add(task)
                        task.add_done_callback(self._running_tasks.discard)
                        continue

                    if next_due is None or job.next_run_at < next_due:
                        next_due = job.next_run_at

                sleep_for = 60.0 if next_due is None else max(
                    0.0,
                    (next_due - now).total_seconds(),
                )

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("cron_scheduler_loop_failed", error=str(exc))

    async def _run_job_task(self, job: _PreparedJob) -> None:
        try:
            await self.run_job(job.runtime.config)
        finally:
            job.running = False

    def _resolve_runtime(self, job: CronJobSettings | CronJobRuntime) -> _PreparedJob:
        if isinstance(job, CronJobRuntime):
            for runtime in self._jobs:
                if runtime.runtime is job:
                    return runtime
            return _PreparedJob(runtime=job)

        for runtime in self._jobs:
            if runtime.runtime.config is job or runtime.runtime.config == job:
                return runtime
        return _PreparedJob(runtime=CronJobRuntime(config=job, schedule=parse_schedule(job.schedule)))

    def _resolve_project_path(
        self,
        job: CronJobSettings,
        session: Any | None,
    ) -> str:
        if job.project_path:
            return str(Path(job.project_path).expanduser())

        if session is not None:
            session_path = getattr(session, "project_path", "")
            if session_path:
                return str(Path(session_path).expanduser())

        project_paths = self.settings.get_project_paths()
        if project_paths:
            return str(project_paths[0].expanduser())

        raise ValueError("cron job requires a project_path or at least one configured project")

    def _resolve_project_name(
        self,
        job: CronJobSettings,
        session: Any | None,
        project_path: str,
    ) -> str:
        if job.project_name:
            return job.project_name

        if session is not None:
            session_name = getattr(session, "project_name", "")
            if session_name:
                return session_name

        return Path(project_path).name


def _event_name(event: Any) -> str:
    kind = getattr(event, "type", None)
    if kind is None:
        return ""
    if hasattr(kind, "name"):
        return str(kind.name)
    return str(kind).upper()
