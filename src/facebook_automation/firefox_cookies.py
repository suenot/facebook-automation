"""Read only Facebook cookies from a consistent Firefox SQLite snapshot.

Cookie values stay in process memory and are passed directly to Camoufox. They
must never be included in errors, diagnostics or stdout.
"""

import sqlite3
import time
from pathlib import Path

from .errors import PublishError

MAX_COOKIES = 4096
SCHEMA_VERSIONS = {15, 16, 17}
REQUIRED_COLUMNS = {
    "name", "value", "host", "path", "expiry", "isSecure", "isHttpOnly",
    "sameSite", "originAttributes",
}
SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}


def _facebook_host(host: str) -> bool:
    host = host.lstrip(".").lower()
    return host == "facebook.com" or host.endswith(".facebook.com")


def firefox_cookies(profile: Path) -> list[dict]:
    """Snapshot a live WAL database; reject unknown schema and container cookies."""
    source_path = profile.expanduser().resolve() / "cookies.sqlite"
    if not source_path.is_file():
        raise PublishError("FIREFOX_PROFILE_UNREADABLE", "Firefox profile has no readable cookies.sqlite; use visible Camoufox login.")
    source = snapshot = None
    try:
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        snapshot = sqlite3.connect(":memory:")
        deadline = time.monotonic() + 15

        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise TimeoutError()

        source.backup(snapshot, pages=128, progress=progress, sleep=0.05)
        snapshot.row_factory = sqlite3.Row
        version = snapshot.execute("PRAGMA user_version").fetchone()[0]
        if version not in SCHEMA_VERSIONS:
            raise PublishError("FIREFOX_SCHEMA_UNSUPPORTED", "Firefox cookie database schema has not been reviewed for this importer.")
        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(moz_cookies)")}
        if not REQUIRED_COLUMNS <= columns:
            raise PublishError("FIREFOX_SCHEMA_UNSUPPORTED", "Firefox cookie database lacks required cookie attributes.")
        rows = snapshot.execute(
            "SELECT name,value,host,path,expiry,isSecure,isHttpOnly,sameSite,originAttributes "
            "FROM moz_cookies WHERE host=? OR host=? OR host LIKE ? LIMIT ?",
            ("facebook.com", ".facebook.com", "%.facebook.com", MAX_COOKIES + 1),
        ).fetchall()
        if len(rows) > MAX_COOKIES:
            raise PublishError("FIREFOX_COOKIES_UNSUPPORTED", "Firefox has too many Facebook cookies to import safely.")
        now = time.time()
        result = []
        for row in rows:
            host = row["host"]
            if not isinstance(host, str) or not _facebook_host(host):
                continue
            if row["originAttributes"]:
                # Partitioned third-party cookies are invalid in a top-level
                # Facebook context; never flatten their original scope.
                continue
            same_site = row["sameSite"]
            if same_site not in (*SAME_SITE, 256):
                raise PublishError("FIREFOX_COOKIES_UNSUPPORTED", "Firefox cookie has an unsupported SameSite attribute.")
            expiry = row["expiry"] / (1000 if version >= 16 else 1)
            if expiry <= now:
                continue
            if not isinstance(row["name"], str) or not isinstance(row["value"], str) or not row["name"] or not isinstance(row["path"], str):
                raise PublishError("FIREFOX_COOKIES_UNSUPPORTED", "Firefox cookie has invalid required attributes.")
            cookie = {
                "name": row["name"], "value": row["value"], "path": row["path"],
                "expires": expiry, "secure": bool(row["isSecure"]), "httpOnly": bool(row["isHttpOnly"]),
            }
            if host.startswith("."):
                cookie["domain"] = host
            else:
                cookie["url"] = "https://" + host
            if same_site in SAME_SITE:
                cookie["sameSite"] = SAME_SITE[same_site]
            result.append(cookie)
        if not result:
            raise PublishError("FIREFOX_SESSION_MISSING", "Firefox profile has no live Facebook cookies; use visible Camoufox login.")
        return result
    except PublishError:
        raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise PublishError("FIREFOX_PROFILE_BUSY", "Firefox is using its cookie database; quit Firefox, then retry the import.") from exc
        raise PublishError("FIREFOX_PROFILE_UNREADABLE", "macOS denied access to the Firefox cookie database; check Full Disk Access for Codex.") from exc
    except (sqlite3.Error, OSError, TimeoutError, ValueError, TypeError) as exc:
        raise PublishError("FIREFOX_PROFILE_UNREADABLE", "The Firefox cookie snapshot failed; check profile access or use visible Camoufox login.") from exc
    finally:
        if snapshot is not None:
            snapshot.close()
        if source is not None:
            source.close()
