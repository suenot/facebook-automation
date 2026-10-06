from argparse import Namespace
from contextlib import asynccontextmanager
import sqlite3
import time

import pytest

from facebook_automation.errors import PublishError
from facebook_automation.firefox_cookies import firefox_cookies
from facebook_automation.service import login
from facebook_automation.state import State


def profile_db(tmp_path, version=17):
    profile = tmp_path / "firefox-profile"
    profile.mkdir()
    db = sqlite3.connect(profile / "cookies.sqlite")
    db.execute(f"PRAGMA user_version={version}")
    db.execute("""CREATE TABLE moz_cookies (
        name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER,
        isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER, originAttributes TEXT
    )""")
    return profile, db


def test_firefox_snapshot_is_site_scoped_and_preserves_attributes(tmp_path):
    profile, db = profile_db(tmp_path)
    future = int((time.time() + 86400) * 1000)
    rows = [
        ("c_user", "secret-a", ".facebook.com", "/", future, 1, 1, 2, ""),
        ("xs", "secret-b", "www.facebook.com", "/", future, 1, 1, 256, ""),
        ("wrong", "other-site-secret", "accounts.instagram.com", "/", future, 1, 0, 1, ""),
        ("expired", "old", ".facebook.com", "/", 1, 1, 1, 0, ""),
    ]
    db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    cookies = firefox_cookies(profile)
    assert {cookie["name"] for cookie in cookies} == {"c_user", "xs"}
    assert cookies[0] == {"name": "c_user", "value": "secret-a", "domain": ".facebook.com",
                          "path": "/", "expires": future / 1000, "secure": True, "httpOnly": True, "sameSite": "Strict"}
    assert cookies[1]["domain"] == "www.facebook.com" and cookies[1]["path"] == "/"
    assert "url" not in cookies[1] and "sameSite" not in cookies[1]
    with sqlite3.connect(profile / "cookies.sqlite") as original:
        assert original.execute("SELECT count(*) FROM moz_cookies").fetchone()[0] == len(rows)


def test_firefox_skips_partitioned_cookies_and_rejects_unknown_schema(tmp_path):
    profile, db = profile_db(tmp_path)
    db.execute("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?,?)", (
        "c_user", "secret", ".facebook.com", "/", int((time.time() + 86400) * 1000), 1, 1, 1, "^userContextId=2"))
    db.commit()
    db.close()
    with pytest.raises(PublishError) as error:
        firefox_cookies(profile)
    assert error.value.code == "FIREFOX_SESSION_MISSING" and "secret" not in error.value.message
    with sqlite3.connect(profile / "cookies.sqlite") as db:
        db.execute("PRAGMA user_version=18")
    with pytest.raises(PublishError) as error:
        firefox_cookies(profile)
    assert error.value.code == "FIREFOX_SCHEMA_UNSUPPORTED"


async def test_imported_login_binds_only_verified_personal_profile(tmp_path, monkeypatch):
    import facebook_automation.service as service
    expected = "https://www.facebook.com/profile.php?id=123"
    account = State(tmp_path / "state")
    args = Namespace(account="personal", profile_url=expected, display_name="Person",
                     import_firefox=tmp_path / "firefox-profile", timeout_seconds=30)
    cookie = {"name": "c_user", "value": "secret", "domain": ".facebook.com", "path": "/"}
    monkeypatch.setattr(service, "firefox_cookies", lambda _: [cookie])
    calls = []

    class Context:
        async def add_cookies(self, values):
            calls.append(("add", values))

        async def clear_cookies(self):
            calls.append(("clear", None))

    @asynccontextmanager
    async def session(*_):
        yield Context()

    async def verified(_context, target):
        assert target["profile_url"] == expected

    monkeypatch.setattr(service, "verify_personal_login", verified)
    result, code = await login(account, args, session)
    assert code == 0 and result["status"] == "authenticated"
    assert account.account("personal")["profile_url"] == expected
    assert calls == [("add", [cookie])]


async def test_wrong_imported_identity_clears_cookies_and_does_not_bind(tmp_path, monkeypatch):
    import facebook_automation.service as service
    account = State(tmp_path / "state")
    args = Namespace(account="personal", profile_url="https://www.facebook.com/profile.php?id=123",
                     display_name="Person", import_firefox=tmp_path / "firefox-profile", timeout_seconds=30)
    monkeypatch.setattr(service, "firefox_cookies", lambda _: [{"name": "c_user", "value": "secret"}])
    cleared = []

    class Context:
        async def add_cookies(self, _):
            pass

        async def clear_cookies(self):
            cleared.append(True)

    @asynccontextmanager
    async def session(*_):
        yield Context()

    async def wrong(*_):
        raise PublishError("ACCOUNT_MISMATCH", "Wrong personal profile")

    monkeypatch.setattr(service, "verify_personal_login", wrong)
    with pytest.raises(PublishError) as error:
        await login(account, args, session)
    assert error.value.code == "ACCOUNT_MISMATCH" and cleared == [True]
    with pytest.raises(PublishError) as error:
        account.account("personal")
    assert error.value.code == "UNKNOWN_ACCOUNT"
