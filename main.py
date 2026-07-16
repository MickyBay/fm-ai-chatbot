"""
fm-ai-chatbot backend
----------------------
Flow:
  1. User enters server_url + username + password only.
  2. Backend calls FileMaker Data API to list available DATABASES.
  3. User picks database(s) and names the profile.
  4. Profile (server, db, credentials) is saved.
  5. Chatbot uses either Gemini OR Claude function-calling under the hood
     (user's own choice, set from the frontend Settings modal - see
     /api/settings/ai). The model silently calls tool functions
     (list_available_layouts, describe_schema, get_records, find_records,
     create_record, update_record, delete_record) to discover layouts and
     query data based on user questions. The user never has to select or
     deal with FileMaker layout names.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Or on Windows, just double-click setup_and_run.bat - it does all of the
above automatically (creates a virtual environment, installs packages,
and starts the server).
"""

import asyncio
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Resource paths - aware of running as a plain script VS a PyInstaller-built
# standalone .exe ("frozen"). When frozen, bundled read-only files (static/,
# config.example.json) live in a temporary extraction folder (sys._MEIPASS).
#
# User data (config.json) is ALWAYS written to a fixed location in the
# user's AppData folder - NOT next to the .exe or the script. This ensures
# the exact same config.json is used whether the app is run as a plain
# script (during development, e.g. `uvicorn main:app`) or as a built .exe
# (for end users, e.g. Sohaib double-clicking FileMaker-AI-Chatbot.exe), so
# data never silently splits across two different files depending on how
# the app happened to be launched, and it survives even if the .exe is
# moved to a different folder later.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)          # bundled, read-only resources
else:
    BASE_DIR = Path(__file__).parent

APP_DIR = Path.home() / "AppData" / "Local" / "FileMaker-AI-Chatbot"
APP_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = APP_DIR / "config.json"
SECRET_KEY_PATH = APP_DIR / "secret.key"

def get_encryption_key() -> bytes:
    if not SECRET_KEY_PATH.exists():
        key = Fernet.generate_key()
        SECRET_KEY_PATH.write_bytes(key)
        return key
    return SECRET_KEY_PATH.read_bytes()

def encrypt_value(val: str) -> str:
    if not val:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    return f.encrypt(val.encode()).decode()

def decrypt_value(val: str) -> str:
    if not val:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    try:
        return f.decrypt(val.encode()).decode()
    except Exception:
        return val

app = FastAPI(title="fm-ai-chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# AI provider registry - Sohaib wants ANY provider to be selectable, not
# just Gemini/Claude hardcoded. This list is just a set of convenient
# presets (so the frontend can offer a one-click dropdown with sane
# defaults) - it is NOT a whitelist. A user can also type in any other
# provider name (DeepSeek, Groq, a local model server, etc.) and supply
# their own base_url - see /api/settings/ai below. Almost every
# third-party LLM API today speaks the same "OpenAI-compatible" chat-
# completions + tools wire format, so that's the default api_style for
# anything not explicitly listed here.
# ---------------------------------------------------------------------------
PROVIDER_REGISTRY = {
    "gemini": {
        "label": "Google Gemini",
        "api_style": "gemini",
        "default_model": DEFAULT_GEMINI_MODEL,
        "base_url": None,  # call_gemini() builds the Google endpoint itself
    },
    "claude": {
        "label": "Anthropic Claude",
        "api_style": "claude",
        "default_model": DEFAULT_CLAUDE_MODEL,
        "base_url": None,  # call_claude() builds the Anthropic endpoint itself
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_style": "openai",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
    },
    "openai": {
        "label": "OpenAI",
        "api_style": "openai",
        "default_model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        # First run - no separate setup step needed ("end user ko kuch
        # nahi karna"): create a starter config.json next to the app
        # automatically, from the bundled template if there is one.
        example_path = BASE_DIR / "config.example.json"
        if example_path.exists():
            CONFIG_PATH.write_text(example_path.read_text())
        else:
            CONFIG_PATH.write_text(json.dumps({"projects": {}}, indent=2))

    cfg = json.loads(CONFIG_PATH.read_text())

    # --- Migration: single "active_profile" (str) -> "active_profiles" (list) ---
    if "active_profiles" not in cfg:
        old = cfg.get("active_profile")
        cfg["active_profiles"] = [old] if old else []

    # --- Migration: no shared "credentials" block yet ---
    # Zeeshan sets up the SAME username/password across every database on
    # the server, so we keep one shared credentials block instead of
    # duplicating it per profile.
    if "credentials" not in cfg:
        cfg["credentials"] = None
        for profile in cfg.get("profiles", {}).values():
            cfg["credentials"] = {
                "server_url": profile["server_url"],
                "username": profile["username"],
                "password": profile["password"],
                "verify_ssl": profile.get("verify_ssl", False),
            }
            break

    cfg.setdefault("profiles", {})

    # --- Migration: flat profiles -> Projects ---
    # Nigah wants to group databases per client: "Project A" holds
    # Client A's databases + its own chat, "Project B" holds Client B's.
    # Any existing flat "profiles"/"active_profiles" (from before Projects
    # existed) are moved into a single "Default" project so nothing is
    # lost, and everything else keeps working unchanged.
    if "projects" not in cfg:
        cfg["projects"] = {
            "default": {
                "name": "Default",
                "profiles": cfg.get("profiles", {}),
                "active_profiles": cfg.get("active_profiles", []),
            }
        }
        cfg["active_project"] = "default"
        # Keep top-level profiles/active_profiles out of the way now that
        # they live inside the project - avoids two sources of truth.
        cfg.pop("profiles", None)
        cfg.pop("active_profiles", None)
        cfg.pop("active_profile", None)

    cfg.setdefault("active_project", next(iter(cfg["projects"]), None))

    # --- Migration: single-provider "gemini" block -> multi-provider "ai" block ---
    # Sohaib wants any normal user to be able to plug in their OWN Gemini
    # OR Claude API key from the frontend (Settings), instead of it being
    # hardcoded once in config.json. This keeps both provider's settings
    # side by side so switching providers doesn't lose the other key.
    if "ai" not in cfg:
        old_gemini = cfg.get("gemini", {}) or {}
        cfg["ai"] = {
            "provider": "gemini",
            "daily_request_limit": old_gemini.get("daily_request_limit", 20),
            "providers": {
                "gemini": {
                    "api_key": old_gemini.get("api_key", ""),
                    "model": old_gemini.get("model", DEFAULT_GEMINI_MODEL),
                    "api_style": "gemini",
                    "base_url": None,
                },
            },
        }
        cfg.pop("gemini", None)

    cfg["ai"].setdefault("provider", "gemini")
    cfg["ai"].setdefault("daily_request_limit", 20)
    cfg["ai"].setdefault("providers", {})

    # --- Migration: old fixed "ai.gemini" / "ai.claude" blocks -> the new
    # open-ended "ai.providers.<any-name>" dict. This is the actual fix for
    # Sohaib's "why is this hardcoded to gemini/claude?" complaint - any
    # provider name can now live in this dict, not just these two. ---
    for legacy_key in ("gemini", "claude"):
        if legacy_key in cfg["ai"]:
            legacy_cfg = cfg["ai"].pop(legacy_key) or {}
            cfg["ai"]["providers"].setdefault(legacy_key, {
                "api_key": legacy_cfg.get("api_key", ""),
                "model": legacy_cfg.get("model") or PROVIDER_REGISTRY[legacy_key]["default_model"],
                "api_style": legacy_key,
                "base_url": None,
            })

    # Keep gemini/claude present as ready-to-use options even if never
    # configured yet, so the AI Settings dropdown always has them without
    # a special case in the frontend.
    for key in ("gemini", "claude"):
        cfg["ai"]["providers"].setdefault(key, {
            "api_key": "",
            "model": PROVIDER_REGISTRY[key]["default_model"],
            "api_style": key,
            "base_url": None,
        })
        cfg["ai"]["providers"][key].setdefault("model", PROVIDER_REGISTRY[key]["default_model"])
        cfg["ai"]["providers"][key].setdefault("api_style", key)
        cfg["ai"]["providers"][key].setdefault("base_url", None)

    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_active_project(cfg: dict) -> tuple[str, dict]:
    key = cfg.get("active_project")
    project = cfg.get("projects", {}).get(key) if key else None
    if not project:
        raise HTTPException(status_code=400, detail="No active project selected yet.")
    project.setdefault("profiles", {})
    project.setdefault("active_profiles", [])
    return key, project


def resolve_credentials(cfg: dict, server_url: str, username: str, password: str, verify_ssl: bool):
    """If a password was provided, use it as-is. Otherwise fall back to
    the shared saved credentials (shared across ALL projects, since
    Zeeshan uses the same server login everywhere), so adding another
    database doesn't require retyping the username/password every time."""
    if password:
        return server_url, username, password, verify_ssl
    creds = cfg.get("credentials")
    if not creds:
        raise HTTPException(status_code=400, detail="No saved credentials to reuse - please enter username/password.")
    return creds["server_url"], creds["username"], creds["password"], creds.get("verify_ssl", False)


def get_active_profiles(cfg: dict) -> list[tuple[str, dict]]:
    """Returns [(profile_key, profile_dict), ...] for every currently
    active/selected database WITHIN THE ACTIVE PROJECT. Raises if none
    are selected."""
    _, project = get_active_project(cfg)
    keys = project.get("active_profiles") or []
    profiles = project.get("profiles", {})
    active = [(k, profiles[k]) for k in keys if k in profiles]
    if not active:
        raise HTTPException(status_code=400, detail="No active database(s) selected yet.")
    return active


# ---------------------------------------------------------------------------
# Daily AI-provider quota tracker (shared across whichever provider is active)
# ---------------------------------------------------------------------------

_quota_state = {"date": None, "count": 0}


def check_and_increment_quota(limit: int):
    today = time.strftime("%Y-%m-%d")
    if _quota_state["date"] != today:
        _quota_state["date"] = today
        _quota_state["count"] = 0
    if _quota_state["count"] >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Daily quota ({limit} requests) reached. Try again tomorrow.",
        )
    _quota_state["count"] += 1


# ---------------------------------------------------------------------------
# Low-level FileMaker Data API helpers (used during discovery, before a
# profile / layout has even been chosen yet)
# ---------------------------------------------------------------------------

async def fm_list_databases(server_url: str, username: str, password: str, verify_ssl: bool) -> list[str]:
    url = f"{server_url.rstrip('/')}/fmi/data/vLatest/databases"
    async with httpx.AsyncClient(verify=verify_ssl, timeout=30) as client:
        resp = await client.get(url, auth=(username, password))
        resp.raise_for_status()
        databases = resp.json()["response"]["databases"]
        return [d["name"] for d in databases]


async def fm_login(server_url: str, database: str, username: str, password: str, verify_ssl: bool) -> str:
    url = f"{server_url.rstrip('/')}/fmi/data/vLatest/databases/{database}/sessions"
    async with httpx.AsyncClient(verify=verify_ssl, timeout=30) as client:
        resp = await client.post(url, json={}, auth=(username, password))
        resp.raise_for_status()
        return resp.json()["response"]["token"]


async def fm_logout(server_url: str, database: str, token: str, verify_ssl: bool) -> None:
    url = f"{server_url.rstrip('/')}/fmi/data/vLatest/databases/{database}/sessions/{token}"
    async with httpx.AsyncClient(verify=verify_ssl, timeout=15) as client:
        try:
            await client.delete(url)
        except httpx.HTTPError:
            pass  # best-effort cleanup


async def fm_list_layouts(server_url: str, database: str, token: str, verify_ssl: bool) -> list[str]:
    url = f"{server_url.rstrip('/')}/fmi/data/vLatest/databases/{database}/layouts"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(verify=verify_ssl, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        layouts = resp.json()["response"]["layouts"]

        flat = []

        def walk(items):
            for item in items:
                if "folderLayoutNames" in item:
                    walk(item["folderLayoutNames"])
                elif "name" in item:
                    flat.append(item["name"])

        walk(layouts)
        return flat


async def fm_layout_schema(server_url: str, database: str, layout: str, token: str, verify_ssl: bool) -> dict:
    """Returns field names + related table (portal) names for a layout."""
    url = f"{server_url.rstrip('/')}/fmi/data/vLatest/databases/{database}/layouts/{layout}"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(verify=verify_ssl, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()["response"]

        fields = [f["name"] for f in data.get("fieldMetaData", [])]
        related_tables = list(data.get("portalMetaData", {}).keys())

        return {"fields": fields, "related_tables": related_tables}


# ---------------------------------------------------------------------------
# FileMaker client for an already-saved profile (used during normal chat)
# ---------------------------------------------------------------------------

class FileMakerClient:
    def __init__(self, profile: dict):
        self.server_url = profile["server_url"].rstrip("/")
        self.database = profile["database"]
        self.username = profile["username"]
        self.password = profile["password"]
        self.verify_ssl = profile.get("verify_ssl", True)
        self.token: Optional[str] = None
        # Cache the layout list once fetched - the model often calls
        # list_available_layouts more than once in the same conversation
        # (or across separate chat messages, since the client itself is
        # already reused via FM_CLIENT_CACHE). Re-fetching this from the
        # FileMaker server every single time adds an avoidable network
        # round-trip on top of the Gemini/Claude round-trips, which is a
        # big part of why responses feel slow - this removes that cost
        # after the first fetch.
        self._layouts_cache: Optional[list[str]] = None

    async def _ensure_token(self):
        if not self.token:
            self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)

    async def list_layouts(self) -> list[str]:
        await self._ensure_token()
        if self._layouts_cache is None:
            self._layouts_cache = await fm_list_layouts(self.server_url, self.database, self.token, self.verify_ssl)
        return self._layouts_cache

    async def describe_layout_schema(self, layout_name: str) -> dict:
        await self._ensure_token()
        return await fm_layout_schema(self.server_url, self.database, layout_name, self.token, self.verify_ssl)

    async def get_records(self, layout_name: str, limit: int = 20) -> list[dict]:
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = (
                f"{self.server_url}/fmi/data/vLatest/databases/{self.database}"
                f"/layouts/{layout_name}/records?_limit={limit}"
            )
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = await client.get(url, headers=headers)
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()["response"]["data"]
            return [record["fieldData"] for record in data]

    async def find_records(self, layout_name: str, query: dict) -> list[dict]:
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = (
                f"{self.server_url}/fmi/data/vLatest/databases/{self.database}"
                f"/layouts/{layout_name}/_find"
            )
            headers = {"Authorization": f"Bearer {self.token}"}
            payload = {"query": [query]}
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()["response"]["data"]
            return [record["fieldData"] for record in data]

    async def _find_raw(self, layout_name: str, query: dict) -> list[dict]:
        """Same as find_records but keeps the FileMaker recordId, which
        write operations (update/delete) need to target a specific row."""
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = (
                f"{self.server_url}/fmi/data/vLatest/databases/{self.database}"
                f"/layouts/{layout_name}/_find"
            )
            headers = {"Authorization": f"Bearer {self.token}"}
            payload = {"query": [query]}
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json()["response"]["data"]

    async def create_record(self, layout_name: str, fields: dict) -> dict:
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = f"{self.server_url}/fmi/data/vLatest/databases/{self.database}/layouts/{layout_name}/records"
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = await client.post(url, headers=headers, json={"fieldData": fields})
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.post(url, headers=headers, json={"fieldData": fields})
            resp.raise_for_status()
            return {"status": "created", "fields": fields}

    async def update_record(self, layout_name: str, query: dict, fields: dict) -> dict:
        """
        Finds the record matching `query` and updates it with `fields`.
        Safety rule: only proceeds if the query matches EXACTLY ONE
        record. Zero matches or multiple matches are reported back
        instead of guessing which record to change.
        """
        matches = await self._find_raw(layout_name, query)
        if not matches:
            return {"status": "not_found", "query": query}
        if len(matches) > 1:
            return {
                "status": "multiple_matches",
                "count": len(matches),
                "sample_records": [m["fieldData"] for m in matches[:5]],
                "message": "More than one record matched - narrow down the search before updating.",
            }

        record_id = matches[0]["recordId"]
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = f"{self.server_url}/fmi/data/vLatest/databases/{self.database}/layouts/{layout_name}/records/{record_id}"
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = await client.patch(url, headers=headers, json={"fieldData": fields})
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.patch(url, headers=headers, json={"fieldData": fields})
            resp.raise_for_status()
            return {"status": "updated", "previous": matches[0]["fieldData"], "changed_fields": fields}

    async def delete_record(self, layout_name: str, query: dict) -> dict:
        """
        Finds the record matching `query` and deletes it. Same safety
        rule as update_record: only proceeds on an exact single match.
        """
        matches = await self._find_raw(layout_name, query)
        if not matches:
            return {"status": "not_found", "query": query}
        if len(matches) > 1:
            return {
                "status": "multiple_matches",
                "count": len(matches),
                "sample_records": [m["fieldData"] for m in matches[:5]],
                "message": "More than one record matched - narrow down the search before deleting.",
            }

        record_id = matches[0]["recordId"]
        await self._ensure_token()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            url = f"{self.server_url}/fmi/data/vLatest/databases/{self.database}/layouts/{layout_name}/records/{record_id}"
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = await client.delete(url, headers=headers)
            if resp.status_code == 401:
                self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)
                headers = {"Authorization": f"Bearer {self.token}"}
                resp = await client.delete(url, headers=headers)
            resp.raise_for_status()
            return {"status": "deleted", "deleted_record": matches[0]["fieldData"]}


FM_CLIENT_CACHE = {}

def get_fm_client(profile_key: str, profile: dict) -> FileMakerClient:
    """
    Caches and retrieves FileMakerClient instances to reuse session login tokens,
    preventing slow duplicate login requests on every chat turn.
    """
    cache_key = (
        profile_key,
        profile.get("server_url"),
        profile.get("database"),
        profile.get("username"),
        profile.get("password"),
        profile.get("verify_ssl", True)
    )
    if cache_key not in FM_CLIENT_CACHE:
        FM_CLIENT_CACHE[cache_key] = FileMakerClient(profile)
    return FM_CLIENT_CACHE[cache_key]


# ---------------------------------------------------------------------------
# AI provider integration - Gemini and Claude, chosen by the user from the
# frontend Settings modal. Both share the same tool definitions (the tool
# names/args are identical) and the same run_tool_across_databases executor;
# only the wire format each provider's API expects is different.
# ---------------------------------------------------------------------------

GEMINI_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "list_available_layouts",
                "description": (
                    "Returns the list of all available layouts (views into the data) in the active "
                    "database(s). Use this FIRST whenever you are not sure which exact layout name "
                    "to use for get_records/find_records/etc, or when the user asks what data/tables "
                    "are available. NEVER guess or invent a layout name - always confirm it exists "
                    "in this list first."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_records",
                "description": "Fetch recent records from a specific layout in the active FileMaker database(s).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        },
                        "limit": {"type": "integer", "description": "Max records to fetch"},
                    },
                    "required": ["layout_name"],
                },
            },
            {
                "name": "find_records",
                "description": "Search FileMaker records on a specific layout matching field criteria.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        },
                        "query": {
                            "type": "object",
                            "description": "Field name/value pairs to search for, e.g. {'transactionNumber': '063548'}",
                        },
                    },
                    "required": ["layout_name", "query"],
                },
            },
            {
                "name": "describe_schema",
                "description": "Explain what fields and related tables exist on a specific layout, so the user understands what data is available before querying it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        }
                    },
                    "required": ["layout_name"],
                },
            },
            {
                "name": "create_record",
                "description": "Creates a brand new record on a specific layout with the given field values.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        },
                        "fields": {
                            "type": "object",
                            "description": "Field name/value pairs for the new record, e.g. {'FullName': 'Ali Raza', 'Phone': '0300...'}",
                        },
                    },
                    "required": ["layout_name", "fields"],
                },
            },
            {
                "name": "update_record",
                "description": (
                    "Updates an EXISTING record's fields on a specific layout. You must first identify the "
                    "record with a specific, unique query (e.g. a transaction number or exact name) - if the "
                    "query matches more than one record, nothing is changed and you must narrow it down."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        },
                        "query": {
                            "type": "object",
                            "description": "Field name/value pairs that uniquely identify the ONE record to update, e.g. {'transactionNumber': '063548'}",
                        },
                        "fields": {
                            "type": "object",
                            "description": "Field name/value pairs to change on that record, e.g. {'FullName': 'New Name'}",
                        },
                    },
                    "required": ["layout_name", "query", "fields"],
                },
            },
            {
                "name": "delete_record",
                "description": (
                    "Permanently deletes an EXISTING record on a specific layout. You must first identify "
                    "the record with a specific, unique query - if the query matches more than one record, "
                    "nothing is deleted and you must narrow it down. This action cannot be undone."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout_name": {
                            "type": "string",
                            "description": "The EXACT layout name, taken from list_available_layouts - never guessed.",
                        },
                        "query": {
                            "type": "object",
                            "description": "Field name/value pairs that uniquely identify the ONE record to delete, e.g. {'transactionNumber': '063548'}",
                        },
                    },
                    "required": ["layout_name", "query"],
                },
            },
        ]
    }
]


def _gemini_tools_to_claude_tools() -> list:
    """Claude's tool format is name/description/input_schema, which lines
    up almost 1:1 with Gemini's function_declarations parameters - so we
    derive it once instead of maintaining two separate tool lists that
    could drift out of sync."""
    fdecls = GEMINI_TOOLS[0]["function_declarations"]
    return [
        {"name": fd["name"], "description": fd["description"], "input_schema": fd["parameters"]}
        for fd in fdecls
    ]


CLAUDE_TOOLS = _gemini_tools_to_claude_tools()


def _gemini_tools_to_openai_tools() -> list:
    """OpenAI-compatible chat-completions tool format (used by OpenAI
    itself, DeepSeek, and most other third-party providers) - derived
    once from the same source as the other two, same reasoning as
    _gemini_tools_to_claude_tools() above."""
    fdecls = GEMINI_TOOLS[0]["function_declarations"]
    return [
        {
            "type": "function",
            "function": {"name": fd["name"], "description": fd["description"], "parameters": fd["parameters"]},
        }
        for fd in fdecls
    ]


OPENAI_TOOLS = _gemini_tools_to_openai_tools()

SYSTEM_INSTRUCTIONS = """
You are a helpful assistant that answers questions about the user's
currently active FileMaker database(s). One or MORE databases may be
active at once - you do not need to ask the user which one to use; your
tools automatically run against every currently active database and
return results tagged with the database name they came from.

HANDLING MULTIPLE DATABASES:
- Every tool result is a list of per-database blocks, e.g.
  [{"database": "LaundryPOS", "records": [...]}, {"database": "TAILORINGDEV", "records": [...]}]
  (a block may instead contain "error" if that one database failed - ignore
  that block and use whichever other databases succeeded; only mention the
  error if EVERY database failed).
- If only ONE active database has matching data, just answer normally -
  you do not need to name the database unless the user asks or it's
  genuinely ambiguous.
- If MORE THAN ONE active database has matching data for the same
  question, mention which database each result/value came from so the
  user isn't confused (e.g. "In LaundryPOS the balance is 0, and in
  TAILORINGDEV it is 1500.").
- If NO active database has matching data, say so plainly, e.g. "No
  matching records were found in any of the active databases."
- NEVER merge records from different databases into a single combined
  table, especially when their fields/columns are different (e.g.
  LaundryPOS "Transaction #, Status, Subtotal, VAT, Total" vs
  TailoringDBDev "Invoice #, Book #, Customer Name, Balance Amount").
  Doing so produces a confusing table with mismatched columns and a
  repeated header in the middle. Instead, give each database its OWN
  heading and its OWN separate table, e.g.:
    **LaundryPOS - Transactions**
    | Transaction # | Date | Status | ... |
    ...
    **TailoringDBDev - Invoices**
    | Invoice # | Book # | Customer Name | ... |
    ...
  Only combine into one shared table when every database's records have
  the SAME columns/fields for that question.

IMPORTANT - YOU CAN ANALYZE DATA YOURSELF:
Your tools (get_records, find_records) only fetch raw data - they do not
sort, filter, count, or calculate anything for you. That is expected and
is NOT a limitation you need to mention to the user. Once you have the
records, YOU are responsible for sorting, filtering, counting, comparing,
or reordering them yourself when writing your answer. For example, if the
user asks to see records "sorted by transaction number" or "with the
highest balance first", fetch the records and then simply list them in
that order in your own answer - never tell the user you "don't have a
sort function", because you can do this yourself with the data you have.

CRITICAL RULE ABOUT LAYOUT NAMES:
The user is an end user of a FileMaker-based business system (e.g. a shop
owner) - they do NOT know what a "layout" is and should never be asked to
name one or be shown FileMaker terminology. Every tool below requires a
layout_name argument, but YOU must figure that out yourself, silently:
- NEVER guess, invent, combine, or modify a layout name yourself.
- If you do not already know the exact layout name for what the user is
  asking about, call list_available_layouts FIRST and pick the closest
  matching name from that exact list, based on what the user described.
  Many databases name each layout EXACTLY the same as the table it
  represents (e.g. "customers" -> a layout literally named "Customers")
  - always prefer an exact (case-insensitive) name match over a partial
  or fuzzy one when one is available.
- The user's wording will rarely match a layout name exactly - expect
  typos, extra/missing spaces, singular vs plural (e.g. "customer" vs
  "Customers"), and partial names. Normalize mentally before comparing:
  ignore case, ignore extra whitespace, and match on the core word
  (e.g. "custmer list", "Customer", "customers" should all match a
  layout literally named "Customers : List"). Do not require an exact
  character-for-character match to consider it found.
- Only pass a layout_name that is an EXACT string taken from the list
  returned by list_available_layouts - never a variation, abbreviation,
  or a name the user typed verbatim if it doesn't exactly match.
- If your first attempt at a layout_name comes back as an error or
  "not found" from a tool, do NOT keep guessing new variations one at a
  time - call list_available_layouts (if you haven't already in this
  conversation) and pick the correct name directly from that returned
  list instead of guessing blind again.
- If no layout in the list reasonably matches what the user asked for,
  tell the user (in plain terms, e.g. "I couldn't find that type of
  data") - do not mention "layouts" to the user; that word is internal.
- WHEN SEVERAL LAYOUTS HAVE SIMILAR NAMES (e.g. "Services : List",
  "Services : Form", "Services_Price", "Service", "Services Type :
  List Card Window" all exist for one topic): do NOT call
  describe_schema on multiple candidates one after another to compare
  them - this wastes steps and often still fails to produce an answer.
  Instead:
  1. Pick ONE best guess immediately: prefer a name containing "List"
     when the user wants to browse/see multiple records (that is the
     normal browse view); prefer a name containing "Form" only if the
     user is asking about a single specific record's full layout of
     fields; ignore layout names containing "Card Window" unless
     nothing else matches.
  2. Call get_records (or find_records) on that ONE guess directly -
     do not call describe_schema first just to "check" it unless the
     user's request needs specific field names you don't already have
     from the records themselves.
  3. Only if that call errors or returns clearly wrong data, try your
     NEXT best guess from the list - one at a time, maximum two total
     attempts. If both fail, tell the user you couldn't find that data
     rather than continuing to try every remaining similarly-named
     layout.
- Once you've correctly identified a layout for a given topic (e.g.
  "customers" -> "Customers : List") within this conversation, remember
  and reuse it for follow-up questions about the same topic, instead of
  calling list_available_layouts again every single time.

HOW TO ANSWER:
- If the user asks for a specific value (e.g. "what is the balance of
  transaction 063548"), use find_records with the right field/value query,
  then answer in a short, plain sentence using the actual field value you
  found. Example: "The balance for transaction number 063548 is 0."
- If the user asks "how many records" or "how many X are there", fetch the
  relevant records and answer with a plain sentence stating the count.
  Example: "There are 49 records in this layout."
- If the user asks to see/list/show/sort/filter MULTIPLE records in
  general (e.g. "show me the customers", "list all transactions"), fetch
  them, then format your answer as a clean markdown table (pipe-separated).
  In this case, only include the 3-6 most relevant fields for the
  question, to keep the table readable.
- If the user asks for the FULL record, COMPLETE details, or "everything"
  about ONE specific person/record (e.g. "give me the full record of
  this customer", "show me all the details", "what is their complete
  info"), you MUST include every field that the tool actually returned
  for that record - do not trim or shorten the field list in this case.
  Only exclude fields that are true internal identifiers: raw UUIDs,
  auto-generated record IDs, or fields ending in "_fk". A field like
  "name", "phone", "address", "log_note", or "BalanceBK" must always be
  shown when the user asked for full/complete details, even if you
  personally judge it "less relevant".
- RELATED-TABLE FIELDS: some field names contain "::", e.g.
  "Transaction::summaryAmountBalance" - the part before "::" is a
  DIFFERENT, related table, not the table/layout the user is actually
  asking about. If the user asks for a customer's (or any record's) own
  details, leave out "TableName::field" values UNLESS the table name
  matches what they asked about, or they explicitly ask for related/
  linked data too (e.g. "including their transactions", "with order
  history"). This keeps a "customer's full details" answer limited to
  the customer's own fields, instead of mixing in unrelated tables.
- If you are not sure which fields exist, call describe_schema first
  instead of guessing a field name.
- Never invent or assume a value that a tool did not actually return.
- If a search returns zero matching records, say so plainly in a short
  sentence (e.g. "No matching records were found for that search.")
  instead of returning an empty or header-only table.
- Keep answers concise, except when the user explicitly asked for full/
  complete details - in that case, completeness matters more than brevity.

WRITE OPERATIONS (create_record, update_record, delete_record) - SAFETY RULES:
- These actions change real data and update_record/delete_record cannot
  be undone. Never call them casually or as a guess.
- Before calling update_record or delete_record, you MUST already know
  exactly which record is targeted. If you are not certain, first call
  find_records with the same query and show the user the matching
  record's key fields, then ASK for explicit confirmation (e.g. "I found
  this record: [details]. Do you want me to delete it?") and STOP - do
  not call update_record/delete_record in that same turn.
- Only call update_record or delete_record when the user's most recent
  message is a clear confirmation (e.g. "yes", "confirm", "do it", "go
  ahead") directly responding to a confirmation question you asked in
  this same conversation about that specific record.
- If update_record or delete_record reports "multiple_matches", do NOT
  pick one arbitrarily - tell the user how many matched and ask for more
  specific criteria (e.g. an exact ID instead of a name).
- If update_record or delete_record reports "not_found", tell the user
  plainly that no matching record was found - do not claim success.
- Before calling create_record, list out the field values you are about
  to save and ask for confirmation, unless the user already gave
  complete, explicit values and clearly asked you to create it directly
  (e.g. "add a new customer named X with phone Y").
- After any successful write, confirm plainly and specifically what
  changed (e.g. "Updated the customer's phone number to 0300-1234567."
  or "Deleted transaction 063548."). Never claim a write succeeded if
  the tool result did not report success.
"""


async def refresh_oauth_token(provider: str, refresh_token: str) -> dict:
    cfg = load_config()
    oauth_cfg = cfg.get("oauth", {})

    if refresh_token.startswith("mock_"):
        return {
            "access_token": f"mock_access_{int(time.time())}",
            "refresh_token": refresh_token,
            "expires_in": 3600
        }

    if provider == "gemini":
        client_id = oauth_cfg.get("google_client_id")
        client_secret_enc = oauth_cfg.get("google_client_secret")
        if not client_id or not client_secret_enc:
            raise Exception("Google client credentials not configured.")

        client_secret = decrypt_value(client_secret_enc)

        url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token") or refresh_token,
                "expires_in": data.get("expires_in", 3600)
            }

    return {
        "access_token": f"mock_access_{int(time.time())}",
        "refresh_token": refresh_token,
        "expires_in": 3600
    }


async def get_valid_oauth_token(cfg: dict, provider: str) -> Optional[str]:
    providers = cfg.get("ai", {}).get("providers", {})
    provider_cfg = providers.get(provider, {})
    oauth_tokens = provider_cfg.get("oauth_tokens")
    if not oauth_tokens:
        return None

    access_token_enc = oauth_tokens.get("access_token")
    refresh_token_enc = oauth_tokens.get("refresh_token")
    expires_at = oauth_tokens.get("expires_at")

    if not access_token_enc:
        return None

    access_token = decrypt_value(access_token_enc)

    current_time = int(time.time())
    if expires_at and current_time < expires_at - 30:
        return access_token

    if not refresh_token_enc:
        return None

    refresh_token = decrypt_value(refresh_token_enc)

    try:
        new_tokens = await refresh_oauth_token(provider, refresh_token)
        if new_tokens:
            cfg["ai"]["providers"][provider]["oauth_tokens"] = {
                "access_token": encrypt_value(new_tokens["access_token"]),
                "refresh_token": encrypt_value(new_tokens.get("refresh_token") or refresh_token),
                "expires_at": int(time.time()) + int(new_tokens.get("expires_in", 3600))
            }
            save_config(cfg)
            return new_tokens["access_token"]
    except Exception as e:
        print(f"Error refreshing OAuth token for {provider}: {e}")

    return None


async def call_gemini(contents: list, cfg: dict, provider_key: str = "gemini", max_retries: int = 2) -> dict:
    provider_cfg = cfg["ai"]["providers"][provider_key]
    model = provider_cfg.get("model") or DEFAULT_GEMINI_MODEL

    headers = {}
    access_token = await get_valid_oauth_token(cfg, provider_key)
    if access_token:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers["Authorization"] = f"Bearer {access_token}"
    else:
        api_key = provider_cfg.get("api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="No API key or OAuth account linked for Gemini.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    body = {
        "contents": contents,
        "tools": GEMINI_TOOLS,
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
    }

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as e:
            # Connection drops / timeouts are common when Google's servers
            # are under heavy load - retry with backoff instead of failing
            # the whole chat turn on the first blip.
            if attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))  # 2s, then 4s
                continue
            raise

        # 503 (model overloaded) and 429 (rate limited) are transient -
        # Google's own docs recommend a short retry for these, rather
        # than immediately failing the whole request.
        if resp.status_code in (503, 429) and attempt < max_retries:
            last_error = resp
            await asyncio.sleep(2 * (attempt + 1))  # 2s, then 4s
            continue

        resp.raise_for_status()
        return resp.json()

    last_error.raise_for_status()  # exhausted retries - raise the last error


async def call_claude(messages: list, cfg: dict, provider_key: str = "claude", max_retries: int = 2) -> dict:
    provider_cfg = cfg["ai"]["providers"][provider_key]
    model = provider_cfg.get("model") or DEFAULT_CLAUDE_MODEL
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    access_token = await get_valid_oauth_token(cfg, provider_key)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    else:
        api_key = provider_cfg.get("api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="No API key or OAuth account linked for Claude.")
        headers["x-api-key"] = api_key
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": SYSTEM_INSTRUCTIONS,
        "tools": CLAUDE_TOOLS,
        "messages": messages,
    }

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as e:
            if attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise

        if resp.status_code in (503, 429) and attempt < max_retries:
            last_error = resp
            await asyncio.sleep(2 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()

    last_error.raise_for_status()


async def call_openai_compatible(messages: list, cfg: dict, provider_key: str, max_retries: int = 2) -> dict:
    """
    Works for DeepSeek, OpenAI itself, and any "anything" provider Sohaib
    wants to plug in (Groq, a local Ollama/vLLM server, etc.) - almost
    all of them implement the same OpenAI-style /chat/completions +
    tools wire format, so one function covers all of them. Which exact
    server to hit comes from provider_cfg["base_url"] (or the registry
    preset), never hardcoded here.
    """
    provider_cfg = cfg["ai"]["providers"][provider_key]
    model = provider_cfg.get("model")
    base_url = (provider_cfg.get("base_url") or PROVIDER_REGISTRY.get(provider_key, {}).get("base_url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=500, detail=f"No base_url configured for provider '{provider_key}'.")
    url = f"{base_url}/chat/completions"

    access_token = await get_valid_oauth_token(cfg, provider_key)
    if access_token:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    else:
        api_key = provider_cfg.get("api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail=f"No API key or OAuth account linked for {provider_key}.")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    body = {"model": model, "messages": messages, "tools": OPENAI_TOOLS, "tool_choice": "auto"}

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as e:
            if attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise

        if resp.status_code in (503, 429) and attempt < max_retries:
            last_error = resp
            await asyncio.sleep(2 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()

    last_error.raise_for_status()


async def run_tool_across_databases(active: list, name: str, args: dict) -> list:
    """
    Runs one tool call (get_records / find_records / describe_schema)
    against EVERY currently active database, in parallel, and returns a
    list of per-database result blocks. A failure in one database (bad
    field name, that server being down, etc.) is captured as an "error"
    entry for just that block, instead of aborting the whole request -
    so the model can still answer from whichever databases worked.
    """

    async def run_one(profile_key: str, profile: dict, fm_client: "FileMakerClient") -> dict:
        db_label = profile.get("profile_name") or profile_key
        layout_name = args.get("layout_name")
        try:
            if name == "list_available_layouts":
                layouts = await fm_client.list_layouts()
                return {"database": db_label, "layouts": layouts}
            elif name == "get_records":
                records = await fm_client.get_records(layout_name, limit=args.get("limit", 50))
                return {"database": db_label, "records": records}
            elif name == "find_records":
                records = await fm_client.find_records(layout_name, args.get("query", {}))
                return {"database": db_label, "records": records}
            elif name == "describe_schema":
                schema = await fm_client.describe_layout_schema(layout_name)
                return {"database": db_label, "schema": schema}
            elif name == "create_record":
                result = await fm_client.create_record(layout_name, args.get("fields", {}))
                return {"database": db_label, **result}
            elif name == "update_record":
                result = await fm_client.update_record(layout_name, args.get("query", {}), args.get("fields", {}))
                return {"database": db_label, **result}
            elif name == "delete_record":
                result = await fm_client.delete_record(layout_name, args.get("query", {}))
                return {"database": db_label, **result}
            else:
                return {"database": db_label, "error": f"Unknown tool: {name}"}
        except httpx.HTTPStatusError as e:
            return {"database": db_label, "error": f"FileMaker error ({e.response.status_code}): {e.response.text[:200]}"}
        except httpx.RequestError:
            return {"database": db_label, "error": "Could not reach this FileMaker server."}
        except Exception as e:
            return {"database": db_label, "error": f"Internal database error: {str(e)}"}

    tasks = [run_one(key, profile, fm_client) for key, profile, fm_client in active]
    return await asyncio.gather(*tasks)


async def run_chat_loop(cfg: dict, active: list, contents: list, provider_key: str = "gemini", max_turns: int = 8) -> str:
    """
    Gemini version of the tool-calling loop: send the conversation to
    Gemini, execute any tool it asks for (against every active database),
    feed the results back, and repeat until Gemini returns a final
    natural-language answer (or max_turns is hit).

    `active` is a list of (profile_key, profile_dict, fm_client) tuples -
    one per currently active/selected database.
    """
    for _ in range(max_turns):
        try:
            gemini_response = await call_gemini(contents, cfg, provider_key)
        except KeyError as e:
            raise HTTPException(status_code=500, detail=f"config.json is missing key: {e}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini API rejected the request ({e.response.status_code}): {e.response.text[:300]}",
            )
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Could not reach the Gemini API. Check your internet connection.")

        candidates = gemini_response.get("candidates", [])
        if not candidates:
            return "Sorry, I couldn't process that request (no candidates returned)."

        first_candidate = candidates[0]
        finish_reason = first_candidate.get("finishReason")
        if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
            return f"Gemini request stopped due to finish reason: {finish_reason}."

        content = first_candidate.get("content")
        if not content:
            print("GEMINI DEBUG - empty content, full response:", json.dumps(gemini_response, indent=2))
            return "Sorry, I couldn't process that request (empty content)."

        parts = content.get("parts", [])
        if not parts:
            print("GEMINI DEBUG - no parts, full response:", json.dumps(gemini_response, indent=2))
            return "Sorry, I couldn't process that request (no response parts)."

        function_call_parts = [p for p in parts if "functionCall" in p]

        # TEMP DEBUG - remove once the "couldn't get final answer" issue is
        # resolved. Prints what the model asked for on every turn.
        print(
            "GEMINI TURN DEBUG - finish_reason:", finish_reason,
            "| function_calls:", [
                {"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                for p in function_call_parts
            ],
            "| text_parts:", [p["text"] for p in parts if "text" in p],
        )

        if not function_call_parts:
            text_parts = [p["text"] for p in parts if "text" in p]
            return " ".join(text_parts) if text_parts else "Sorry, I couldn't process that request."

        # Execute every requested tool call across every active database,
        # then feed all the results back to Gemini in one round-trip.
        function_responses = []
        for part in function_call_parts:
            fn = part["functionCall"]
            name = fn["name"]
            args = fn.get("args", {})

            result = await run_tool_across_databases(active, name, args)

            response_part = {"functionResponse": {"name": name, "response": {"result": result}}}
            if "id" in fn:
                response_part["functionResponse"]["id"] = fn["id"]
            function_responses.append(response_part)

        # Per the Gemini REST API: the turn holding functionCall parts
        # has role "model"; the turn holding the matching
        # functionResponse parts has role "user".
        contents.append(content)
        contents.append({"role": "user", "parts": function_responses})

    return "Sorry, I couldn't get a final answer within the allowed steps."


async def run_chat_loop_claude(cfg: dict, active: list, messages: list, provider_key: str = "claude", max_turns: int = 8) -> str:
    """
    Claude version of the same tool-calling loop as run_chat_loop, using
    the Anthropic Messages API's tool_use / tool_result blocks instead of
    Gemini's functionCall / functionResponse parts.
    """
    for _ in range(max_turns):
        try:
            claude_response = await call_claude(messages, cfg, provider_key)
        except KeyError as e:
            raise HTTPException(status_code=500, detail=f"config.json is missing key: {e}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Claude API rejected the request ({e.response.status_code}): {e.response.text[:300]}",
            )
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Could not reach the Claude API. Check your internet connection.")

        stop_reason = claude_response.get("stop_reason")
        content = claude_response.get("content", [])
        if not content:
            return "Sorry, I couldn't process that request (empty content)."

        tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]

        if stop_reason != "tool_use" or not tool_use_blocks:
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            return " ".join(text_parts) if text_parts else "Sorry, I couldn't process that request."

        # The assistant turn that requested tools must be echoed back
        # verbatim, followed by a user turn containing the matching
        # tool_result blocks (Anthropic Messages API convention).
        messages.append({"role": "assistant", "content": content})

        tool_results = []
        for block in tool_use_blocks:
            name = block["name"]
            args = block.get("input", {})
            result = await run_tool_across_databases(active, name, args)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I couldn't get a final answer within the allowed steps."


async def run_chat_loop_openai(cfg: dict, active: list, messages: list, provider_key: str, max_turns: int = 8) -> str:
    """
    Same tool-calling loop again, this time for the OpenAI-compatible wire
    format (DeepSeek, OpenAI, or any custom "anything" provider Sohaib
    wants to add) - uses choices[0].message.tool_calls / role:"tool"
    messages instead of Gemini's or Claude's own conventions.
    """
    for _ in range(max_turns):
        try:
            response = await call_openai_compatible(messages, cfg, provider_key)
        except KeyError as e:
            raise HTTPException(status_code=500, detail=f"config.json is missing key: {e}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"{provider_key.title()} API rejected the request ({e.response.status_code}): {e.response.text[:300]}",
            )
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail=f"Could not reach the {provider_key.title()} API. Check your internet connection.")

        choices = response.get("choices", [])
        if not choices:
            return "Sorry, I couldn't process that request (no choices returned)."

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return message.get("content") or "Sorry, I couldn't process that request."

        # The assistant turn that requested tools must be echoed back
        # verbatim, followed by one role:"tool" message per tool call
        # (OpenAI-compatible convention).
        messages.append(message)

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await run_tool_across_databases(active, name, args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})

    return "Sorry, I couldn't get a final answer within the allowed steps."


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" or "bot"
    text: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class CredentialsInput(BaseModel):
    server_url: str
    username: str
    password: str
    verify_ssl: bool = False


class LayoutsRequest(CredentialsInput):
    database: str


class ProfileSaveRequest(LayoutsRequest):
    profile_key: str
    profile_name: str


class AISettingsRequest(BaseModel):
    provider: str  # any provider name - a PROVIDER_REGISTRY preset, or a custom one
    api_key: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None   # required for a custom (non-preset) provider
    api_style: Optional[str] = None  # "gemini" | "claude" | "openai" - only needed for a custom provider


# ---------------------------------------------------------------------------
# Discovery routes: credentials -> databases -> layouts -> schema
# ---------------------------------------------------------------------------

@app.post("/api/discover/databases")
async def discover_databases(creds: CredentialsInput):
    try:
        databases = await fm_list_databases(creds.server_url, creds.username, creds.password, creds.verify_ssl)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Could not authenticate with this server/credentials.")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach that server. Check the URL and your network.")

    # Credentials worked - persist them as the shared login (Zeeshan uses
    # the same username/password across every database on this server),
    # so the list can be refreshed later without retyping anything.
    cfg = load_config()
    cfg["credentials"] = {
        "server_url": creds.server_url,
        "username": creds.username,
        "password": creds.password,
        "verify_ssl": creds.verify_ssl,
    }
    save_config(cfg)

    return {"databases": databases}


@app.post("/api/discover/databases/refresh")
async def refresh_databases():
    """Re-lists databases on the server using the already-saved shared
    credentials - lets Nigah repopulate the list (e.g. if Zeeshan added a
    new database) without re-entering server/username/password."""
    cfg = load_config()
    creds = cfg.get("credentials")
    if not creds:
        raise HTTPException(status_code=400, detail="No saved credentials yet. Connect once first.")
    try:
        databases = await fm_list_databases(
            creds["server_url"], creds["username"], creds["password"], creds.get("verify_ssl", False)
        )
    except httpx.HTTPStatusError:
        raise HTTPException(status_code=401, detail="Saved credentials no longer work for this server.")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach that server. Check the URL and your network.")
    return {"databases": databases}


@app.get("/api/credentials")
def get_credentials():
    """Tells the frontend whether shared credentials are already saved,
    without ever exposing the password."""
    cfg = load_config()
    creds = cfg.get("credentials")
    if not creds:
        return {"has_credentials": False}
    return {
        "has_credentials": True,
        "server_url": creds["server_url"],
        "username": creds["username"],
    }



@app.post("/api/profiles/save")
async def save_profile(req: ProfileSaveRequest):
    cfg = load_config()
    server_url, username, password, verify_ssl = resolve_credentials(
        cfg, req.server_url, req.username, req.password, req.verify_ssl
    )
    try:
        token = await fm_login(server_url, req.database, username, password, verify_ssl)
        await fm_logout(server_url, req.database, token, verify_ssl)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Could not connect to that database.")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach that server. Check the URL and your network.")

    _, project = get_active_project(cfg)
    project["profiles"][req.profile_key] = {
        "profile_name": req.profile_name,
        "server_url": server_url,
        "database": req.database,
        "username": username,
        "password": password,
        "verify_ssl": verify_ssl,
    }
    if req.profile_key not in project["active_profiles"]:
        project["active_profiles"].append(req.profile_key)
    save_config(cfg)
    return {"message": f"Profile '{req.profile_key}' saved."}


# ---------------------------------------------------------------------------
# Profile management (list / switch / delete)
# ---------------------------------------------------------------------------

class ActiveProfilesRequest(BaseModel):
    profile_keys: list[str]


@app.get("/api/config/profiles")
def list_profiles():
    cfg = load_config()
    _, project = get_active_project(cfg)
    safe = {
        name: {k: v for k, v in p.items() if k != "password"}
        for name, p in project["profiles"].items()
    }
    return {"active_profiles": project.get("active_profiles", []), "profiles": safe}


@app.post("/api/config/active")
def set_active_profiles(req: ActiveProfilesRequest):
    """Sets the FULL list of currently active/selected databases within
    the active project - lets Nigah pick one database, or several at
    once, to query against."""
    cfg = load_config()
    _, project = get_active_project(cfg)
    unknown = [k for k in req.profile_keys if k not in project["profiles"]]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown profile(s): {', '.join(unknown)}")
    project["active_profiles"] = req.profile_keys
    save_config(cfg)
    return {"message": "Active databases updated.", "active_profiles": req.profile_keys}


@app.delete("/api/config/profiles/{name}")
def delete_profile(name: str):
    cfg = load_config()
    _, project = get_active_project(cfg)
    project["profiles"].pop(name, None)
    project["active_profiles"] = [k for k in project.get("active_profiles", []) if k != name]
    save_config(cfg)
    return {"message": f"Profile '{name}' deleted."}


# ---------------------------------------------------------------------------
# Projects (client-level grouping): each project has its own set of
# database profiles, so "Client A" and "Client B" stay completely
# separate - switching projects also starts a fresh chat on the frontend.
# ---------------------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
    name: str


@app.get("/api/projects")
def list_projects():
    cfg = load_config()
    projects = {
        key: {"name": p.get("name", key), "database_count": len(p.get("profiles", {}))}
        for key, p in cfg.get("projects", {}).items()
    }
    return {"active_project": cfg.get("active_project"), "projects": projects}


@app.post("/api/projects")
def create_project(req: ProjectCreateRequest):
    cfg = load_config()
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required.")

    key = name.lower().replace(" ", "_")
    base_key = key
    n = 2
    while key in cfg["projects"]:
        key = f"{base_key}_{n}"
        n += 1

    cfg["projects"][key] = {"name": name, "profiles": {}, "active_profiles": []}
    cfg["active_project"] = key
    save_config(cfg)
    return {"message": f"Project '{name}' created.", "project_key": key}


@app.post("/api/projects/active/{key}")
def set_active_project(key: str):
    cfg = load_config()
    if key not in cfg.get("projects", {}):
        raise HTTPException(status_code=404, detail="Project not found.")
    cfg["active_project"] = key
    save_config(cfg)
    return {"message": f"Active project set to '{key}'."}


@app.delete("/api/projects/{key}")
def delete_project(key: str):
    cfg = load_config()
    if len(cfg.get("projects", {})) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the only remaining project.")
    cfg["projects"].pop(key, None)
    if cfg.get("active_project") == key:
        cfg["active_project"] = next(iter(cfg["projects"]), None)
    save_config(cfg)
    return {"message": f"Project '{key}' deleted.", "active_project": cfg.get("active_project")}


# ---------------------------------------------------------------------------
# AI provider settings - lets ANY normal user plug in their own Gemini or
# Claude API key from the frontend, instead of it being hardcoded in
# config.json by a developer ahead of time.
# ---------------------------------------------------------------------------

@app.get("/api/settings/ai")
def get_ai_settings():
    """
    Returns the currently active provider's settings, PLUS the preset
    registry (so the frontend dropdown can offer Gemini/Claude/DeepSeek/
    OpenAI/"Custom..." out of the box) and every provider that has
    already been configured with a key (so switching providers in the
    dropdown doesn't lose the others' saved keys - same behavior as
    before, just no longer limited to exactly two providers).
    """
    cfg = load_config()
    ai = cfg.get("ai", {})
    provider = ai.get("provider", "gemini")
    providers_cfg = ai.get("providers", {})
    current = providers_cfg.get(provider, {})
    return {
        "provider": provider,
        "has_key": bool(current.get("api_key")) or bool(current.get("oauth_tokens")),
        "has_oauth": bool(current.get("oauth_tokens")),
        "model": current.get("model", ""),
        "api_style": current.get("api_style") or PROVIDER_REGISTRY.get(provider, {}).get("api_style", "openai"),
        "base_url": current.get("base_url"),
        "known_providers": {
            key: {"label": v["label"], "api_style": v["api_style"], "default_model": v["default_model"]}
            for key, v in PROVIDER_REGISTRY.items()
        },
        "configured_providers": {
            key: {
                "has_key": bool(p.get("api_key")) or bool(p.get("oauth_tokens")),
                "model": p.get("model", ""),
                "has_oauth": bool(p.get("oauth_tokens"))
            }
            for key, p in providers_cfg.items()
        },
    }


@app.post("/api/settings/ai")
def save_ai_settings(req: AISettingsRequest):
    provider = req.provider.strip().lower()
    if not provider:
        raise HTTPException(status_code=400, detail="Provider name is required.")

    cfg = load_config()
    cfg["ai"].setdefault("providers", {})
    existing = cfg["ai"]["providers"].get(provider, {})

    api_key = req.api_key.strip() if req.api_key else ""
    if not api_key and not existing.get("api_key") and not existing.get("oauth_tokens"):
        raise HTTPException(status_code=400, detail="API key is required.")

    registry_entry = PROVIDER_REGISTRY.get(provider)
    if registry_entry:
        api_style = registry_entry["api_style"]
        base_url = req.base_url.strip() if req.base_url else registry_entry.get("base_url")
        default_model = registry_entry["default_model"]
    else:
        api_style = (req.api_style or "openai").strip().lower()
        if api_style not in ("openai", "gemini", "claude"):
            raise HTTPException(status_code=400, detail="api_style must be 'openai', 'gemini', or 'claude' for a custom provider.")
        base_url = req.base_url.strip() if req.base_url else None
        if api_style == "openai" and not base_url:
            raise HTTPException(status_code=400, detail=f"base_url is required for a custom provider like '{provider}'.")
        default_model = None

    model = req.model.strip() if req.model and req.model.strip() else (existing.get("model") or default_model)
    if not model:
        raise HTTPException(status_code=400, detail=f"A model name is required for provider '{provider}'.")

    final_api_key = api_key if api_key else existing.get("api_key", "")

    cfg["ai"]["provider"] = provider
    cfg["ai"]["providers"][provider] = {
        "api_key": final_api_key,
        "model": model,
        "api_style": api_style,
        "base_url": base_url,
    }
    if "oauth_tokens" in existing:
        cfg["ai"]["providers"][provider]["oauth_tokens"] = existing["oauth_tokens"]

    save_config(cfg)
    return {"message": f"{provider.title()} settings saved."}


# ---------------------------------------------------------------------------
# OAuth 2.0 Integration
# ---------------------------------------------------------------------------

class OAuthCredentialsRequest(BaseModel):
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None


@app.get("/api/auth/config")
def get_auth_config():
    cfg = load_config()
    oauth = cfg.get("oauth", {})
    return {
        "google_client_id": oauth.get("google_client_id", ""),
        "has_google_secret": bool(oauth.get("google_client_secret")),
        "is_demo_mode": not bool(oauth.get("google_client_id") and oauth.get("google_client_secret")),
    }


@app.post("/api/auth/config")
def save_auth_config(req: OAuthCredentialsRequest):
    cfg = load_config()
    cfg.setdefault("oauth", {})
    if req.google_client_id is not None:
        cfg["oauth"]["google_client_id"] = req.google_client_id.strip()
    if req.google_client_secret is not None:
        secret = req.google_client_secret.strip()
        if secret:
            cfg["oauth"]["google_client_secret"] = encrypt_value(secret)
        else:
            cfg["oauth"]["google_client_secret"] = ""
    save_config(cfg)
    return {"message": "OAuth credentials saved."}


@app.post("/api/auth/{provider}/unlink")
def unlink_auth(provider: str):
    cfg = load_config()
    provider_cfg = cfg.get("ai", {}).get("providers", {}).get(provider)
    if provider_cfg and "oauth_tokens" in provider_cfg:
        provider_cfg.pop("oauth_tokens")
        save_config(cfg)
        return {"message": f"{provider.title()} account unlinked."}
    return {"message": "No account linked."}


@app.get("/api/auth/{provider}/login")
def oauth_login(provider: str):
    cfg = load_config()
    oauth = cfg.get("oauth", {})
    client_id = oauth.get("google_client_id")
    client_secret = oauth.get("google_client_secret")

    if provider == "gemini" and client_id and client_secret:
        import urllib.parse
        redirect_uri = "http://127.0.0.1:8000/api/auth/gemini/callback"
        scopes = "https://www.googleapis.com/auth/generative-language.tuning https://www.googleapis.com/auth/cloud-platform"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
        return HTMLResponse(content=f'<script>window.location.href = "{url}";</script>')
    else:
        # Redirect to simulation page
        return HTMLResponse(content=f'<script>window.location.href = "/mock_login.html?provider={provider}";</script>')


@app.get("/api/auth/{provider}/callback", response_class=HTMLResponse)
async def oauth_callback(provider: str, code: Optional[str] = None):
    cfg = load_config()
    oauth = cfg.get("oauth", {})
    client_id = oauth.get("google_client_id")
    client_secret_enc = oauth.get("google_client_secret")

    access_token = ""
    refresh_token = ""
    expires_in = 3600

    if provider == "gemini" and client_id and client_secret_enc and code and not code.startswith("mock_"):
        client_secret = decrypt_value(client_secret_enc)
        url = "https://oauth2.googleapis.com/token"
        payload = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": "http://127.0.0.1:8000/api/auth/gemini/callback",
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload)
            if resp.status_code != 200:
                return HTMLResponse(content=f"<h3>Authentication failed: {resp.text}</h3>")
            data = resp.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)
    else:
        # Simulation mode: generate mock tokens
        access_token = f"mock_access_{int(time.time())}"
        refresh_token = f"mock_refresh_{int(time.time())}"
        expires_in = 3600

    cfg["ai"].setdefault("providers", {})
    cfg["ai"]["providers"].setdefault(provider, {})
    cfg["ai"]["providers"][provider]["oauth_tokens"] = {
        "access_token": encrypt_value(access_token),
        "refresh_token": encrypt_value(refresh_token),
        "expires_at": int(time.time()) + int(expires_in),
    }
    cfg["ai"]["provider"] = provider
    save_config(cfg)

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Authentication Successful</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                background: linear-gradient(135deg, #0f0c20 0%, #15102a 100%);
                color: #fff;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
            }
            .card {
                background: rgba(255, 255, 255, 0.05);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.1);
                padding: 40px;
                border-radius: 20px;
                text-align: center;
                box-shadow: 0 20px 50px rgba(0,0,0,0.3);
                max-width: 400px;
            }
            h1 { color: #00e5ff; margin-top: 0; font-size: 24px; }
            p { color: #a5a1b8; font-size: 15px; line-height: 1.5; margin-bottom: 30px; }
            button {
                background: linear-gradient(135deg, #007bff, #00e5ff);
                border: none;
                color: white;
                padding: 12px 30px;
                font-size: 15px;
                font-weight: 600;
                border-radius: 30px;
                cursor: pointer;
                transition: all 0.3s ease;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 10px 20px rgba(0, 229, 255, 0.4);
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Account Connected!</h1>
            <p>Your account has been linked successfully. You can now close this tab and return to the chatbot.</p>
            <button onclick="window.close()">Close Window</button>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    provider = ai_cfg.get("provider", "gemini")
    provider_settings = ai_cfg.get("providers", {}).get(provider, {})

    if not provider_settings.get("api_key") and not provider_settings.get("oauth_tokens"):
        raise HTTPException(
            status_code=400,
            detail=f"No {provider.title()} API key or OAuth account linked yet. Open AI Settings in the sidebar and add your API key or link your account first.",
        )

    check_and_increment_quota(ai_cfg.get("daily_request_limit", 20))

    # One or more databases can be active at once (Nigah may have selected
    # just LaundryPOS, just TAILORINGDEV, or both together).
    active_profiles = get_active_profiles(cfg)
    active = [(key, profile, get_fm_client(key, profile)) for key, profile in active_profiles]

    # api_style decides which wire format to speak - NOT the provider name
    # itself. This is what makes "any provider" actually work: a brand
    # new provider just needs an api_style, not a new branch of code here.
    api_style = provider_settings.get("api_style") or PROVIDER_REGISTRY.get(provider, {}).get("api_style", "openai")

    if api_style == "claude":
        # Anthropic Messages API: plain string content for normal turns.
        messages = []
        for msg in req.history:
            role = "assistant" if msg.role == "bot" else "user"
            messages.append({"role": role, "content": msg.text})
        messages.append({"role": "user", "content": req.message})
        answer_text = await run_chat_loop_claude(cfg, active, messages, provider)
    elif api_style == "gemini":
        # Build the conversation: prior turns (if any) + the new question.
        # This is what gives the chatbot memory across follow-up questions
        # like "show me those again but sorted by name".
        contents = []
        for msg in req.history:
            role = "model" if msg.role == "bot" else "user"
            contents.append({"role": role, "parts": [{"text": msg.text}]})
        contents.append({"role": "user", "parts": [{"text": req.message}]})
        answer_text = await run_chat_loop(cfg, active, contents, provider)
    else:
        # OpenAI-compatible wire format - DeepSeek, OpenAI, or any custom
        # "anything" provider Sohaib wants to add later.
        messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
        for msg in req.history:
            role = "assistant" if msg.role == "bot" else "user"
            messages.append({"role": role, "content": msg.text})
        messages.append({"role": "user", "content": req.message})
        answer_text = await run_chat_loop_openai(cfg, active, messages, provider)

    return {"type": "text", "data": answer_text}


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


# ---------------------------------------------------------------------------
# Standalone launch entrypoint - lets this run like a normal double-click
# app (as a plain script during development, or as a PyInstaller .exe for
# end users): starts the server AND opens the browser automatically, so
# there is nothing else for the user to run or type.
# ---------------------------------------------------------------------------

def _open_browser_after_delay(url: str, delay_seconds: float = 1.5) -> None:
    def _open():
        time.sleep(delay_seconds)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    import uvicorn

    PORT = 8000
    _open_browser_after_delay(f"http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
