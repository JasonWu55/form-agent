"""Entrypoint: load the survey, build the agent, launch the chat TUI.

``python -m agent`` (or the ``form-agent`` console script) loads ``.env``,
resolves ``SURVEY_PATH`` (default ``agent/survey.yaml``), validates the survey
config (a bad file fails fast with a clear message), builds the Agno agent, and
runs the Textual TUI with a stable ``session_id`` so a half-finished survey
resumes on relaunch.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    survey_path = os.getenv("SURVEY_PATH", "agent/survey.yaml")

    from agent.agent import assert_db_writable, build_agent, new_session_id
    from agent.survey import load_survey

    try:
        survey = load_survey(survey_path)
    except Exception as e:
        print(f"Failed to load survey from {survey_path!r}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # Fail fast on an unwritable session DB instead of looping silently on Q1.
    try:
        assert_db_writable()
    except RuntimeError as e:
        print(f"Session database not writable: {e}", file=sys.stderr)
        raise SystemExit(1)

    agent = build_agent(survey)
    agent.survey = survey  # so the TUI can title itself from survey.title

    from agent.tui import run_tui

    # Each launch is a fresh survey; the session is wiped when the TUI closes.
    session_id = new_session_id()
    try:
        run_tui(agent, session_id)
    finally:
        try:
            agent.delete_session(session_id)
        except Exception:  # never let cleanup mask a real exit
            pass


if __name__ == "__main__":
    main()
