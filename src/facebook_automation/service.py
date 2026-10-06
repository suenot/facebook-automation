"""Service entry points; the journal boundary always precedes the sole Post call."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

from .browser import ADAPTER_REVISION, RUNTIME, FacebookReels, accepted_contract, browser_session, load_contract
from .errors import PublishError
from .media import copy_video, probe_video, read_caption
from .state import State, canonical, result


def normalized(args):
    if args.privacy != "public":
        raise PublishError("INVALID_PRIVACY", "Only explicitly requested public Page Reels are supported.")
    return {"type": "reel", "video": str(Path(args.video).expanduser().absolute()),
            "caption_file": str(Path(args.caption_file).expanduser().absolute()) if args.caption_file else None,
            "metadata_file": str(Path(args.metadata_file).expanduser().absolute()) if args.metadata_file else None,
            "privacy": args.privacy}


def prepared(args, directory: Path):
    arguments = normalized(args)
    video = directory / ("video" + Path(arguments["video"]).suffix.lower())
    digest = copy_video(arguments["video"], video)
    probe_video(video)
    caption = read_caption(arguments["caption_file"], arguments["metadata_file"])
    details = {**arguments, "video_sha256": digest, "caption": caption}
    fingerprint = hashlib.sha256(canonical(details).encode()).hexdigest()
    return arguments, video, caption, details, fingerprint


async def diagnostic_error(exc, adapter, submitted):
    if isinstance(exc, PublishError):
        error = exc
    else:
        error = PublishError("SUBMISSION_UNCERTAIN" if submitted else "TECHNICAL_FAILURE",
                             "Browser operation failed or timed out; inspect local diagnostics.", 4 if submitted else 3)
    try:
        async with asyncio.timeout(5):
            screenshot = await adapter.screenshot()
        if screenshot:
            error = PublishError(error.code, f"{error.message} Screenshot: {screenshot}", error.exit_code)
    except BaseException:
        pass
    return error


def cleanup_temporary(path: Path):
    try:
        shutil.rmtree(path)
    except Exception:
        print(f"Could not remove private temporary directory {path}; remove it manually.", file=sys.stderr)


async def publish(state: State, args, session_factory=browser_session, adapter_factory=FacebookReels):
    with state.lock(args.account):
        account = state.account(args.account)
        arguments = normalized(args)
        row = state.get(args.account, args.request_id)
        if row:
            if row["arguments"] != canonical(arguments):
                raise PublishError("REQUEST_CONFLICT", "Request ID was already used with different arguments.")
            row = state.recover(row)
            if row["status"] in {"published", "uncertain"}:
                return result(row), 0 if row["status"] == "published" else 4
        request_dir = state.request_dir(args.account, args.request_id)
        temporary = Path(tempfile.mkdtemp(prefix="upload-", dir=request_dir))
        submitted, adapter = False, None
        try:
            _, video, caption, details, fingerprint = prepared(args, temporary)
            if row and row["fingerprint"] != fingerprint:
                raise PublishError("REQUEST_CONFLICT", "Request ID was already used with different caption or media bytes.")
            state.start(args.account, args.request_id, arguments, fingerprint, details)
            try:
                contract = accepted_contract(account)
                async with asyncio.timeout(args.timeout_seconds):
                    async with session_factory(state.profile(args.account), args.headless) as context:
                        adapter = adapter_factory(context, contract, request_dir / "diagnostics")
                        try:
                            await adapter.open_editor(account)
                            before = await adapter.existing_urls()
                            await adapter.prepare(video, caption, args.privacy)
                            await adapter.verify(account, caption, args.privacy)
                            state.finish(args.account, args.request_id, "submitting")
                            submitted = True
                            await adapter.submit()
                            url = await adapter.confirm(before, account, caption)
                            if not url:
                                raise PublishError("UNCONFIRMED_POST", "A concrete URL for the submitted Reel was not confirmed.", 4)
                            state.finish(args.account, args.request_id, "published", post_url=url)
                        except BaseException as exc:
                            raise await diagnostic_error(exc, adapter, submitted) from exc
                return result(state.get(args.account, args.request_id)), 0
            except BaseException as exc:
                try:
                    saved = state.get(args.account, args.request_id)
                except Exception:
                    saved = None
                if saved and saved["status"] == "published":
                    return result(saved), 0
                error = exc if isinstance(exc, PublishError) else PublishError(
                    "SUBMISSION_UNCERTAIN" if submitted else "TECHNICAL_FAILURE",
                    "Browser operation failed or was interrupted; read status before retrying.", 4 if submitted else 3)
                status = "uncertain" if submitted else "failed"
                try:
                    state.finish(args.account, args.request_id, status, error.json(), adapter.post_url if adapter else None)
                    output = result(state.get(args.account, args.request_id))
                except Exception:
                    output = {"platform": "facebook", "account": args.account, "request_id": args.request_id,
                              "status": status, "post_url": None, "error": {"code": "JOURNAL_FAILURE", "message": "Could not persist the result; inspect this request and the journal manually."}}
                return output, 4 if submitted else error.exit_code
        finally:
            cleanup_temporary(temporary)


async def wait_for_enter():
    if not sys.stdin.isatty():
        raise PublishError("INTERACTIVE_LOGIN_REQUIRED", "Run visible login from an interactive terminal.")
    loop = asyncio.get_running_loop()
    complete = loop.create_future()

    def ready():
        if not complete.done():
            complete.set_result(sys.stdin.readline())

    descriptor = sys.stdin.fileno()
    loop.add_reader(descriptor, ready)
    try:
        if not await complete:
            raise PublishError("INTERACTIVE_LOGIN_REQUIRED", "Login terminal input closed before confirmation.")
    finally:
        loop.remove_reader(descriptor)


async def login(state: State, args, session_factory=browser_session, adapter_factory=FacebookReels):
    expected = {"page_url": args.page_url, "display_name": args.display_name}
    contract = load_contract(args.ui_contract, expected)
    with state.lock(args.account):
        async with asyncio.timeout(args.timeout_seconds):
            async with session_factory(state.profile(args.account), False) as context:
                adapter = adapter_factory(context, contract, state.root / "accounts" / args.account / "diagnostics")
                adapter.page = await context.new_page()
                await adapter.page.goto(expected["page_url"], wait_until="domcontentloaded")
                print("Sign in, finish 2FA and switch to the explicitly requested Page in Camoufox. Press Enter here when ready.", file=sys.stderr)
                await wait_for_enter()
                try:
                    await adapter.verify_identity(expected)
                    state.bind(args.account, expected["page_url"], expected["display_name"])
                except BaseException as exc:
                    raise await diagnostic_error(exc, adapter, False) from exc
        return {"platform": "facebook", "account": args.account, "status": "authenticated", "error": None}, 0


async def accept_ui(state: State, args, session_factory=browser_session, adapter_factory=FacebookReels):
    """Visible rehearsal: no journal submission, no call to submit()."""
    with state.lock(args.account):
        account = state.account(args.account)
        contract = load_contract(args.ui_contract, account)
        directory = state.root / "accounts" / args.account
        temporary = Path(tempfile.mkdtemp(prefix="preflight-", dir=directory))
        try:
            _, video, caption, _, _ = prepared(args, temporary)
            async with asyncio.timeout(args.timeout_seconds):
                async with session_factory(state.profile(args.account), False) as context:
                    adapter = adapter_factory(context, contract, directory / "diagnostics")
                    try:
                        await adapter.open_editor(account)
                        await adapter.prepare(video, caption, args.privacy)
                        await adapter.verify(account, caption, args.privacy)
                        evidence = await adapter.screenshot("accepted-editor.png")
                        if evidence is None:
                            raise PublishError("ACCEPTANCE_EVIDENCE_MISSING", "Cannot record the visible preflight screenshot.", 3)
                        with evidence.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        state.accept_ui(args.account, {"adapter_revision": ADAPTER_REVISION, "runtime": RUNTIME,
                            "confirmed_at": datetime.now(timezone.utc).isoformat(), "evidence_sha256": digest,
                            "evidence_path": str(evidence), "contract": contract})
                    except BaseException as exc:
                        raise await diagnostic_error(exc, adapter, False) from exc
            return {"platform": "facebook", "account": args.account, "status": "ui_accepted", "evidence_path": str(evidence), "error": None}, 0
        finally:
            cleanup_temporary(temporary)


def reconcile(state: State, args):
    """Explicit manual attestation of one inspected Reel; never open an editor."""
    from .browser import reel_url

    with state.lock(args.account):
        state.account(args.account)
        row = state.get(args.account, args.request_id)
        if row is None:
            raise PublishError("REQUEST_NOT_FOUND", "No journal entry for this account and request ID.")
        row = state.recover(row)
        url = reel_url(args.post_url)
        if row["status"] == "published":
            if row["post_url"] != url:
                raise PublishError("REQUEST_CONFLICT", "This request already has a different confirmed Reel URL.")
            return result(row), 0
        if row["status"] != "uncertain" or not args.confirm_matching_reel:
            raise PublishError("RECONCILE_UNAVAILABLE", "Manually inspect the specific Reel, author, caption, video and Public audience before reconciliation.")
        state.finish(args.account, args.request_id, "published", post_url=url)
        return result(state.get(args.account, args.request_id)), 0
