"""
Standalone smoke test for the multi-database chat flow.
Mocks FileMaker + Gemini calls (no real network needed) and exercises:
  - config migration (active_profile -> active_profiles, credentials block)
  - /api/config/profiles, /api/config/active (multi-select)
  - /api/discover/databases/refresh (uses saved shared credentials)
  - /api/chat with TWO active databases at once, verifying the model
    receives per-database tagged results and both were actually queried.
  - one database failing does NOT take down the whole /api/chat request.

Safe to re-run: your real config.json is backed up first and always
restored at the end, even if a test fails.
"""
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))

CONFIG_PATH = Path(__file__).parent / "config.json"
BACKUP_PATH = Path(__file__).parent / "config_backup.json"

if CONFIG_PATH.exists():
    shutil.copy(CONFIG_PATH, BACKUP_PATH)

try:
    seed = {
        "active_profile": "laundrypos",  # old-style key, to test migration
        "profiles": {
            "laundrypos": {
                "profile_name": "LaundryPOS",
                "server_url": "https://fm.idiosol.com",
                "database": "LaundryPOS",
                "layout": "Customers : List",
                "username": "Admin",
                "password": "sohaib086",
                "verify_ssl": False,
                "schema": {"fields": ["SEARCH", "log_note", "BalanceBK", "Phone"], "related_tables": []},
            },
            "tailoringdev": {
                "profile_name": "TAILORINGDEV",
                "server_url": "https://fm.idiosol.com",
                "database": "TAILORINGDEV",
                "layout": "Orders : List",
                "username": "Admin",
                "password": "sohaib086",
                "verify_ssl": False,
                "schema": {"fields": ["OrderID", "CustomerName", "Balance"], "related_tables": []},
            },
        },
        "gemini": {"api_key": "FAKE_KEY", "model": "gemini-2.5-flash", "daily_request_limit": 20},
    }
    CONFIG_PATH.write_text(json.dumps(seed, indent=2))

    import main  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    client = TestClient(main.app)

    print("== 1. /api/config/profiles (tests active_profile -> active_profiles migration) ==")
    r = client.get("/api/config/profiles")
    print(r.status_code, r.json())
    assert r.json()["active_profiles"] == ["laundrypos"], "migration from active_profile failed"

    print("\n== 2. select BOTH databases as active ==")
    r = client.post("/api/config/active", json={"profile_keys": ["laundrypos", "tailoringdev"]})
    print(r.status_code, r.json())
    assert r.json()["active_profiles"] == ["laundrypos", "tailoringdev"]

    print("\n== 3. /api/discover/databases/refresh (mocked fm_list_databases) ==")
    with patch("main.fm_list_databases", new=AsyncMock(return_value=["LaundryPOS", "TAILORINGDEV", "NewDB"])):
        r = client.post("/api/discover/databases/refresh")
        print(r.status_code, r.json())
        assert r.json()["databases"] == ["LaundryPOS", "TAILORINGDEV", "NewDB"]

    print("\n== 4. /api/chat with both databases active ==")
    calls_seen = []

    async def fake_get_records(self, limit=20):
        calls_seen.append(self.database)
        if self.database == "LaundryPOS":
            return [{"Phone": "0300-1111111", "BalanceBK": "0"}]
        return [{"OrderID": "OD-99", "CustomerName": "Ali", "Balance": "1500"}]

    gemini_call_count = {"n": 0}

    async def fake_call_gemini(contents, cfg, max_retries=2):
        gemini_call_count["n"] += 1
        if gemini_call_count["n"] == 1:
            return {
                "candidates": [{
                    "content": {"parts": [{"functionCall": {"name": "get_records", "args": {"limit": 20}}}]}
                }]
            }
        last_msg = contents[-1]
        result_payload = last_msg["parts"][0]["functionResponse"]["response"]["result"]
        dbs_in_result = sorted(b["database"] for b in result_payload)
        assert dbs_in_result == ["LaundryPOS", "TAILORINGDEV"], f"expected both DBs tagged, got {dbs_in_result}"
        return {"candidates": [{"content": {"parts": [{"text": "Balances: LaundryPOS=0, TAILORINGDEV=1500"}]}}]}

    with patch.object(main.FileMakerClient, "get_records", new=fake_get_records), \
         patch("main.call_gemini", new=fake_call_gemini):
        r = client.post("/api/chat", json={"message": "what are the balances?", "history": []})
        print(r.status_code, r.json())
        assert r.status_code == 200
        assert calls_seen == ["LaundryPOS", "TAILORINGDEV"], f"expected both DBs queried, got {calls_seen}"
        assert "1500" in r.json()["data"] and "0" in r.json()["data"]

    print("\n== 5. one database errors out, the other still answers ==")
    gemini_call_count["n"] = 0

    async def flaky_get_records(self, limit=20):
        if self.database == "LaundryPOS":
            import httpx
            raise httpx.ConnectError("boom")
        return [{"OrderID": "OD-1", "CustomerName": "Sara", "Balance": "500"}]

    async def fake_call_gemini_2(contents, cfg, max_retries=2):
        gemini_call_count["n"] += 1
        if gemini_call_count["n"] == 1:
            return {
                "candidates": [{
                    "content": {"parts": [{"functionCall": {"name": "get_records", "args": {"limit": 20}}}]}
                }]
            }
        last_msg = contents[-1]
        result_payload = last_msg["parts"][0]["functionResponse"]["response"]["result"]
        by_db = {b["database"]: b for b in result_payload}
        assert "error" in by_db["LaundryPOS"], "expected LaundryPOS block to carry an error, not crash the request"
        assert "records" in by_db["TAILORINGDEV"], "TAILORINGDEV should still have succeeded"
        return {"candidates": [{"content": {"parts": [{"text": "TAILORINGDEV: Sara's balance is 500."}]}}]}

    with patch.object(main.FileMakerClient, "get_records", new=flaky_get_records), \
         patch("main.call_gemini", new=fake_call_gemini_2):
        r = client.post("/api/chat", json={"message": "balances?", "history": []})
        print(r.status_code, r.json())
        assert r.status_code == 200, "a single DB failing must NOT take down the whole /api/chat call"

    print("\nALL SMOKE TESTS PASSED")

finally:
    # Always restore the real config.json, whether tests passed or failed.
    if BACKUP_PATH.exists():
        shutil.copy(BACKUP_PATH, CONFIG_PATH)
        BACKUP_PATH.unlink()
