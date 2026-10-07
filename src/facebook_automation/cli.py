"""One JSON object on stdout for every command, help and parser error."""

import argparse
import asyncio
from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sys
import tomllib

from .errors import PublishError
from .service import accept_ui, login, publish, reconcile
from .state import State, account_name, profile_url, request_id, result


class HelpRequested(Exception):
    def __init__(self, content):
        self.content = content


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PublishError("INVALID_ARGUMENTS", message)

    def print_help(self, file=None):
        raise HelpRequested({"help": self.format_help()})


class VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            current = version("facebook-automation")
        except PackageNotFoundError:
            current = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())["project"]["version"]
        raise HelpRequested({"version": current})


def timeout_seconds(value):
    try:
        number = int(value)
        if number < 1:
            raise ValueError()
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number of seconds") from exc


def parser():
    root = Parser(prog="facebook-publish", description="Publish a Reel from an explicitly verified personal Facebook profile.")
    root.add_argument("--version", action=VersionAction, nargs=0)
    commands = root.add_subparsers(dest="command", required=True)
    common = Parser(add_help=False)
    common.add_argument("--state-dir", type=Path, default=Path(os.environ.get("FACEBOOK_PUBLISH_STATE_DIR", Path(__file__).parents[2] / ".local" / "personal")))
    common.add_argument("--account", required=True)
    runtime = Parser(add_help=False)
    runtime.add_argument("--timeout-seconds", type=timeout_seconds, default=1800)
    log = commands.add_parser("login", parents=[common, runtime])
    log.add_argument("--profile-url", required=True)
    log.add_argument("--display-name", required=True)
    log.add_argument("--import-firefox", type=Path)
    for name in ("publish-reel", "accept-ui"):
        command = commands.add_parser(name, parents=[common, runtime])
        command.add_argument("--video", required=True)
        command.add_argument("--privacy", required=True, choices=["public"])
        command.add_argument("--ai-generated", action="store_true")
        caption = command.add_mutually_exclusive_group()
        caption.add_argument("--caption-file")
        caption.add_argument("--metadata-file")
        if name == "publish-reel":
            command.add_argument("--request-id", required=True)
            command.add_argument("--headless", action="store_true")
        else:
            command.add_argument("--ui-contract", required=True)
    status = commands.add_parser("status", parents=[common])
    status.add_argument("--request-id", required=True)
    recovery = commands.add_parser("reconcile", parents=[common])
    recovery.add_argument("--request-id", required=True)
    recovery.add_argument("--post-url", required=True)
    recovery.add_argument("--confirm-matching-reel", action="store_true", required=True)
    accounts = commands.add_parser("accounts")
    actions = accounts.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--state-dir", type=Path, default=common.get_default("state_dir"))
    return root


def raw_flag(raw, flag):
    for index, token in enumerate(raw):
        if token.startswith(flag + "="):
            return token[len(flag) + 1:]
        if token == flag and index + 1 < len(raw):
            return raw[index + 1]
    return None


def failure(args, error, raw):
    payload = {"platform": "facebook", "status": "failed", "error": error.json()}
    command = args.command if args else (raw[0] if raw else None)
    if command in {"publish-reel", "status", "reconcile"}:
        payload.update({"account": getattr(args, "account", raw_flag(raw, "--account")),
                        "request_id": getattr(args, "request_id", raw_flag(raw, "--request-id")), "post_url": None})
    elif command in {"login", "accept-ui"}:
        payload["account"] = getattr(args, "account", raw_flag(raw, "--account"))
    return payload


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    args, state = None, None
    try:
        with redirect_stdout(sys.stderr):
            args = parser().parse_args(raw)
            if hasattr(args, "account"):
                account_name(args.account)
            if hasattr(args, "request_id"):
                request_id(args.request_id)
            if hasattr(args, "profile_url"):
                args.profile_url = profile_url(args.profile_url)
            old_umask = os.umask(0o077)
            try:
                state = State(args.state_dir.expanduser().absolute())
                if args.command == "accounts":
                    payload, code = {"platform": "facebook", "status": "ok", "accounts": state.accounts(), "error": None}, 0
                elif args.command == "status":
                    row = state.get(args.account, args.request_id)
                    if row is None:
                        raise PublishError("REQUEST_NOT_FOUND", "No journal entry for this account and request ID.")
                    if row["status"] == "submitting":
                        row["status"] = "uncertain"
                        row["error"] = json.dumps({"code": "INTERRUPTED_SUBMISSION", "message": "Submission may have occurred; inspect the specific Reel manually."})
                    payload, code = result(row), 0
                elif args.command == "reconcile":
                    payload, code = reconcile(state, args)
                else:
                    handler = {"login": login, "publish-reel": publish, "accept-ui": accept_ui}[args.command]
                    payload, code = asyncio.run(handler(state, args))
            finally:
                os.umask(old_umask)
    except HelpRequested as exc:
        payload, code = {"platform": "facebook", "status": "ok", **exc.content, "error": None}, 0
    except PublishError as exc:
        payload, code = failure(args, exc, raw), exc.exit_code
    except KeyboardInterrupt:
        payload, code = failure(args, PublishError("INTERRUPTED", "Command interrupted; read status before retrying.", 3), raw), 3
        if args and state and args.command == "publish-reel":
            try:
                row = state.get(args.account, args.request_id)
                if row and row["status"] in {"submitting", "uncertain"}:
                    payload, code = result(state.recover(row)), 4
            except Exception:
                payload, code = failure(args, PublishError("INTERRUPTED_UNCERTAIN", "Journal unavailable after interruption; inspect this request manually.", 4), raw), 4
                payload["status"] = "uncertain"
    except Exception:
        payload, code = failure(args, PublishError("TECHNICAL_FAILURE", "Local state or browser operation failed.", 3), raw), 3
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
