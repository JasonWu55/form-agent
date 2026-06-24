"""Headless check for the TUI streaming worker (no terminal, no LLM).

Feeds RunContent deltas + a full-content RunCompleted event through a fake
agent and asserts the bot bubble shows the reply exactly once (i.e. the
RunCompleted full text is NOT double-counted).

Run: `uv run python test_tui_stream.py`
"""

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("OPENAI_BASE_URL", "http://x")
os.environ.setdefault("OPENAI_API_KEY", "x")

from agno.run.agent import RunCompletedEvent, RunContentEvent  # noqa: E402
from textual.widgets import Markdown  # noqa: E402

from agent.tui import Response, SurveyChatApp  # noqa: E402


class FakeAgent:
    survey = SimpleNamespace(title="Test Survey")

    async def arun(self, text, session_id=None, stream=True):
        for part in ["Hello", " there", "!"]:
            yield RunContentEvent(content=part)
        yield RunContentEvent(content="")          # empty delta, must be skipped
        yield RunCompletedEvent(content="Hello there!")  # full text, must be ignored


async def main() -> None:
    captured: list[str] = []
    orig = Markdown.update

    def cap(self, markdown):
        if isinstance(self, Response):
            captured.append(markdown)
        return orig(self, markdown)

    Markdown.update = cap
    try:
        app = SurveyChatApp(FakeAgent(), "test-session")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()   # opening (kickoff) turn
            await pilot.pause()
            opening = captured[-1]
            assert opening == "Hello there!", f"opening dup? {opening!r}"

            # a user turn
            await pilot.press(*"hi")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert captured[-1] == "Hello there!", f"reply dup? {captured[-1]!r}"
    finally:
        Markdown.update = orig

    print("OK: streamed deltas accumulate once; RunCompleted full text not duplicated")
    print("ALL TUI CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
