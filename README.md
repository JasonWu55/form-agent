# Form Agent — 問卷 AI Agent

A **conversational survey agent**. It runs a survey by chatting with the
respondent — asking the questions **one at a time** in natural language,
validating each answer, remembering progress across turns — and when the survey is
complete it **appends the response as a row in a Google Sheet**.

Front-end is a full-screen **terminal chat (TUI)**. One small Python service, built
on **[Agno](https://docs.agno.com) v2** with an OpenAI-compatible LLM. No backend
API, no MCP server, no database to run — answers land directly in your Sheet.

```
┌──────────────────────────────┐
│  Textual chat TUI            │  user bubbles + streaming bot bubbles
└──────────────┬───────────────┘
               │  agent.arun(text, session_id, stream=True)
┌──────────────▼──────────────────────────────────────┐
│  Agno Agent                                          │
│   • model: OpenAILike(base_url, api_key)  ← env      │
│   • db: SqliteDb  (conversation + answers persist)   │
│   • instructions built from survey.yaml              │
│   • tools: record_answer()  ·  submit_survey() ──────┼──► gspread ──► Google Sheet
└──────────────────────────────────────────────────────┘
```

---

## Features

- **Conversational, one-question-at-a-time** flow driven by the LLM.
- **Config-driven survey** — define questions in `agent/survey.yaml` (no code).
  Supports `text`, `int` (with `min`/`max`), and `choice` (with `options`) types,
  required/optional, bilingual prompts (繁中 / English).
- **Answer validation** — out-of-range numbers, invalid choices, and missing
  required answers are caught and re-asked.
- **Fresh session each run** — every TUI launch starts a brand-new survey; the
  session (conversation + partial answers) is deleted when you close the TUI.
- **Streaming TUI** — the bot's reply types in live.
- **Writes straight to Google Sheets** — one row per completed survey, with a
  `timestamp` column plus one column per question.

---

## Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) (or Docker).
- An **OpenAI-compatible LLM endpoint** that supports **function/tool calling**
  (e.g. OpenAI `gpt-4o-mini`, or any compatible gateway). Tool calling is required —
  a chat-only model will talk but never record answers.
- A **Google account** + a **Google Cloud project** (free) to create a service
  account, and a Google Sheet to write into.

---

## 1. Google Sheets setup (one-time)

The agent authenticates as a **service account** and writes to a Sheet you own.

1. **Create / pick a Google Cloud project** — <https://console.cloud.google.com>.
2. **Enable the Google Sheets API** for that project
   (*APIs & Services → Library → Google Sheets API → Enable*).
3. **Create a service account** (*IAM & Admin → Service Accounts → Create*), then
   **add a JSON key** (*Keys → Add key → Create new key → JSON*) and download it.
   The file contains a `client_email` like
   `form-agent@my-project.iam.gserviceaccount.com`.
4. **Create the target Google Sheet.** Copy its **ID** from the URL —
   `https://docs.google.com/spreadsheets/d/`**`<THIS_IS_THE_ID>`**`/edit` — into
   `GOOGLE_SHEET_ID`.
5. **⚠️ Share the Sheet with the service account.** Open the Sheet → *Share* → paste
   the SA's `client_email` → give it **Editor**. **This is the #1 forgotten step** —
   skip it and writes fail with `SpreadsheetNotFound` / 403 even though login worked.

You do **not** need to add a header row — the agent writes it on first submit.

---

## 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`:

| Variable | Required | Description |
|---|---|---|
| `OPENAI_BASE_URL` | yes | LLM endpoint, e.g. `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | yes | API key for the endpoint |
| `LLM_MODEL` | yes | Model id, e.g. `gpt-4o-mini` (must support tool calling) |
| `GOOGLE_SHEET_ID` | yes | Spreadsheet ID from the Sheet URL |
| `GOOGLE_WORKSHEET` | no | Worksheet/tab name; defaults to the first worksheet |
| `GOOGLE_SA_JSON` | one of | The **whole** service-account JSON, inline. Single-quote it so the `private_key` newlines survive. |
| `GOOGLE_SA_KEY_PATH` | one of | Path to the JSON key file (used only if `GOOGLE_SA_JSON` is empty) |
| `SURVEY_PATH` | no | Path to the survey YAML (default `agent/survey.yaml`) |
| `AGENT_DB_PATH` | no | SQLite session store (default `data/agent.db`) |

Provide the service-account key **either** inline:

```dotenv
GOOGLE_SA_JSON='{"type":"service_account","project_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n","client_email":"...","...":"..."}'
```

**or** as a file path (`GOOGLE_SA_KEY_PATH=/path/to/service_account.json`).

---

## 3. Run

### Local (recommended for a TUI)

```bash
uv sync                      # install deps into .venv
uv run python -m agent       # launch the chat TUI
```

The agent greets you, asks the first question, and walks through the survey. Answer
in the input box at the bottom; the bot's replies stream in. When you finish, it
writes a row to your Sheet and confirms. Press `Ctrl+C` (or `Ctrl+Q`) to quit — your
progress is saved.

### Docker

The TUI is interactive, so use `run` (not `up`) with a TTY:

```bash
docker compose build
docker compose run --rm form-agent
```

### Web terminal (quarantine)

Expose the TUI through a **browser terminal** ("web SSH") inside a locked-down
container — useful for letting someone run the survey without shell/host access.
Uses [`ttyd`](https://github.com/tsl0922/ttyd); the box is non-root, read-only
root fs, all Linux capabilities dropped, `no-new-privileges`, with pids/memory/CPU
caps and an isolated network (only the web port is reachable; outbound stays open
so the agent can reach the LLM + Sheets).

```bash
# 1) build the base app image first (Dockerfile.web extends it)
docker compose build
# 2) set WEB_USER / WEB_PASS / WEB_PORT in .env (use a STRONG password)
# 3) build + start the quarantine box
docker compose -f docker-compose.web.yml up -d --build
```

Open `http://HOST:${WEB_PORT}` (default `7681`), enter the basic-auth
`WEB_USER` / `WEB_PASS`, then type **`tui`** to start the survey. Stop it with
`docker compose -f docker-compose.web.yml down`.

> Notes — the image bundles the **amd64** ttyd binary (`ttyd.x86_64`); on an arm64
> host change it to `ttyd.aarch64` in `Dockerfile.web`. There's **no TLS** — front
> it with a reverse proxy if it's exposed beyond a trusted network. The shell is a
> normal `bash`; the container (not the shell) is the quarantine boundary.

---

## 4. Customize the survey

Edit `agent/survey.yaml` — no code changes needed. Each question becomes one column
in the Sheet (keyed by `id`).

```yaml
title: 顧客滿意度問卷 / Customer Satisfaction Survey
description: 您的回饋將協助我們改善服務（約 1 分鐘）。
worksheet: Responses          # optional; overrides GOOGLE_WORKSHEET
questions:
  - id: name                  # → column header "name"
    prompt: 請問如何稱呼您？ / How may we address you?
    type: text
    required: true

  - id: rating
    prompt: 對本次整體服務的滿意度（1–5）？
    type: int
    min: 1
    max: 5
    required: true

  - id: recommend
    prompt: 您會推薦給朋友嗎？
    type: choice
    options: ["會 / Yes", "不會 / No"]
    required: true

  - id: comments
    prompt: 還有其他建議嗎？（可略過）
    type: text
    required: false
```

**Question fields**

| Field | Applies to | Notes |
|---|---|---|
| `id` | all | Unique; becomes the Sheet column header |
| `prompt` | all | What the agent asks |
| `type` | all | `text` · `int` · `choice` |
| `required` | all | `true` / `false` (default `true`) |
| `min` / `max` | `int` | Inclusive bounds (optional) |
| `options` | `choice` | List of allowed answers |

The Sheet's header row is `timestamp` + every question `id`, written once.

---

## Project structure

```
form-agent/
├── agent/
│   ├── survey.yaml      # the survey definition (edit this)
│   ├── survey.py        # load + validate survey.yaml; answer validation
│   ├── sheets.py        # gspread: auth, open sheet, append a response row
│   ├── agent.py         # build the Agno agent + record_answer / submit_survey tools
│   ├── tui.py           # Textual chat UI with streaming
│   └── __main__.py      # entrypoint (uv run python -m agent)
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── Dockerfile.web         # web-terminal (ttyd) image, extends the app image
├── docker-compose.web.yml # quarantine box exposing the TUI over a browser terminal
├── .env.example
└── README.md
```

---

## How it works

1. On startup the survey YAML is loaded and validated, and its questions are baked
   into the agent's instructions.
2. The agent asks the next **unanswered** question. Your reply is validated against
   that question's type/constraints; on success the agent calls `record_answer`,
   which stores it in the session state (persisted to SQLite).
3. When every **required** question is answered, the agent calls `submit_survey`,
   which builds a row (`timestamp` + answers in question order) and appends it to the
   Google Sheet via `gspread`.
4. Each launch uses a fresh, random session id, so every run starts a clean survey.
   When you close the TUI, that session (history + any partial answers) is deleted
   from the SQLite store — only the submitted rows in your Google Sheet remain.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `SpreadsheetNotFound` or 403 on submit | The Sheet isn't shared with the SA `client_email` (setup step 5), or `GOOGLE_SHEET_ID` is wrong. |
| `insufficient authentication scopes` | The SA key/scope is wrong; ensure the Sheets API is enabled for the project. |
| Agent chats but never records / submits | The model doesn't support **tool calling**. Use a function-calling model. |
| `GOOGLE_SA_JSON` parse error | Newlines in `private_key` got mangled — single-quote the value in `.env`, or use `GOOGLE_SA_KEY_PATH` instead. |
| Survey won't start, YAML error | `agent/survey.yaml` is malformed (duplicate `id`, `choice` without `options`, etc.). The error names the problem. |
| Want to restart a survey | Just relaunch — every run is a fresh session. |

---

## Out of scope

No MCP server, REST API, Telegram/web front-ends, RAG, multimodal, analytics
dashboards, respondent auth, or auto-creating the Sheet (you provide one). Kept
deliberately small — add these only when actually needed.
