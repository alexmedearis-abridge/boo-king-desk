"""Versioned, person-scoped preference memory. No model or framework dependency."""
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
PATIENT_KEYS = {"time_of_day", "days_of_week", "location", "provider"}
PROVIDER_KEYS = PATIENT_KEYS - {"provider"} | {"evaluation_gap_minutes", "blocked_periods"}


class VersionConflict(ValueError):
    pass


def validate_preferences(kind, changes):
    """Validate a partial patch; null deletes a key. Each key has its own strength."""
    keys = PATIENT_KEYS if kind == "patient" else PROVIDER_KEYS
    if kind not in ("patient", "provider") or not isinstance(changes, dict) or not changes or len(changes) > 8:
        raise ValueError("Invalid preference patch")
    for key, entry in changes.items():
        if key not in keys:
            raise ValueError("Unsupported preference key")
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) != {"value", "strength"} or entry["strength"] not in ("hard", "soft"):
            raise ValueError("Each preference needs value and hard/soft strength")
        value = entry["value"]
        if key == "time_of_day" and value not in ("morning", "afternoon", "any"):
            raise ValueError("Invalid time of day")
        if key == "location" and value not in ("Downtown", "Eastside", "any"):
            raise ValueError("Invalid location")
        if key == "provider" and (not isinstance(value, str) or not 1 <= len(value) <= 100):
            raise ValueError("Invalid provider")
        if key == "days_of_week" and (not isinstance(value, list) or len(value) > 7 or any(not isinstance(d, str) or d not in DAYS for d in value)):
            raise ValueError("Invalid weekdays")
        if key == "evaluation_gap_minutes" and (type(value) is not int or not 0 <= value <= 480 or entry["strength"] != "hard"):
            raise ValueError("Invalid evaluation gap")
        if key == "blocked_periods":
            if not isinstance(value, list) or len(value) > 14 or entry["strength"] != "hard":
                raise ValueError("Invalid blocked periods")
            for period in value:
                if not isinstance(period, dict) or set(period) != {"day", "time_of_day"} or period["day"] not in DAYS or period["time_of_day"] not in ("morning", "afternoon", "any"):
                    raise ValueError("Invalid blocked period")


class PreferenceStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(kind, subject));
                CREATE TABLE IF NOT EXISTS preferences (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, scope TEXT NOT NULL,
                    key TEXT NOT NULL, entry TEXT NOT NULL, valid_until TEXT,
                    source TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind, subject, scope, key));
                CREATE TABLE IF NOT EXISTS preference_changes (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    scope TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _read(self, db, kind, subject, request_id, on_date):
        row = db.execute("SELECT version FROM profiles WHERE kind=? AND subject=?", (kind, subject)).fetchone()
        scopes = ["standing"] + (["request:" + request_id] if request_id else [])
        records = []
        effective = {}
        for scope in scopes:
            rows = db.execute("SELECT * FROM preferences WHERE kind=? AND subject=? AND scope=? ORDER BY key", (kind, subject, scope)).fetchall()
            for item in rows:
                if item["valid_until"] and item["valid_until"] < on_date:
                    continue
                entry = json.loads(item["entry"])
                effective[item["key"]] = entry
                records.append({"key": item["key"], **entry, "scope": scope,
                                "valid_until": item["valid_until"], "source": item["source"], "updated_at": item["updated_at"]})
        return {"version": row["version"] if row else 0, "effective": effective, "records": records}

    def read(self, kind, subject, request_id=None, on_date=None):
        with self.connect() as db:
            # One transaction keeps the version and records from the same snapshot.
            db.execute("BEGIN")
            return self._read(db, kind, subject, request_id, on_date or date.today().isoformat())

    def patch(self, kind, subject, changes, expected_version, *, request_id=None,
              valid_until=None, source, actor):
        validate_preferences(kind, changes)
        if not isinstance(subject, str) or not 1 <= len(subject) <= 150:
            raise ValueError("Invalid subject")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Expected version required")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise ValueError("Invalid request scope")
        if valid_until is not None:
            if not isinstance(valid_until, str) or date.fromisoformat(valid_until).isoformat() != valid_until:
                raise ValueError("Invalid expiry date")
        scope = "request:" + request_id if request_id else "standing"
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = self._read(db, kind, subject, request_id, date.today().isoformat())
            if before["version"] != expected_version:
                raise VersionConflict("Preferences changed; read current context and retry")
            version = expected_version + 1
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?, ?)", (kind, subject, version))
            for key, entry in changes.items():
                if entry is None:
                    db.execute("DELETE FROM preferences WHERE kind=? AND subject=? AND scope=? AND key=?", (kind, subject, scope, key))
                else:
                    db.execute("INSERT OR REPLACE INTO preferences VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (kind, subject, scope, key, json.dumps(entry), valid_until, source, now))
            after = self._read(db, kind, subject, request_id, date.today().isoformat())
            # Dedicated change history stays in the protected store, never application logs.
            db.execute("INSERT INTO preference_changes(kind,subject,scope,version,actor,changed_at,before_json,after_json) VALUES (?,?,?,?,?,?,?,?)",
                       (kind, subject, scope, version, actor, now, json.dumps(before), json.dumps(after)))
            return after

    def seed(self, kind, subject, changes):
        """Initialize a profile once. Restarting never overwrites edited defaults."""
        try:
            self.patch(kind, subject, changes, 0, source="demo_seed", actor="system")
        except VersionConflict:
            pass

    def clear(self):
        """Explicit demo reset only; ordinary construction and new calls preserve memory."""
        with self.connect() as db:
            db.execute("DELETE FROM preferences")
            db.execute("DELETE FROM profiles")
            db.execute("DELETE FROM preference_changes")
