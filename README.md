# fm-ai-chatbot

A white-label AI chat widget that lets users query a FileMaker database
using natural language, powered by Gemini function-calling and the
FileMaker Data API.

## Features

- **Guided setup wizard** — user only enters server + username +
  password first. The app then discovers:
  1. Available **databases** on that server
  2. Available **layouts** inside the chosen database
  3. The **schema** (fields + related/portal tables) of the chosen
     layout — shown as a preview before saving
- **Multi-database support** — save multiple profiles this way and
  switch between them from the sidebar.
- **Natural language queries** — Gemini decides when to call
  `get_records`, `find_records`, or `describe_schema`.
- **Schema-aware chatbot** — users can ask "what data is here?" and
  the bot answers from the cached field/table list (no extra API
  quota spent).
- **Daily quota guard** — tracks Gemini free-tier usage (default 20
  requests/day) and returns a friendly message once the limit is hit.
- **Dockerized** — one command to build and run.

## Project Structure

```
fm-ai-chatbot/
├── main.py              # FastAPI backend
├── config.json           # Multi-database + Gemini config
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── static/
    ├── index.html         # Chat widget UI
    ├── style.css
    └── chat.js
```

## Setup

Copy `config.example.json` to `config.json`, then fill in your real credentials (server URL, username, password, and Gemini API key).
### 1. Add your Gemini API key

Edit `config.json` and set your key:

```json
{
  "active_profile": null,
  "profiles": {},
  "gemini": {
    "api_key": "YOUR_GEMINI_API_KEY",
    "model": "gemini-1.5-flash",
    "daily_request_limit": 20
  }
}
```

Database profiles are **not** typed into this file by hand — they're
created through the in-app setup wizard (see below).

### 2. Connect your database via the wizard

On first launch (or clicking "+ Add New Database"), the widget walks
through:

1. **Credentials** — server URL, username, password only
2. **Database** — pick from the list the server returns
3. **Layout** — pick from the list of layouts in that database
4. **Confirm** — preview the layout's fields and related tables, name
   the profile, and save

> Create a dedicated FileMaker account with the `fmrest` extended
> privilege, and pick/create a layout that only exposes the fields you
> want the chatbot to see. Do not use a full-access account.

### 2. Run locally (without Docker)

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser.

### 3. Run with Docker

```bash
docker compose up --build
```

## API Endpoints

| Method | Endpoint                        | Description                                   |
|--------|----------------------------------|------------------------------------------------|
| GET    | `/api/health`                   | Health check                                   |
| POST   | `/api/discover/databases`       | List databases for given server + credentials  |
| POST   | `/api/discover/layouts`         | List layouts inside a chosen database          |
| POST   | `/api/discover/schema`          | Get fields + related tables for a layout       |
| POST   | `/api/profiles/save`            | Save a fully-discovered profile                |
| GET    | `/api/config/profiles`          | List all saved DB profiles                     |
| POST   | `/api/config/active/{name}`     | Switch the active DB profile                   |
| DELETE | `/api/config/profiles/{name}`   | Remove a saved profile                         |
| POST   | `/api/chat`                     | Send a chat message, get AI response           |

## Known Limitations

- **Gemini free-tier quota**: 20 requests/day by default. Adjust
  `gemini.daily_request_limit` in `config.json` if you're on a paid tier.
- **Tables vs Layouts**: FileMaker's Data API always reads from a
  *layout*, not a table. Make sure `layout` in your profile points to
  a layout name, not a table name.
- **Session tokens**: FileMaker Data API tokens expire after 15
  minutes of inactivity; the backend automatically re-logs in on a
  401 response.

## Next Steps / Ideas

- Add write support (`create_record`, `update_record`) via Gemini
  function-calling.
- Switch to the `Execute FileMaker Data API` script step for
  in-context calls that skip external token auth entirely.
- Add authentication on the widget itself (currently open CORS for
  demo purposes — restrict `allow_origins` in `main.py` for production).
