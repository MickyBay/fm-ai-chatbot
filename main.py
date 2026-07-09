"""
fm-ai-chatbot backend
----------------------
Flow:
  1. User enters server_url + username + password only.
  2. Backend calls FileMaker Data API to list available DATABASES.
  3. User picks database(s) and names the profile.
  4. Profile (server, db, credentials) is saved.
  5. Chatbot uses Gemini function-calling under the hood. Gemini silently
     calls tool functions (list_available_layouts, describe_schema, get_records,
     find_records) to discover layouts and query data based on user questions.
     The user never has to select or deal with FileMaker layout names.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONFIG_PATH = Path(__file__).parent / "config.json"

app = FastAPI(title="fm-ai-chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail="config.json not found")
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
# Daily Gemini quota tracker
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
            detail=f"Daily Gemini quota ({limit} requests) reached. Try again tomorrow.",
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

    async def _ensure_token(self):
        if not self.token:
            self.token = await fm_login(self.server_url, self.database, self.username, self.password, self.verify_ssl)

    async def list_layouts(self) -> list[str]:
        await self._ensure_token()
        return await fm_list_layouts(self.server_url, self.database, self.token, self.verify_ssl)

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


# ---------------------------------------------------------------------------
# Gemini integration
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
- Only pass a layout_name that is an EXACT string taken from the list
  returned by list_available_layouts - never a variation, abbreviation,
  or a name the user typed verbatim if it doesn't exactly match.
- If no layout in the list reasonably matches what the user asked for,
  tell the user (in plain terms, e.g. "I couldn't find that type of
  data") - do not mention "layouts" to the user; that word is internal.
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


async def call_gemini(contents: list, cfg: dict, max_retries: int = 2) -> dict:
    api_key = cfg["gemini"]["api_key"]
    model = cfg["gemini"].get("model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    body = {
        "contents": contents,
        "tools": GEMINI_TOOLS,
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
    }

    last_error = None
    for attempt in range(max_retries + 1):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body)

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

    tasks = [run_one(key, profile, fm_client) for key, profile, fm_client in active]
    return await asyncio.gather(*tasks)


async def run_chat_loop(cfg: dict, active: list, contents: list, max_turns: int = 5) -> str:
    """
    Runs the full tool-calling loop: send the conversation to Gemini,
    execute any tool it asks for (against every active database), feed
    the results back, and repeat until Gemini returns a final
    natural-language answer (or max_turns is hit).

    `active` is a list of (profile_key, profile_dict, fm_client) tuples -
    one per currently active/selected database.
    """
    for _ in range(max_turns):
        try:
            gemini_response = await call_gemini(contents, cfg)
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
            return "Sorry, I couldn't process that request (empty content)."

        parts = content.get("parts", [])
        if not parts:
            return "Sorry, I couldn't process that request (no response parts)."

        function_call_parts = [p for p in parts if "functionCall" in p]

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

            function_responses.append({"functionResponse": {"name": name, "response": {"result": result}}})

        # Per the Gemini REST API: the turn holding functionCall parts
        # has role "model"; the turn holding the matching
        # functionResponse parts has role "user".
        contents.append({"role": "model", "parts": function_call_parts})
        contents.append({"role": "user", "parts": function_responses})

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
# Chat
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    cfg = load_config()
    check_and_increment_quota(cfg["gemini"].get("daily_request_limit", 20))

    # One or more databases can be active at once (Nigah may have selected
    # just LaundryPOS, just TAILORINGDEV, or both together).
    active_profiles = get_active_profiles(cfg)
    active = [(key, profile, FileMakerClient(profile)) for key, profile in active_profiles]

    # Build the conversation: prior turns (if any) + the new question.
    # This is what gives the chatbot memory across follow-up questions
    # like "show me those again but sorted by name".
    contents = []
    for msg in req.history:
        role = "model" if msg.role == "bot" else "user"
        contents.append({"role": role, "parts": [{"text": msg.text}]})
    contents.append({"role": "user", "parts": [{"text": req.message}]})

    answer_text = await run_chat_loop(cfg, active, contents)
    return {"type": "text", "data": answer_text}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
