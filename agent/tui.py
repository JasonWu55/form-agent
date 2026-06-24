"""Textual chat TUI: streams the Agno survey agent's replies into chat bubbles.

Follows the official Textual streaming-chat pattern: a ``VerticalScroll`` log of
``Prompt`` (user) / ``Response`` (bot) ``Markdown`` bubbles plus a bottom
``Input``. Each turn mounts an empty ``Response`` bubble and an **async**
``@work`` worker streams ``agent.arun(..., stream=True)`` chunks into it via
``Markdown.update(full_text)`` (update replaces content, so we accumulate).

Async workers run on the UI event loop, so widgets are updated DIRECTLY (no
``call_from_thread``). The input is disabled while a reply streams and
re-enabled in ``finally`` so an LLM/Sheets error surfaces in-bubble, not as a
crash.

``run_tui(agent, session_id)`` is the front door used by ``agent.__main__``.
"""

from __future__ import annotations

from agno.run.agent import RunContentEvent
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown

# Neutral kickoff so the agent greets + asks Q1 on startup (its instructions
# handle the actual greeting; this is just a trigger).
KICKOFF = "請開始問卷 / Begin the survey."


class Prompt(Markdown):
    """A user message bubble."""


class Response(Markdown):
    """An agent message bubble."""

    BORDER_TITLE = "Survey"


class SurveyChatApp(App):
    """Full-screen chat that drives a survey via a streaming Agno agent."""

    AUTO_FOCUS = "Input"

    CSS = """
    Prompt {
        background: $primary 10%;
        color: $text;
        margin: 1 8 0 1;
        padding: 1 2 0 2;
        border: wide $primary;
    }
    Response {
        background: $surface;
        color: $text;
        margin: 1 1 0 8;
        padding: 1 2 0 2;
        border: wide $accent;
    }
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self, agent, session_id: str) -> None:
        super().__init__()
        self.agent = agent
        self.session_id = session_id
        self.TITLE = getattr(getattr(agent, "survey", None), "title", None) or "Survey"

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-view")
        yield Input(placeholder="Type your answer and press Enter…")
        yield Footer()

    async def on_mount(self) -> None:
        """Kick off the opening turn so the agent greets and asks Q1."""
        self.query_one(Input).disabled = True
        response = Response()
        await self.query_one("#chat-view", VerticalScroll).mount(response)
        response.anchor()
        self.stream_reply(KICKOFF, response)

    @on(Input.Submitted)
    async def on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        await chat_view.mount(Prompt(text))
        response = Response()
        await chat_view.mount(response)
        response.anchor()
        self.stream_reply(text, response)

    @work(exclusive=True)
    async def stream_reply(self, user_text: str, response_widget: Response) -> None:
        """Stream one agent turn into ``response_widget`` (runs on the UI loop)."""
        self.query_one(Input).disabled = True
        content = ""
        try:
            async for ev in self.agent.arun(
                user_text, session_id=self.session_id, stream=True
            ):
                # Only accumulate streamed content deltas. Other events
                # (RunCompleted, tool calls) also carry `.content`, and
                # RunCompleted holds the FULL text — counting it would
                # duplicate the whole reply.
                if isinstance(ev, RunContentEvent) and ev.content:
                    content += ev.content
                    response_widget.update(content)
            if not content:
                response_widget.update("_(no response)_")
        except Exception as e:  # surface errors in-bubble instead of crashing
            response_widget.update(f"⚠️ {e}")
        finally:
            input_widget = self.query_one(Input)
            input_widget.disabled = False
            input_widget.focus()


def run_tui(agent, session_id: str) -> None:
    """Construct and run the survey chat TUI (blocks until the user quits)."""
    SurveyChatApp(agent, session_id).run()
