"""Private state, account ownership and durable submission boundaries."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .errors import PublishError


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def account_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PublishError("INVALID_ACCOUNT", "Account must contain ASCII letters, digits, _ or -.")
    return value


def request_id(value: str) -> str:
    if not value.strip():
        raise PublishError("INVALID_REQUEST_ID", "Request ID must be nonempty.")
    return value


def profile_url(value: str) -> str:
    """Accept a canonical personal Facebook profile, never a post or redirect."""
    url = urlsplit(value)
    if url.scheme != "https" or url.netloc.lower() not in {"facebook.com", "www.facebook.com"} or url.fragment:
        raise PublishError("INVALID_PROFILE_URL", "Use an HTTPS facebook.com profile handle or numeric profile.php?id URL.")
    if url.path == "/profile.php":
        query = parse_qs(url.query, keep_blank_values=True)
        if set(query) == {"id"} and len(query["id"]) == 1 and re.fullmatch(r"\d+", query["id"][0]):
            return "https://www.facebook.com/profile.php?id=" + query["id"][0]
    handle = url.path.strip("/")
    reserved = {"reel", "reels", "watch", "login", "pages", "groups", "settings", "me", "home.php", "profile.php"}
    if not url.query and re.fullmatch(r"[A-Za-z0-9.]+", handle) and handle.lower() not in reserved:
        return "https://www.facebook.com/" + handle.lower()
    raise PublishError("INVALID_PROFILE_URL", "Use the exact canonical personal profile URL, without a post path or tracking query.")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class State:
    def __init__(self, root: Path):
        self.root = private_dir(root)
        self.db_path = self.root / "journal.sqlite3"
        with self.connect() as db:
            legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
            if legacy and "page_url" in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
                raise PublishError("LEGACY_PAGE_STATE", "This state directory belongs to the old Page publisher. Use a fresh state directory for personal-profile Reels.", 3)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY, profile_url TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, ui_contract TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    account TEXT NOT NULL, request_id TEXT NOT NULL,
                    arguments TEXT NOT NULL, fingerprint TEXT NOT NULL, details TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('preparing','submitting','published','uncertain','failed')),
                    post_url TEXT, error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(account, request_id)
                );
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self, account: str):
        directory = private_dir(self.root / "accounts" / account_name(account))
        descriptor = os.open(directory / "profile.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PublishError("PROFILE_BUSY", "Another process is using this account profile.", 3) from exc
            yield
        finally:
            os.close(descriptor)

    def profile(self, account: str) -> Path:
        return private_dir(self.root / "accounts" / account_name(account) / "profile")

    def account(self, account: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE account=?", (account,)).fetchone()
        if row is None:
            raise PublishError("UNKNOWN_ACCOUNT", "Account is not bound; run visible login first.")
        return dict(row)

    def bind(self, account: str, profile_url: str, display_name: str):
        if not display_name.strip():
            raise PublishError("INVALID_DISPLAY_NAME", "Expected profile display name must be nonempty.")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM accounts WHERE account=? OR profile_url=?", (account, profile_url)).fetchall()
            if any(row["account"] != account or row["profile_url"] != profile_url or row["display_name"] != display_name for row in rows):
                raise PublishError("ACCOUNT_BINDING_CONFLICT", "Alias or canonical profile is already bound differently.")
            if not rows:
                try:
                    db.execute("INSERT INTO accounts(account,profile_url,display_name) VALUES(?,?,?)", (account, profile_url, display_name))
                except sqlite3.IntegrityError as exc:
                    raise PublishError("ACCOUNT_BINDING_CONFLICT", "This profile was concurrently bound to another alias.") from exc

    def accept_ui(self, account: str, contract: dict):
        with self.connect() as db:
            db.execute("UPDATE accounts SET ui_contract=? WHERE account=?", (canonical(contract), account))

    def accounts(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT account,profile_url,display_name FROM accounts ORDER BY account")]

    def get(self, account: str, request: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE account=? AND request_id=?", (account, request)).fetchone()
        return dict(row) if row else None

    def start(self, account: str, request: str, arguments: dict, fingerprint: str, details: dict):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE account=? AND request_id=?", (account, request)).fetchone()
            if row is not None:
                if row["arguments"] != canonical(arguments) or row["fingerprint"] != fingerprint:
                    raise PublishError("REQUEST_CONFLICT", "Request ID was already used with different arguments or bytes.")
                if row["status"] not in {"failed", "preparing"}:
                    raise PublishError("REQUEST_NOT_RETRYABLE", "Read status; this request has already reached submission.", 4)
                db.execute("UPDATE requests SET status='preparing',error=NULL,post_url=NULL,updated_at=CURRENT_TIMESTAMP WHERE account=? AND request_id=?", (account, request))
            else:
                db.execute("INSERT INTO requests(account,request_id,arguments,fingerprint,details,status) VALUES(?,?,?,?,?,'preparing')", (account, request, canonical(arguments), fingerprint, canonical(details)))

    def finish(self, account: str, request: str, status: str, error=None, post_url=None):
        with self.connect() as db:
            row = db.execute("SELECT status FROM requests WHERE account=? AND request_id=?", (account, request)).fetchone()
            allowed = {"preparing": {"submitting", "failed"}, "submitting": {"published", "uncertain"}, "uncertain": {"published", "uncertain"}}
            if row is None or status not in allowed.get(row["status"], set()):
                raise PublishError("INVALID_TRANSITION", "Refusing an unsafe journal transition.", 3)
            db.execute("UPDATE requests SET status=?,error=?,post_url=?,updated_at=CURRENT_TIMESTAMP WHERE account=? AND request_id=?", (status, canonical(error) if error else None, post_url, account, request))

    def recover(self, row: dict):
        if row["status"] == "submitting":
            self.finish(row["account"], row["request_id"], "uncertain", {
                "code": "INTERRUPTED_SUBMISSION", "message": "Submission was interrupted; inspect the specific Reel before any new request."
            }, row["post_url"])
            return self.get(row["account"], row["request_id"])
        return row

    def request_dir(self, account: str, request: str) -> Path:
        key = hashlib.sha256(request.encode()).hexdigest()
        return private_dir(self.root / "requests" / account_name(account) / key)


def result(row: dict) -> dict:
    return {"platform": "facebook", "account": row["account"], "request_id": row["request_id"],
            "status": row["status"], "post_url": row["post_url"],
            "error": json.loads(row["error"]) if row["error"] else None}
