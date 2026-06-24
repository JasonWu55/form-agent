"""Agno v2 survey agent: model/db/session-state + the two function tools.

``build_agent(survey)`` wires an Agno ``Agent`` that runs the given survey as a
chat: it asks the next unanswered question one at a time, validates each reply
via :mod:`agent.survey`, records valid answers into ``session_state["answers"]``,
and on completion appends a row to Google Sheets via :mod:`agent.sheets`.

Agno v2 API points used (confirmed against agno 2.6.x docs):
  - ``from agno.models.openai.like import OpenAILike`` (the ``.like`` submodule).
  - ``from agno.db.sqlite import SqliteDb`` -> ``db=`` (not ``storage=``).
  - ``session_state`` + ``add_session_state_to_context=True``; instructions may
    interpolate ``{answers}`` from session_state.
  - Function tools take ``run_context: RunContext`` (``from agno.run``) first and
    read/write ``run_context.session_state`` (reassign the dict key to persist).
  - Streaming for the TUI: ``agent.arun(msg, session_id=sid, stream=True)``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai.like import OpenAILike
from agno.run import RunContext

from agent import sheets
from agent.survey import Survey, validate_answer


def new_session_id() -> str:
    """A fresh session id per launch (each TUI start is a new survey)."""
    return uuid.uuid4().hex


def assert_db_writable(db_path: str | None = None) -> None:
    """Fail LOUDLY if the session DB path can't be written.

    Agno catches DB read/write errors and silently falls back to a fresh
    session, so a non-writable DB shows up as the TUI looping on Q1 (no history
    or session_state ever persists) rather than as an error. This probes the real
    SQLite file up front so a permissions/mount problem surfaces clearly — the
    common cause in Docker is a tmpfs/volume that the non-root user can't write
    (give it mode=1777 or chown it).
    """
    import sqlite3

    path = Path(db_path or os.getenv("AGENT_DB_PATH", "data/agent.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS _write_probe(x)")
            con.execute("INSERT INTO _write_probe VALUES (1)")
            con.execute("DROP TABLE _write_probe")
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as e:
        st = path.parent.stat()
        raise RuntimeError(
            f"Cannot write the session database at {str(path)!r}: {e}. "
            f"uid={os.getuid()} gid={os.getgid()} cwd={os.getcwd()} "
            f"parent={path.parent} parent_mode={oct(st.st_mode & 0o7777)}. "
            "In Docker this usually means the mount holding the DB isn't writable "
            "by the non-root user — give that tmpfs/volume mode=1777 (or chown it)."
        ) from e


def _build_instructions(survey: Survey) -> str:
    """Render the survey into an instruction string (questions in order + rules)."""
    lines: list[str] = []
    lines.append(f"You are running a survey titled: {survey.title}")
    if survey.description:
        lines.append(f"Survey description: {survey.description}")
    lines.append("")
    lines.append("The survey questions, IN ORDER:")
    for i, q in enumerate(survey.questions, 1):
        parts = [
            f"{i}. id={q.id!r}",
            f"prompt={q.prompt!r}",
            f"type={q.type}",
            ("required" if q.required else "optional"),
        ]
        if q.type == "int":
            bounds = []
            if q.min is not None:
                bounds.append(f"min={q.min}")
            if q.max is not None:
                bounds.append(f"max={q.max}")
            if bounds:
                parts.append("(" + ", ".join(bounds) + ")")
        elif q.type == "choice" and q.options:
            parts.append("options=" + " | ".join(q.options))
        lines.append("   " + ", ".join(parts))
    lines.append("")
    lines.append("Already-collected answers (do NOT re-ask these): {answers}")
    lines.append("")
    lines.append(
        "Rules:\n"
        "- Greet briefly using the survey title/description, then immediately ask "
        "the FIRST unanswered question.\n"
        "- Ask the NEXT unanswered question ONE at a time, in the order above. "
        "Never re-ask a question whose id is already a key in {answers}.\n"
        "- When the user replies, call the `record_answer` tool with that "
        "question's exact `id` and the user's raw answer text. Do NOT validate "
        "yourself; the tool validates.\n"
        "- EXCEPTION for `choice` questions: do NOT pass the user's raw words. "
        "Infer which option they mean from their reply (in any language/phrasing) "
        "and pass the EXACT canonical option string from that question's options "
        "list as the answer. If the reply is genuinely ambiguous (could match more "
        "than one option), ask a brief clarifying question instead of guessing.\n"
        "- If `record_answer` returns a string starting with 'ERROR:', relay the "
        "message to the user in plain language and re-ask the same question.\n"
        "- Optional questions may be skipped: if the user clearly declines, call "
        "`record_answer` with an empty answer to record the skip.\n"
        "- When every REQUIRED question has been recorded, briefly recap the "
        "collected answers, then call the `submit_survey` tool.\n"
        "- If `submit_survey` returns 'ERROR:', relay it and ask the missing "
        "question(s). On success, thank the user and confirm their response was "
        "recorded.\n"
        "- Keep every reply short and friendly."
    )
    return "\n".join(lines)


def build_agent(survey: Survey) -> Agent:
    """Build the Agno survey agent for ``survey`` (model/db/state/tools/instructions)."""
    db_path = os.getenv("AGENT_DB_PATH", "data/agent.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    model = OpenAILike(
        id=os.environ["LLM_MODEL"],
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )

    # Map id -> Question so the tools (which close over `survey`) can look up fast.
    by_id = {q.id: q for q in survey.questions}
    total = len(survey.questions)
    required_ids = [q.id for q in survey.questions if q.required]

    def record_answer(run_context: RunContext, question_id: str, answer: str) -> str:
        """Validate and record one survey answer.

        Args:
            question_id: The exact `id` of the survey question being answered.
            answer: The user's raw answer text (may be empty to skip an optional
                question).

        Returns:
            A short progress string on success, or a string starting with
            'ERROR:' if the question id is unknown or the answer is invalid (in
            which case you should relay the message and re-ask).
        """
        q = by_id.get(question_id)
        if q is None:
            return f"ERROR: unknown question id {question_id!r}."
        try:
            value = validate_answer(q, answer)
        except ValueError as e:
            return f"ERROR: {e}"

        state = run_context.session_state
        if state is None:
            state = run_context.session_state = {}
        answers = state.get("answers") or {}
        answers[question_id] = value
        state["answers"] = answers  # reassign the key to persist
        return f"Recorded {question_id}. {len(answers)}/{total} answered."

    def submit_survey(run_context: RunContext) -> str:
        """Submit the completed survey (append a row to Google Sheets).

        Checks that every REQUIRED question has been recorded, then appends the
        response. Returns a confirmation string, or a string starting with
        'ERROR:' if required answers are missing or the write fails.
        """
        state = run_context.session_state or {}
        answers = state.get("answers") or {}

        missing = [qid for qid in required_ids if qid not in answers]
        if missing:
            return "ERROR: still missing required answers for: " + ", ".join(missing)

        worksheet = survey.worksheet or os.getenv("GOOGLE_WORKSHEET")
        if not sheets.sheet_is_configured():
            state["completed"] = True
            return (
                "Survey recorded (DRY RUN: Google Sheets is not configured, so no "
                "row was written). Thank the user and confirm completion."
            )
        try:
            sheets.append_response(answers, survey.ids, worksheet=worksheet)
        except RuntimeError as e:
            return f"ERROR: could not write to the response sheet: {e}"

        state["completed"] = True
        return "Survey submitted and recorded successfully."

    return Agent(
        model=model,
        db=SqliteDb(db_file=db_path),
        session_state={"answers": {}},
        add_session_state_to_context=True,
        add_history_to_context=True,
        num_history_runs=10,
        instructions=_build_instructions(survey),
        tools=[record_answer, submit_survey],
        markdown=True,
    )


if __name__ == "__main__":
    # Offline tool-logic self-check (no LLM / no network).
    # `uv run python -m agent.agent`
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent.survey import load_survey

    os.environ.setdefault("LLM_MODEL", "gpt-4o-mini")
    os.environ.setdefault("OPENAI_BASE_URL", "http://x")
    os.environ.setdefault("OPENAI_API_KEY", "x")

    s = load_survey("agent/survey.yaml")
    agent = build_agent(s)
    print("built", type(agent).__name__, "tools:", [t.__name__ for t in agent.tools])

    # Pull the closures back out of the built agent to exercise them directly.
    tools = {t.__name__: t for t in agent.tools}
    record_answer = tools["record_answer"]
    submit_survey = tools["submit_survey"]

    ctx = SimpleNamespace(session_state={"answers": {}})

    # invalid int -> ERROR, nothing stored
    out = record_answer(ctx, "rating", "9")
    assert out.startswith("ERROR:"), out
    assert "rating" not in ctx.session_state["answers"], ctx.session_state
    print("OK: invalid answer rejected ->", out)

    # unknown id -> ERROR
    assert record_answer(ctx, "nope", "x").startswith("ERROR:")
    print("OK: unknown question id rejected")

    # submit with missing required -> ERROR
    out = submit_survey(ctx)
    assert out.startswith("ERROR: still missing"), out
    print("OK: premature submit rejected ->", out)

    # valid answers stored (cleaned values)
    assert record_answer(ctx, "name", "  Ada  ").startswith("Recorded name.")
    assert record_answer(ctx, "rating", "4").startswith("Recorded rating.")
    assert record_answer(ctx, "recommend", "會 / Yes").startswith("Recorded recommend.")
    assert ctx.session_state["answers"] == {
        "name": "Ada",
        "rating": 4,
        "recommend": "會 / Yes",
    }, ctx.session_state["answers"]
    print("OK: valid answers stored cleaned ->", ctx.session_state["answers"])

    # submit -> append_response called with ordered ids; capture payload
    captured = {}

    def fake_append(answers, question_ids, worksheet=None):
        captured["answers"] = dict(answers)
        captured["question_ids"] = list(question_ids)
        captured["worksheet"] = worksheet

    with patch.object(sheets, "append_response", fake_append), patch.object(
        sheets, "sheet_is_configured", lambda: True
    ):
        out = submit_survey(ctx)
    assert "successfully" in out, out
    assert captured["question_ids"] == s.ids, captured["question_ids"]
    assert captured["answers"]["rating"] == 4, captured
    assert ctx.session_state.get("completed") is True
    print("OK: submit called append_response with ordered ids ->", captured["question_ids"])
    print("OK: worksheet passed ->", captured["worksheet"])

    # dry-run path when sheets not configured: no crash, marks completed
    ctx2 = SimpleNamespace(
        session_state={"answers": {"name": "Bo", "rating": 5, "recommend": "不會 / No"}}
    )
    with patch.object(sheets, "sheet_is_configured", lambda: False):
        out = submit_survey(ctx2)
    assert "DRY RUN" in out, out
    assert ctx2.session_state.get("completed") is True
    print("OK: dry-run submit (sheets unconfigured) ->", out[:40], "...")

    # write-probe: passes on a writable file path; raises (uid-independently) when
    # the path can't be opened as a db (here: the path IS a directory).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert_db_writable(os.path.join(d, "probe.db"))
        try:
            assert_db_writable(d)
            raise AssertionError("write-probe should have failed for an unwritable db path")
        except RuntimeError:
            pass
    print("OK: db write-probe (writable passes; bad path raises loudly)")

    print("ALL TOOL CHECKS PASSED")
