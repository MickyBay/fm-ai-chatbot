# fm-ai-chatbot

A white-label AI chat widget that lets users query one or more FileMaker
databases using natural language — powered by **either Gemini or Claude**
function-calling (user's choice) and the FileMaker Data API.

## Features

- **One-click setup on Windows** — double-click `setup_and_run.bat` and
  it creates a virtual environment, installs dependencies, creates a
  starter `config.json`, starts the server, and opens the browser
  automatically. No command line needed.
- **Bring-your-own AI key** — any user can open **AI Settings** in the
  sidebar and plug in their own **Gemini or Claude** API key directly
  from the browser. Nothing has to be hardcoded into `config.json`
  ahead of time, and switching providers is a click away.
- **Guided setup wizard** — user only enters server + username +
  password. The app then discovers the available **databases** on
  that server; the AI figures out which **layout** to use per question
  on its own, so the end user never has to see FileMaker terminology.
- **Multi-database support** — tick one *or several* saved databases
  at once from the sidebar; every question is answered across all of
  them, with results labeled by database when more than one has data.
- **Projects** — group databases per client. Each project has its own
  database list and its own chat history; switching projects starts a
  clean conversation.
- **Natural language queries, including writes** — the AI decides when
  to call `list_available_layouts`, `get_records`, `find_records`,
  `describe_schema`, and — with an explicit confirmation step first —
  `create_record`, `update_record`, or `delete_record`.
- **Schema-aware chatbot** — conversation history is sent with every
  request, so follow-ups like "show me those sorted by name" work
  without re-explaining context.
- **Daily quota guard** — tracks AI provider usage (default 20
  requests/day) and returns a friendly message once the limit is hit.
- **Dockerized** — one command to build and run, if you'd rather not
  use the `.bat` file.

## Project Structure

```
fm-ai-chatbot/
├── main.py               # FastAPI backend
├── config.json            # Projects + database profiles + AI provider settings (not committed)
├── config.example.json    # Template config.json is created from on first run
├── requirements.txt
├── setup_and_run.bat      # One-click Windows setup + run
├── Dockerfile
├── docker-compose.yml
└── static/
    ├── index.html          # Chat widget UI + setup wizard + AI settings modal
    ├── style.css
    └── chat.js
```

## Setup

### Option A — Windows, one click (recommended for non-technical users)

1. Make sure **Python 3.12** is installed (from python.org — "Add
   python.exe to PATH" must be ticked during install). Very new Python
   versions (3.14+) can fail to install some packages because
   pre-built installers don't exist for them yet.
2. Double-click **`setup_and_run.bat`**.
3. Wait for the browser to open at `http://127.0.0.1:8000`.
4. On first launch, the **AI Settings** modal opens automatically —
   choose **Gemini** or **Claude**, paste in an API key, and save.
5. The **FileMaker setup wizard** then walks through connecting a
   database (see below).

Keep the black terminal window open while using the app; closing it
stops the server.

### Option B — Manual (any OS)

```bash
cp config.example.json config.json
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser, then add your AI
provider key from **AI Settings** in the sidebar.

### Option C — Docker

```bash
cp config.example.json config.json
docker compose up --build
```

### Connecting a FileMaker database (in-app wizard)

On first launch (or clicking **+ Add New Database**), the widget walks
through:

1. **Credentials** — server URL, username, password (leave "Verify SSL
   certificate" unticked for self-signed on-premise servers)
2. **Database** — pick one or several databases the server returns
3. **Confirm** — name the profile and save; the AI figures out which
   layout to use per question automatically

> Create a dedicated FileMaker account with the `fmrest` extended
> privilege, and pick/create a layout that only exposes the fields you
> want the chatbot to see. Do not use a full-access account.

## AI Provider Settings

Open **⚙ Gemini / Claude Settings** in the sidebar at any time to:

- Switch between Gemini and Claude
- Add or replace the API key for either provider
- Optionally set a specific model name (defaults to
  `gemini-2.5-flash` for Gemini and `claude-sonnet-4-6` for Claude)

Both providers' keys are kept side by side in `config.json`, so
switching providers doesn't lose the other one's saved key.

## API Endpoints

| Method | Endpoint                              | Description                                              |
|--------|-----------------------------------------|------------------------------------------------------------|
| GET    | `/api/health`                          | Health check                                                |
| POST   | `/api/discover/databases`              | List databases for given server + credentials               |
| POST   | `/api/discover/databases/refresh`      | Re-list databases using saved shared credentials            |
| GET    | `/api/credentials`                     | Whether shared FileMaker credentials are already saved      |
| POST   | `/api/profiles/save`                   | Save a database profile (within the active project)         |
| GET    | `/api/config/profiles`                 | List saved DB profiles + which are active                   |
| POST   | `/api/config/active`                   | Set the full list of active database(s)                     |
| DELETE | `/api/config/profiles/{name}`          | Remove a saved profile                                      |
| GET    | `/api/projects`                        | List projects                                                |
| POST   | `/api/projects`                        | Create a new project                                         |
| POST   | `/api/projects/active/{key}`           | Switch the active project                                    |
| DELETE | `/api/projects/{key}`                  | Delete a project (at least one must remain)                  |
| GET    | `/api/settings/ai`                     | Current AI provider + whether a key is saved (never the key) |
| POST   | `/api/settings/ai`                     | Save/replace the API key + model for a provider              |
| POST   | `/api/chat`                            | Send a chat message, get an AI response                      |

## Known Limitations

- **Free-tier quotas**: Gemini and Claude both have rate/usage limits
  on free tiers. Adjust `ai.daily_request_limit` in `config.json` if
  you're on a paid plan.
- **Tables vs Layouts**: FileMaker's Data API always reads from a
  *layout*, not a table. The AI discovers layout names itself via
  `list_available_layouts` — never hardcode a table name expecting it
  to work as a layout name.
- **Session tokens**: FileMaker Data API tokens expire after 15
  minutes of inactivity; the backend automatically re-logs in on a
  401 response.
- **Write operations** (`create_record`, `update_record`,
  `delete_record`) are real, non-simulated changes. The AI is
  instructed to confirm with the user before calling them, but review
  the system prompt in `main.py` if you need stricter guardrails.
