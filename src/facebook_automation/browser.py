"""Facebook adapter activated only after a visible, nonpublishing acceptance run.

The selector contract is local evidence, not a claim about the current Meta UI.
No selectors discovered from page text or guessed fallbacks can reach Post.
"""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit

from .errors import PublishError
from .state import private_dir, profile_url

ADAPTER_REVISION = "facebook-page-reels-1"
RUNTIME = {"camoufox": "0.5.7", "playwright": "1.60.0"}
BROWSER_BUILD = "official/156.0.1-beta.34"
SELECTORS = {
    "actor_menu_button", "current_actor_link", "page_management_marker", "editor", "upload",
    "upload_ready", "caption", "composer_actor_link", "public_button", "public_option",
    "public_selected", "post_button", "success", "result_link", "post_actor_link", "post_caption", "post_public",
}


def facebook_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"facebook.com", "www.facebook.com"}:
        raise PublishError("INVALID_UI_CONTRACT", "Editor URL must be an HTTPS Facebook URL.")
    return value


def authenticated_url(value: str):
    facebook_url(value)
    if urlsplit(value).path.lower().startswith(("/login", "/checkpoint", "/two_step_verification")):
        raise PublishError("LOGIN_REQUIRED", "Facebook requires visible login or 2FA before continuing.")


def validate_contract(value: object, expected: dict) -> dict:
    keys = {"schema_version", "target", "editor_url", "selectors", "steps_before_caption", "steps_after_caption"}
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != "1.0.0":
        raise PublishError("INVALID_UI_CONTRACT", "Use the complete local UI contract with schema_version 1.0.0.")
    target = value["target"]
    if target != {"kind": "page", "page_url": expected["page_url"], "display_name": expected["display_name"]}:
        raise PublishError("ACCOUNT_MISMATCH", "UI contract target differs from the explicitly bound Page.")
    selectors = value["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != SELECTORS or any(
        not isinstance(selector, str) or (key != "actor_menu_button" and not selector.strip())
        for key, selector in selectors.items()
    ):
        raise PublishError("UI_NOT_VERIFIED", "Live Facebook selectors are not recorded; review the local selector template before login/accept-ui.", 3)
    facebook_url(value["editor_url"])
    for name in ("steps_before_caption", "steps_after_caption"):
        if not isinstance(value[name], list) or any(not isinstance(selector, str) or not selector.strip() for selector in value[name]):
            raise PublishError("INVALID_UI_CONTRACT", "Editor steps must be lists of unique CSS button selectors.")
    return value


def load_contract(path: str, expected: dict) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise PublishError("INVALID_UI_CONTRACT", "Cannot read the local UI contract.") from exc
    return validate_contract(document, expected)


def accepted_contract(account: dict) -> dict:
    if not account["ui_contract"]:
        raise PublishError("UI_NOT_VERIFIED", "Run a visible accept-ui preflight for this Page before publishing.", 3)
    try:
        record = json.loads(account["ui_contract"])
        if record["adapter_revision"] != ADAPTER_REVISION or record["runtime"] != RUNTIME or not record["evidence_sha256"]:
            raise ValueError()
        return validate_contract(record["contract"], account)
    except (ValueError, KeyError, TypeError) as exc:
        raise PublishError("UI_NOT_VERIFIED", "Saved UI acceptance is incompatible; run visible accept-ui again.", 3) from exc


def stable_fingerprint(path: Path):
    from camoufox.fingerprints import generate_fingerprint

    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("navigator"), dict) or not isinstance(value.get("screen"), dict):
                raise ValueError()
            return value
        fingerprint = generate_fingerprint(os="macos" if sys.platform == "darwin" else "linux")
        temporary = path.with_suffix(".tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(fingerprint, stream)
        os.replace(temporary, path)
        return fingerprint
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PublishError("FINGERPRINT_INVALID", "Persistent device fingerprint is unreadable; restore its backup instead of silently rotating it.", 3) from exc


@asynccontextmanager
async def browser_session(profile: Path, headless=False):
    from importlib.metadata import version
    from camoufox.async_api import AsyncCamoufox
    from camoufox.multiversion import find_installed_version
    from camoufox.pkgman import launch_path

    if any(version(package) != required for package, required in RUNTIME.items()):
        raise PublishError("RUNTIME_MISMATCH", "Install the pinned Camoufox/Playwright pair with uv sync --locked.", 3)
    override = os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip()
    if override:
        executable = Path(override).expanduser().absolute()
        matching = False
        for parent in list(executable.parents)[:4]:
            metadata = parent / "version.json"
            if metadata.is_file():
                try:
                    build = json.loads(metadata.read_text())
                    matching = build["version"] == "156.0.1" and build["build"] == "beta.34"
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                break
        if not executable.is_file() or not matching:
            raise PublishError("BROWSER_MISMATCH", "Explicit Camoufox executable needs matching 156.0.1-beta.34 version.json metadata.", 3)
    else:
        installed = find_installed_version(BROWSER_BUILD)
        if not installed:
            raise PublishError("BROWSER_MISSING", "Install paired official/156.0.1-beta.34 with python -m camoufox fetch before publishing.", 3)
        executable = Path(launch_path(installed))
    fingerprint = stable_fingerprint(profile.parent / "fingerprint.json")
    mode = "virtual" if headless and sys.platform.startswith("linux") else headless
    try:
        async with AsyncCamoufox(persistent_context=True, user_data_dir=str(profile), executable_path=str(executable), fingerprint=fingerprint,
                                headless=mode, humanize=1.0, locale="en-US", block_images=False) as context:
            context.set_default_timeout(30000)
            yield context
    finally:
        for directory, _, files in os.walk(profile):
            Path(directory).chmod(0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o600)


async def one(locator, description):
    count = await locator.count()
    if count != 1:
        raise PublishError("UI_CHANGED", f"Cannot uniquely locate {description}; found {count} controls.", 3)
    return locator


def reel_url(value: str) -> str:
    url = urlsplit(urljoin("https://www.facebook.com/", value))
    if url.scheme != "https" or url.netloc.lower() not in {"facebook.com", "www.facebook.com"} or not re.fullmatch(r"/reel/\d+/?", url.path):
        raise PublishError("UNCONFIRMED_POST", "Success surface did not expose a concrete Facebook Reel URL.", 4)
    return "https://www.facebook.com" + url.path.rstrip("/")


class FacebookReels:
    def __init__(self, context, contract: dict, diagnostics: Path):
        self.context, self.contract = context, contract
        self.diagnostics = private_dir(diagnostics)
        self.page = None
        self.submitted = False
        self.post_url = None
        self.video_name = None
        self.prepared_request = None

    def selector(self, name: str, scope=None):
        return (scope if scope is not None else self.page).locator(self.contract["selectors"][name])

    async def control(self, name: str, scope=None, visible=True):
        locator = self.selector(name, scope)
        if visible:
            await locator.wait_for(state="visible")
        return await one(locator, name)

    async def pre_submit_click(self, locator, description):
        """Compare resolved elements and click that same handle, never a new match."""
        locator = await one(locator, description)
        target = await locator.element_handle()
        post = self.selector("post_button")
        post_count = await post.count()
        final = None
        try:
            if target is None or post_count != 1:
                raise PublishError("UI_CHANGED", "Cannot verify a unique final Post control before a preparation click.", 3)
            final = await post.element_handle()
            if final is None or await target.evaluate(
                "(target, post) => !target.isConnected || !post.isConnected || target === post || post.contains(target) || target.contains(post)", final
            ):
                raise PublishError("UNSAFE_PRE_SUBMIT_CONTROL", f"{description} resolves to the final Post element or its click area.", 3)
            # An ElementHandle prevents a later locator match from swapping in Post.
            await target.click()
        finally:
            for handle in (target, final):
                if handle is not None:
                    try:
                        await handle.dispose()
                    except Exception:
                        pass

    async def actor(self, name: str, expected: dict, scope=None):
        link = await self.control(name, scope)
        href = await link.get_attribute("href")
        try:
            actual = profile_url(urljoin(self.page.url, href or ""))
        except PublishError as exc:
            raise PublishError("LOGIN_REQUIRED", "Cannot verify the active Page URL; use visible login.") from exc
        if actual != expected["page_url"] or (await link.inner_text()) != expected["display_name"]:
            raise PublishError("ACCOUNT_MISMATCH", "Active actor differs from the bound Facebook Page.")

    async def verify_identity(self, expected: dict):
        authenticated_url(self.page.url)
        menu = self.contract["selectors"]["actor_menu_button"]
        if menu:
            await self.pre_submit_click(await self.control("actor_menu_button"), "actor menu")
        try:
            await self.actor("current_actor_link", expected)
        finally:
            if menu:
                await self.pre_submit_click(await self.control("actor_menu_button"), "actor menu")
        await self.control("page_management_marker")

    async def identity_page(self, expected: dict):
        self.page = await self.context.new_page()
        await self.page.goto(expected["page_url"], wait_until="domcontentloaded")
        await self.verify_identity(expected)

    async def open_editor(self, expected: dict):
        await self.identity_page(expected)
        await self.page.goto(self.contract["editor_url"], wait_until="domcontentloaded")
        authenticated_url(self.page.url)
        await self.control("editor")

    async def existing_urls(self) -> set[str]:
        urls = set()
        for href in await self.page.locator("a[href]").evaluate_all("links => links.map(link => link.href)"):
            try:
                urls.add(reel_url(href))
            except PublishError:
                pass
        return urls

    async def steps(self, name: str):
        for selector in self.contract[name]:
            editor = await self.control("editor")
            control = editor.locator(selector)
            await control.wait_for(state="visible")
            await self.pre_submit_click(control, "configured editor step")

    async def prepare(self, video: Path, caption: str, privacy: str):
        if privacy != "public":
            raise PublishError("INVALID_PRIVACY", "Only explicitly requested public Page Reels are supported.")
        editor = await self.control("editor")
        upload = await self.control("upload", editor, visible=False)
        await upload.set_input_files(str(video))
        self.video_name = video.name
        files = await upload.evaluate("input => Array.from(input.files || [], file => file.name)")
        if files != [video.name]:
            raise PublishError("UPLOAD_UNCONFIRMED", "Editor did not accept the private video copy.", 3)
        await self.control("upload_ready", editor)
        await self.steps("steps_before_caption")
        editor = await self.control("editor")
        await (await self.control("caption", editor)).fill(caption)
        await self.steps("steps_after_caption")
        editor = await self.control("editor")
        await self.pre_submit_click(await self.control("public_button", editor), "audience menu")
        await self.pre_submit_click(await self.control("public_option"), "Public option")

    async def caption_text(self, editor):
        caption = await self.control("caption", editor)
        return await caption.evaluate("element => ('value' in element ? element.value : element.innerText)")

    async def verify(self, expected: dict, caption: str, privacy: str):
        authenticated_url(self.page.url)
        editor = await self.control("editor")
        await self.actor("composer_actor_link", expected, editor)
        if await self.caption_text(editor) != caption:
            raise PublishError("CAPTION_MISMATCH", "Editor caption differs from the exact supplied text.", 3)
        if privacy != "public" or (await (await self.control("public_selected", editor)).inner_text()) != "Public":
            raise PublishError("PRIVACY_MISMATCH", "Public audience is not selected immediately before Post.", 3)
        await self.control("upload_ready", editor)
        # Trial checks actionability without sending anything.
        await (await self.control("post_button", editor)).click(trial=True)
        self.prepared_request = (expected, caption, privacy)

    async def submit(self):
        if self.submitted:
            raise PublishError("DUPLICATE_SUBMIT", "This browser session has already reached Post.", 4)
        self.submitted = True
        if self.prepared_request is None:
            raise PublishError("UNVERIFIED_SUBMIT", "Post requires a verified actor, caption, upload and Public audience.", 4)
        await self.verify(*self.prepared_request)
        editor = await self.control("editor")
        # Exactly one call. No retry/fallback/force/DOM dispatch after the journal boundary.
        await (await self.control("post_button", editor)).click(no_wait_after=True)

    async def verify_post(self, url: str, expected: dict, caption: str):
        await self.page.goto(reel_url(url), wait_until="domcontentloaded")
        if reel_url(self.page.url) != url:
            raise PublishError("UNCONFIRMED_POST", "Result did not open the specific submitted Reel.", 4)
        await self.actor("post_actor_link", expected)
        if (await (await self.control("post_caption")).inner_text()) != caption or (await (await self.control("post_public")).inner_text()) != "Public":
            raise PublishError("UNCONFIRMED_POST", "Reel author, exact caption and Public audience are not confirmed.", 4)

    async def confirm(self, before: set[str], expected: dict, caption: str) -> str:
        success = await self.control("success")
        link = await self.control("result_link", success)
        url = reel_url(await link.get_attribute("href") or "")
        if url in before:
            raise PublishError("UNCONFIRMED_POST", "Success link refers to a Reel present before submission.", 4)
        await self.verify_post(url, expected, caption)
        self.post_url = url
        return url

    async def screenshot(self, name="last-failure.png"):
        if self.page is None or self.page.is_closed():
            return None
        path = self.diagnostics / name
        try:
            await self.page.screenshot(path=str(path))
            path.chmod(0o600)
            return path
        except Exception:
            return None
