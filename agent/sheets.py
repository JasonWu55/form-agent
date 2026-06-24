"""Google Sheets writer (gspread v6).

Authenticates with a service account from env, opens the target sheet by key,
ensures a header row idempotently, and appends a survey response as a row.

Credentials come ONLY from the environment (never hardcoded/logged):
  - GOOGLE_SA_JSON       full service-account JSON blob, OR
  - GOOGLE_SA_KEY_PATH   path to a mounted JSON key file
  - GOOGLE_SHEET_ID      spreadsheet id (from the sheet URL)
  - GOOGLE_WORKSHEET     optional tab name; defaults to the first worksheet
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound

_SETUP_HINT = (
    "Set GOOGLE_SA_JSON (the full service-account JSON blob) or "
    "GOOGLE_SA_KEY_PATH (path to the JSON key file), plus GOOGLE_SHEET_ID. "
    "See the README 'Google Sheets setup' section."
)


def sheet_is_configured() -> bool:
    """True if creds + sheet id are present (lets callers detect a dry-run env)."""
    has_creds = bool(os.environ.get("GOOGLE_SA_JSON")) or bool(
        os.environ.get("GOOGLE_SA_KEY_PATH")
    )
    return has_creds and bool(os.environ.get("GOOGLE_SHEET_ID"))


def _client() -> gspread.Client:
    sa_json = os.environ.get("GOOGLE_SA_JSON")
    if sa_json:
        try:
            info = json.loads(sa_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"GOOGLE_SA_JSON is not valid JSON: {e}. {_SETUP_HINT}"
            ) from e
        return gspread.service_account_from_dict(info)

    key_path = os.environ.get("GOOGLE_SA_KEY_PATH")
    if key_path and os.path.isfile(key_path):
        return gspread.service_account(filename=key_path)

    raise RuntimeError(f"No Google service-account credentials found. {_SETUP_HINT}")


def get_worksheet(worksheet: str | None = None) -> gspread.Worksheet:
    """Return the target gspread Worksheet, raising clear, actionable errors."""
    gc = _client()

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError(f"GOOGLE_SHEET_ID is not set. {_SETUP_HINT}")

    name = worksheet or os.environ.get("GOOGLE_WORKSHEET")
    try:
        sh = gc.open_by_key(sheet_id)
        return sh.worksheet(name) if name else sh.sheet1
    except SpreadsheetNotFound as e:
        raise RuntimeError(
            f"Spreadsheet '{sheet_id}' not found or not accessible. Share the Sheet "
            "with the service account's client_email as Editor (README step 4)."
        ) from e
    except APIError as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 403:
            raise RuntimeError(
                f"Permission denied opening spreadsheet '{sheet_id}'. Share the Sheet "
                "with the service account's client_email as Editor (README step 4)."
            ) from e
        raise RuntimeError(f"Google Sheets API error: {e}") from e


def append_response(
    answers: dict,
    question_ids: list[str],
    worksheet: str | None = None,
) -> None:
    """Append one response row; ensure the header row exists exactly once."""
    ws = get_worksheet(worksheet)

    # ponytail: header-check + append are not atomic; fine for the single-user
    # TUI. Add a lock / batch_update only if concurrent submitters appear.
    header = ["timestamp", *question_ids]
    if ws.row_values(1) != header:
        ws.update([header], "A1")

    row = [datetime.now(timezone.utc).isoformat()]
    row += [answers.get(qid, "") for qid in question_ids]
    ws.append_row(row, value_input_option="USER_ENTERED")
