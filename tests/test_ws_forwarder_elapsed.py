import asyncio

from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, AgentStarted
from src.web.ws_forwarder import WSForwarder


def test_finished_frame_includes_elapsed_ms() -> None:
    async def run() -> dict | None:
        bus = EventBus()
        fwd = WSForwarder(bus=bus)
        await fwd.start()
        q = fwd.register(session_uuid="sess-1")
        await bus.publish(
            AgentStarted(request_id="r1", chat_id=1, topic_id=-1, session_uuid="sess-1")
        )
        await bus.publish(
            AgentFinished(
                request_id="r1",
                chat_id=1,
                topic_id=-1,
                session_uuid="sess-1",
                response_text="ok",
                usage={"input_tokens": 1},
            )
        )
        frame = None
        while not q.empty():
            f = q.get_nowait()
            if f.get("type") == "finished":
                frame = f
        fwd.unregister(session_uuid="sess-1", queue=q)
        await fwd.stop()
        return frame

    frame = asyncio.run(run())
    assert frame is not None
    assert "elapsed_ms" in frame
    assert isinstance(frame["elapsed_ms"], int)
    assert frame["elapsed_ms"] >= 0
