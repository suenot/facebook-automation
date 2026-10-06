from argparse import Namespace
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys
import sqlite3

import pytest

from facebook_automation.browser import ADAPTER_REVISION, RUNTIME, SELECTORS
from facebook_automation.cli import main
from facebook_automation.errors import PublishError
from facebook_automation.media import copy_video, read_caption
from facebook_automation.service import accept_ui, normalized, publish, reconcile
from facebook_automation.state import State, canonical, profile_url

PAGE = "https://www.facebook.com/caribbeanawesome"
NAME = "Caribbean Awesome"
POST = "https://www.facebook.com/reel/123456789"


def candidate():
    return {"schema_version": "1.0.0", "target": {"kind": "page", "page_url": PAGE, "display_name": NAME},
            "editor_url": "https://www.facebook.com/reels/create",
            "selectors": {key: "#" + key for key in SELECTORS},
            "steps_before_caption": [], "steps_after_caption": []}


def request(tmp_path, **changes):
    video = tmp_path / "source.mp4"
    if not video.exists():
        video.write_bytes(b"original media bytes")
    values = dict(account="brand", request_id="one", video=str(video), caption_file=None,
                  metadata_file=None, privacy="public", headless=True, timeout_seconds=30)
    values.update(changes)
    return Namespace(**values)


@asynccontextmanager
async def session(*_):
    yield object()


class Adapter:
    clicks = 0
    failure = None
    uploaded = None
    post_url = None
    state = None
    seen_privacy = None

    def __init__(self, context, contract, diagnostics):
        self.diagnostics = diagnostics
        diagnostics.mkdir(parents=True, exist_ok=True)

    async def open_editor(self, account):
        assert account["page_url"] == PAGE and account["display_name"] == NAME

    async def existing_urls(self):
        return {"https://www.facebook.com/reel/111"}

    async def prepare(self, video, caption, privacy):
        type(self).uploaded = video
        type(self).seen_privacy = privacy
        assert video.read_bytes() == b"original media bytes"

    async def verify(self, account, caption, privacy):
        if self.failure == "verify":
            raise PublishError("ACCOUNT_MISMATCH", "Wrong Page")
        if self.failure == "privacy":
            raise PublishError("PRIVACY_MISMATCH", "Not Public", 3)

    async def submit(self):
        if self.state:
            assert self.state.get("brand", "one")["status"] == "submitting"
        type(self).clicks += 1
        if self.failure == "submit":
            raise TimeoutError()
        if self.failure == "cancel":
            import asyncio
            raise asyncio.CancelledError()

    async def confirm(self, *_):
        if self.failure == "confirm":
            raise PublishError("UNCONFIRMED_POST", "No concrete matching Reel", 4)
        if self.failure == "no-url":
            return None
        return POST

    async def screenshot(self, name="last-failure.png"):
        path = self.diagnostics / name
        path.write_bytes(b"private screenshot evidence")
        return path


@pytest.fixture
def state(tmp_path, monkeypatch):
    import facebook_automation.service as service
    monkeypatch.setattr(service, "probe_video", lambda _: None)
    Adapter.clicks, Adapter.failure, Adapter.uploaded, Adapter.state, Adapter.seen_privacy = 0, None, None, None, None
    state = State(tmp_path / "state")
    state.bind("brand", PAGE, NAME)
    state.accept_ui("brand", {"adapter_revision": ADAPTER_REVISION, "runtime": RUNTIME,
                              "evidence_sha256": "fixture", "contract": candidate()})
    return state


@pytest.mark.parametrize("flags,code", [([], 2), (["publish-reel"], 2), (["--help"], 0),
                                       (["publish-reel", "--help"], 0), (["--version"], 0),
                                       (["accounts", "list"], 0)])
def test_single_json(flags, code, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACEBOOK_PUBLISH_STATE_DIR", str(tmp_path / "state"))
    assert main(flags) == code
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert isinstance(json.loads(output), dict)


def test_parser_errors_keep_request_fields(capsys):
    assert main(["publish-reel", "--account", "brand", "--request-id", "one"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["account"] == "brand" and output["request_id"] == "one" and output["post_url"] is None


@pytest.mark.parametrize("schema_version", ["1.0.0", "2.0.0"])
def test_exact_caption_and_complete_metadata(tmp_path, schema_version):
    caption = tmp_path / "caption.txt"
    caption.write_bytes(b"  Exact\r\nCaption #tag\n")
    assert read_caption(str(caption)) == "  Exact\r\nCaption #tag\n"
    from metadata_automation import generate
    metadata = generate({"language": "en", "title": "A title", "summary": "A summary"},
                        schema_version=schema_version)
    metadata["platforms"]["facebook"] = {"caption": "  Exact\r\nCaption #tag\n", "hashtags": ["#tag"]}
    file = tmp_path / "metadata.json"
    file.write_text(json.dumps(metadata), encoding="utf-8")
    assert read_caption(metadata_file=str(file)) == "  Exact\r\nCaption #tag\n"
    metadata["platforms"].pop("pinterest" if schema_version == "2.0.0" else "vk")
    file.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(PublishError, match="invalid"):
        read_caption(metadata_file=str(file))


@pytest.mark.parametrize("change", [lambda m: m.update(schema_version="3.0.0"),
                                    lambda m: m.update(account="evil"),
                                    lambda m: m["platforms"]["facebook"].update(privacy="private")])
def test_metadata_rejects_versions_and_runtime_fields(tmp_path, change):
    from metadata_automation import generate
    metadata = generate({"language": "en", "title": "A title", "summary": "A summary"})
    change(metadata)
    file = tmp_path / "metadata.json"
    file.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(PublishError):
        read_caption(metadata_file=str(file))


def test_private_snapshot_is_independent(tmp_path):
    source, copy = tmp_path / "source.mp4", tmp_path / "copy.mp4"
    source.write_bytes(b"upload me")
    digest = copy_video(str(source), copy)
    source.write_bytes(b"replacement")
    assert copy.read_bytes() == b"upload me" and len(digest) == 64
    assert copy.stat().st_mode & 0o777 == 0o600


def test_cross_process_lock(state):
    code = "from pathlib import Path\nfrom facebook_automation.state import State\nfrom facebook_automation.errors import PublishError\ns=State(Path(__import__('sys').argv[1]))\ntry:\n with s.lock('brand'): raise SystemExit(99)\nexcept PublishError as e: raise SystemExit(e.exit_code)"
    with state.lock("brand"):
        child = subprocess.run([sys.executable, "-c", code, str(state.root)], capture_output=True)
    assert child.returncode == 3, child.stderr.decode()


def test_canonical_page_and_alias_cannot_rebind(state):
    assert profile_url("https://facebook.com/CaribbeanAwesome/") == PAGE
    assert profile_url("https://facebook.com/profile.php?id=123") == "https://www.facebook.com/profile.php?id=123"
    with pytest.raises(PublishError):
        state.bind("brand", "https://www.facebook.com/personal", "Someone else")
    with pytest.raises(PublishError):
        state.bind("other-alias", PAGE, NAME)
    with pytest.raises(PublishError):
        profile_url("https://www.facebook.com/reel/123")


async def test_success_boundary_and_cached_result(state, tmp_path):
    args = request(tmp_path)
    Adapter.state = state
    first, code = await publish(state, args, session, Adapter)
    assert code == 0 and first["status"] == "published" and first["post_url"] == POST
    assert Adapter.clicks == 1 and Adapter.seen_privacy == "public"
    assert Adapter.uploaded != Path(args.video) and not Adapter.uploaded.exists()
    Path(args.video).unlink()
    second, code = await publish(state, args, session, Adapter)
    assert second == first and code == 0 and Adapter.clicks == 1


@pytest.mark.parametrize("failure", ["submit", "confirm", "cancel", "no-url"])
async def test_uncertain_never_repeats_post(state, tmp_path, failure):
    args = request(tmp_path)
    Adapter.failure = failure
    first, code = await publish(state, args, session, Adapter)
    assert code == 4 and first["status"] == "uncertain" and first["post_url"] is None and Adapter.clicks == 1
    Path(args.video).unlink()
    second, code = await publish(state, args, session, Adapter)
    assert second == first and code == 4 and Adapter.clicks == 1


@pytest.mark.parametrize("failure,exit_code", [("verify", 2), ("privacy", 3)])
async def test_failed_preflight_retries_only_same_fingerprint(state, tmp_path, failure, exit_code):
    args = request(tmp_path)
    Adapter.failure = failure
    first, code = await publish(state, args, session, Adapter)
    assert code == exit_code and first["status"] == "failed" and Adapter.clicks == 0
    Path(args.video).write_bytes(b"different bytes")
    with pytest.raises(PublishError, match="different caption or media bytes"):
        await publish(state, args, session, Adapter)
    Path(args.video).write_bytes(b"original media bytes")
    Adapter.failure = None
    second, code = await publish(state, args, session, Adapter)
    assert code == 0 and second["status"] == "published" and Adapter.clicks == 1


async def test_argument_conflict_without_browser(state, tmp_path):
    args = request(tmp_path)
    await publish(state, args, session, Adapter)
    args.video = str(tmp_path / "other.mp4")
    with pytest.raises(PublishError, match="different arguments"):
        await publish(state, args, session, Adapter)
    assert Adapter.clicks == 1


async def test_unaccepted_ui_never_opens_browser(state, tmp_path):
    state.accept_ui("brand", None)
    args = request(tmp_path)
    async def forbidden(*_):
        raise AssertionError("Unaccepted UI must not open a browser")
    output, code = await publish(state, args, forbidden, Adapter)
    assert code == 3 and output["error"]["code"] == "UI_NOT_VERIFIED" and Adapter.clicks == 0


async def test_crash_recovers_boundary_to_uncertain(state, tmp_path):
    args = request(tmp_path)
    state.start("brand", "one", normalized(args), "fingerprint", {})
    state.finish("brand", "one", "submitting")
    Path(args.video).unlink()
    output, code = await publish(state, args, session, Adapter)
    assert code == 4 and output["status"] == "uncertain" and output["error"]["code"] == "INTERRUPTED_SUBMISSION"
    assert Adapter.clicks == 0 and state.get("brand", "one")["status"] == "uncertain"


def test_status_read_only_even_at_boundary(state, tmp_path, capsys):
    args = request(tmp_path)
    state.start("brand", "one", normalized(args), "fingerprint", {})
    state.finish("brand", "one", "submitting")
    assert main(["status", "--state-dir", str(state.root), "--account", "brand", "--request-id", "one"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "uncertain"
    assert state.get("brand", "one")["status"] == "submitting"


async def test_visible_acceptance_never_posts(state, tmp_path):
    args = request(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(candidate()))
    args.ui_contract = str(contract)
    output, code = await accept_ui(state, args, session, Adapter)
    assert code == 0 and output["status"] == "ui_accepted" and Adapter.clicks == 0
    assert state.get("brand", "one") is None
    assert json.loads(state.account("brand")["ui_contract"])["evidence_sha256"]


async def test_reconcile_requires_specific_manual_attestation(state, tmp_path):
    args = request(tmp_path)
    Adapter.failure = "confirm"
    await publish(state, args, session, Adapter)
    args.post_url, args.confirm_matching_reel = POST, False
    with pytest.raises(PublishError):
        reconcile(state, args)
    args.confirm_matching_reel = True
    output, code = reconcile(state, args)
    assert code == 0 and output["post_url"] == POST and Adapter.clicks == 1


@pytest.mark.parametrize("outage,persisted", [("read-after-submit", "uncertain"),
                                             ("read-after-confirm", "published"),
                                             ("whole-journal", "submitting")])
async def test_journal_outage_after_post_remains_uncertain(state, tmp_path, monkeypatch, outage, persisted):
    args = request(tmp_path)
    original_get, original_connect = state.get, state.connect
    def unavailable(*_):
        raise sqlite3.OperationalError("Synthetic journal outage")
    class JournalOutage(Adapter):
        async def submit(self):
            await super().submit()
            if outage == "whole-journal":
                monkeypatch.setattr(state, "connect", unavailable)
            else:
                monkeypatch.setattr(state, "get", unavailable)
            if outage != "read-after-confirm":
                raise TimeoutError()
    output, code = await publish(state, args, session, JournalOutage)
    assert code == 4 and output["status"] == "uncertain" and output["error"]["code"] == "JOURNAL_FAILURE"
    assert JournalOutage.clicks == 1
    monkeypatch.setattr(state, "get", original_get)
    monkeypatch.setattr(state, "connect", original_connect)
    assert state.get("brand", "one")["status"] == persisted
    again, code = await publish(state, args, session, JournalOutage)
    assert code == (0 if persisted == "published" else 4)
    assert again["status"] == ("published" if persisted == "published" else "uncertain")
    assert JournalOutage.clicks == 1


@pytest.mark.parametrize("failure,expected_code,expected_status", [(None, 0, "published"),
                                                                ("confirm", 4, "uncertain"),
                                                                ("verify", 2, "failed")])
async def test_cleanup_failure_preserves_publish_result(state, tmp_path, monkeypatch, capsys, failure, expected_code, expected_status):
    import facebook_automation.service as service
    def unavailable(_):
        raise OSError("Sensitive exception text must not be logged")
    monkeypatch.setattr(service.shutil, "rmtree", unavailable)
    Adapter.failure = failure
    output, code = await publish(state, request(tmp_path), session, Adapter)
    assert code == expected_code and output["status"] == expected_status
    stderr = capsys.readouterr().err
    assert "Could not remove private temporary directory" in stderr
    assert "Sensitive exception" not in stderr


async def test_cleanup_failure_preserves_acceptance(state, tmp_path, monkeypatch, capsys):
    import facebook_automation.service as service
    def unavailable(_):
        raise OSError("Synthetic cleanup failure")
    monkeypatch.setattr(service.shutil, "rmtree", unavailable)
    args = request(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(candidate()))
    args.ui_contract = str(contract)
    output, code = await accept_ui(state, args, session, Adapter)
    assert code == 0 and output["status"] == "ui_accepted" and Adapter.clicks == 0
    assert "Could not remove private temporary directory" in capsys.readouterr().err
