from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.config.settings import Settings
from src.scheduler.cron import CronScheduler, parse_schedule


class CronScheduleParserTests(unittest.TestCase):
    def test_interval_schedule_advances_by_duration(self) -> None:
        schedule = parse_schedule("every 15m")

        next_run = schedule.next_after(datetime(2026, 3, 18, 8, 30))

        self.assertEqual(next_run, datetime(2026, 3, 18, 8, 45))

    def test_cron_schedule_finds_next_matching_minute(self) -> None:
        schedule = parse_schedule("0 9 * * *")

        next_run = schedule.next_after(datetime(2026, 3, 18, 8, 30))

        self.assertEqual(next_run, datetime(2026, 3, 18, 9, 0))


class CronSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_job_streams_claude_output_into_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir) / "demo-project"
            project_path.mkdir()

            settings = Settings(
                telegram={"token": "bot-token", "chat_id": -100123},
                security={"allowed_user_ids": [1]},
                projects={"paths": [str(project_path)]},
                cron_scheduler_enabled=True,
                cron_jobs=[
                    {
                        "name": "Morning digest",
                        "schedule": "0 9 * * *",
                        "prompt": "Summarize the overnight errors.",
                        "topic_id": 321,
                    }
                ],
            )

            class FakeSession:
                def __init__(self, topic_id: int, project_path: str, project_name: str) -> None:
                    self.topic_id = topic_id
                    self.project_path = project_path
                    self.project_name = project_name
                    self.session_id: str | None = None
                    self.status = "new"
                    self.total_input_tokens = 0
                    self.total_output_tokens = 0
                    self.total_cache_read_tokens = 0
                    self.total_cache_creation_tokens = 0
                    self.total_cost_usd = 0.0

            class FakeSessionManager:
                def __init__(self) -> None:
                    self.sessions: dict[int, FakeSession] = {}

                def get_session(self, topic_id: int):
                    return self.sessions.get(topic_id)

                def has_session(self, topic_id: int) -> bool:
                    return topic_id in self.sessions

                def create_session(self, topic_id: int, project_path: str, project_name: str):
                    session = FakeSession(topic_id, project_path, project_name)
                    self.sessions[topic_id] = session
                    return session

                def update_session_id(self, topic_id: int, session_id: str | None) -> None:
                    self.sessions[topic_id].session_id = session_id

                def add_usage(
                    self,
                    topic_id: int,
                    input_tokens: int,
                    output_tokens: int,
                    cache_read_tokens: int = 0,
                    cache_creation_tokens: int = 0,
                    cost_usd: float = 0.0,
                ) -> None:
                    session = self.sessions[topic_id]
                    session.total_input_tokens += input_tokens
                    session.total_output_tokens += output_tokens
                    session.total_cache_read_tokens += cache_read_tokens
                    session.total_cache_creation_tokens += cache_creation_tokens
                    session.total_cost_usd += cost_usd

                def set_status(self, topic_id: int, status: str) -> None:
                    self.sessions[topic_id].status = status

                def get_usage(self, topic_id: int) -> dict:
                    session = self.sessions[topic_id]
                    return {
                        "total_input_tokens": session.total_input_tokens,
                        "total_output_tokens": session.total_output_tokens,
                        "total_cache_read_tokens": session.total_cache_read_tokens,
                        "total_cache_creation_tokens": session.total_cache_creation_tokens,
                        "total_cost_usd": session.total_cost_usd,
                    }

            session_manager = FakeSessionManager()

            class FakeStreamer:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, tuple, dict]] = []

                async def create_log_message(self, *, chat_id: int, topic_id: int):
                    self.calls.append(("create", (chat_id, topic_id), {}))
                    return SimpleNamespace(chat_id=chat_id, topic_id=topic_id)

                async def stream_response(self, state, text_chunk: str) -> None:
                    self.calls.append(("stream", (state.topic_id, text_chunk), {}))

                async def finalize(self, state, **kwargs) -> None:
                    self.calls.append(("finalize", (state.topic_id,), kwargs))

            class FakeBridge:
                def __init__(self) -> None:
                    self.calls: list[tuple] = []

                async def send_message(
                    self,
                    message: str,
                    topic_id: int,
                    project_path: str | Path,
                    session_id: str | None = None,
                    attachments=None,
                ):
                    self.calls.append((message, topic_id, str(project_path), session_id, attachments))
                    yield SimpleNamespace(type="INIT", metadata={"session_id": "sid-123"})
                    yield SimpleNamespace(type="TEXT", content="digest ready")
                    yield SimpleNamespace(
                        type="COMPLETE",
                        metadata={
                            "session_id": "sid-123",
                            "usage": {
                                "input_tokens": 4,
                                "output_tokens": 12,
                                "cache_read_tokens": 0,
                                "cache_creation_tokens": 0,
                                "cost_usd": 0.02,
                            },
                        },
                    )

            bot = SimpleNamespace()
            streamer = FakeStreamer()
            bridge = FakeBridge()

            scheduler = CronScheduler(
                bot=bot,
                settings=settings,
                session_manager=session_manager,
                claude_bridge=bridge,
                streamer=streamer,
            )

            job = settings.cron_jobs[0]
            await scheduler.run_job(job, now=datetime(2026, 3, 18, 8, 30))

            self.assertEqual(bridge.calls[0][0], "Summarize the overnight errors.")
            self.assertEqual(bridge.calls[0][1], 321)
            self.assertEqual(bridge.calls[0][2], str(project_path))
            self.assertEqual(streamer.calls[0], ("create", (-100123, 321), {}))
            self.assertTrue(session_manager.has_session(321))
            session = session_manager.get_session(321)
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.project_path, str(project_path))
            self.assertEqual(session.session_id, "sid-123")
            self.assertEqual(session.status, "done")
            self.assertEqual(session.total_input_tokens, 4)
            self.assertEqual(session.total_output_tokens, 12)
            self.assertEqual(session.total_cost_usd, 0.02)

    async def test_scheduler_is_disabled_when_flag_is_off(self) -> None:
        settings = Settings(
            telegram={"token": "bot-token", "chat_id": -100123},
            security={"allowed_user_ids": [1]},
            cron_scheduler_enabled=False,
            cron_jobs=[
                {
                    "name": "Disabled job",
                    "schedule": "*/5 * * * *",
                    "prompt": "This should not run.",
                    "topic_id": 999,
                }
            ],
        )
        scheduler = CronScheduler(
            bot=SimpleNamespace(),
            settings=settings,
            session_manager=SimpleNamespace(),
            claude_bridge=SimpleNamespace(),
            streamer=SimpleNamespace(),
        )

        await scheduler.start()

        self.assertIsNone(scheduler._task)


class CronSettingsTests(unittest.TestCase):
    def test_settings_load_cron_jobs_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
telegram:
  token: "bot-token"
  chat_id: -100123
security:
  allowed_user_ids:
    - 1
projects:
  paths:
    - /tmp/demo-project
cron_scheduler_enabled: true
cron_jobs:
  - name: "Morning digest"
    schedule: "0 9 * * *"
    prompt: "Summarize the overnight errors."
    topic_id: 321
""".strip()
            )

            settings = Settings.from_yaml(config_path)

        self.assertTrue(settings.cron_scheduler_enabled)
        self.assertEqual(settings.telegram.chat_id, -100123)
        self.assertEqual(len(settings.cron_jobs), 1)
        job = settings.cron_jobs[0]
        self.assertEqual(job.name, "Morning digest")
        self.assertEqual(job.schedule, "0 9 * * *")
        self.assertEqual(job.topic_id, 321)


if __name__ == "__main__":
    unittest.main()
