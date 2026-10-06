"""Facebook adapter activated only after a visible, nonpublishing acceptance run.

The selector contract is local evidence, not a claim about the current Meta UI.
No selectors discovered from page text or guessed fallbacks can reach Post.
"""

from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urljoin, urlsplit

from .errors import PublishError
from .state import private_dir, profile_url

ADAPTER_REVISION = "facebook-personal-reels-1"
RUNTIME = {"camoufox": "0.5.7", "playwright": "1.60.0"}
BROWSER_BUILD = "official/156.0.1-beta.34"
SELECTORS = {
    "profile_owner_marker", "editor", "upload",
    "upload_ready", "caption",
    "public_selected", "post_button", "post_actor_link", "post_public",
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
    if target != {"kind": "profile", "profile_url": expected["profile_url"], "display_name": expected["display_name"]}:
        raise PublishError("ACCOUNT_MISMATCH", "UI contract target differs from the explicitly bound personal profile.")
    selectors = value["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != SELECTORS or any(
        not isinstance(selector, str) or not selector.strip() for selector in selectors.values()
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
        raise PublishError("UI_NOT_VERIFIED", "Run a visible accept-ui preflight for this personal profile before publishing.", 3)
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


async def verify_personal_login(context, expected: dict):
    """Resolve Facebook's own /me route; a public visitor cannot select its target."""
    page = await context.new_page()
    try:
        await page.goto("https://www.facebook.com/me", wait_until="domcontentloaded")
        authenticated_url(page.url)
        try:
            actual = profile_url(page.url)
        except PublishError as exc:
            raise PublishError("LOGIN_REQUIRED", "Facebook did not resolve /me to an authenticated personal profile.") from exc
        if actual != expected["profile_url"]:
            raise PublishError("ACCOUNT_MISMATCH", "Authenticated personal profile differs from the requested URL.")
    finally:
        await page.close()


def reel_url(value: str) -> str:
    url = urlsplit(urljoin("https://www.facebook.com/", value))
    if url.scheme != "https" or url.netloc.lower() not in {"facebook.com", "www.facebook.com"} or not re.fullmatch(r"/reel/\d+/?", url.path):
        raise PublishError("UNCONFIRMED_POST", "Success surface did not expose a concrete Facebook Reel URL.", 4)
    return "https://www.facebook.com" + url.path.rstrip("/")


def observed_profile_url(value: str) -> str:
    """Facebook adds tracking parameters to real author links; keep only identity."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"facebook.com", "www.facebook.com"}:
        raise PublishError("INVALID_PROFILE_URL", "Author link is not a Facebook profile.")
    if parsed.path == "/profile.php":
        values = parse_qs(parsed.query).get("id", [])
        if len(values) != 1 or not re.fullmatch(r"\d+", values[0]):
            raise PublishError("INVALID_PROFILE_URL", "Author link has no valid profile ID.")
        return profile_url("https://www.facebook.com/profile.php?id=" + values[0])
    return profile_url("https://www.facebook.com" + parsed.path)


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

    async def control(self, name: str, scope=None, visible=True, timeout=None):
        locator = self.selector(name, scope)
        if visible:
            await locator.wait_for(state="visible", timeout=timeout)
        return await one(locator, name)

    async def pre_submit_click(self, locator, description):
        """Compare resolved elements and click that same handle, never a new match."""
        locator = await one(locator, description)
        target = await locator.element_handle()
        post = self.selector("post_button")
        post_count = await post.count()
        final = None
        try:
            if target is None or post_count > 1:
                raise PublishError("UI_CHANGED", "Cannot verify the final Post control before a preparation click.", 3)
            if post_count == 0 and (description != "configured editor step" or
                                    await target.get_attribute("aria-label") != "Next" or
                                    (await target.inner_text()).strip() != "Next"):
                raise PublishError("UNSAFE_PRE_SUBMIT_CONTROL", "Only an exact Next control is allowed before Post appears.", 3)
            final = await post.element_handle() if post_count else None
            if post_count and final is None:
                raise PublishError("UI_CHANGED", "Cannot verify the final Post control before a preparation click.", 3)
            if await target.evaluate(
                """(target, post) => {
                    const overlaps = element => post && element && (element === post || post.contains(element) || element.contains(post));
                    if (!target.isConnected || (post && !post.isConnected) || overlaps(target)) return true;
                    const labels = [target.closest('label'), ...target.querySelectorAll('label')].filter(Boolean);
                    const activated = [target, target.closest('button,input'), ...target.querySelectorAll('button,input'), ...labels.map(label => label.control)].filter(Boolean);
                    return activated.some(element => overlaps(element) || (element.form && ['submit','image'].includes(element.type)));
                }""", final
            ):
                raise PublishError("UNSAFE_PRE_SUBMIT_CONTROL", f"{description} can activate Post or submit an HTML form.", 3)
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
            if not href:
                raise PublishError("LOGIN_REQUIRED", "Current profile link is missing.")
            actual = observed_profile_url(await link.evaluate("element => element.href"))
        except PublishError as exc:
            raise PublishError("LOGIN_REQUIRED", "Cannot verify the active personal profile URL; use visible login.") from exc
        if actual != expected["profile_url"] or (await link.inner_text()).strip() != expected["display_name"]:
            raise PublishError("ACCOUNT_MISMATCH", "Active actor differs from the bound personal profile.")

    async def verify_identity(self, expected: dict):
        authenticated_url(self.page.url)
        if observed_profile_url(self.page.url) != expected["profile_url"]:
            raise PublishError("ACCOUNT_MISMATCH", "The opened personal profile differs from the bound account.")
        await verify_personal_login(self.context, expected)
        await self.control("profile_owner_marker")

    async def identity_page(self, expected: dict):
        self.page = await self.context.new_page()
        await self.page.goto(expected["profile_url"], wait_until="domcontentloaded")
        await self.verify_identity(expected)

    async def open_editor(self, expected: dict):
        self.expected = expected
        await self.identity_page(expected)
        await self.page.goto(self.contract["editor_url"], wait_until="domcontentloaded")
        authenticated_url(self.page.url)
        await self.control("editor")

    async def existing_urls(self) -> set[str]:
        """Snapshot only this actor's own Reels before the submission boundary."""
        page = await self.context.new_page()
        try:
            target = self.expected["profile_url"]
            separator = "&" if urlsplit(target).query else "?"
            await page.goto(target + separator + "sk=reels_tab", wait_until="domcontentloaded")
            await page.get_by_role("tab", name="Reels", exact=True).first.wait_for(state="visible")
            return await self._reel_links(page)
        finally:
            await page.close()

    async def _reel_links(self, page) -> set[str]:
        urls = set()
        for href in await page.locator("a[href*='/reel/']").evaluate_all("links => links.map(link => link.href)"):
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
            raise PublishError("INVALID_PRIVACY", "Only explicitly requested public personal Reels are supported.")
        editor = await self.control("editor")
        upload = await self.control("upload", editor, visible=False)
        await upload.set_input_files(str(video))
        self.video_name = video.name
        # Facebook replaces the input as soon as it accepts a file. The Next
        # stage and enabled Post control below prove the selected file advanced.
        await self.steps("steps_before_caption")
        editor = await self.control("editor")
        field = await self.control("caption", editor)
        await field.focus()
        await self.page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        await self.page.keyboard.press("Backspace")
        # Playwright fill/insert_text doubles input in Facebook's Lexical editor.
        await self.page.keyboard.type(caption, delay=0)
        await self.steps("steps_after_caption")
        editor = await self.control("editor")
        await self.control("upload_ready", editor, timeout=600000)

    async def caption_text(self, editor):
        caption = await self.control("caption", editor)
        value = await caption.evaluate("element => ('value' in element ? element.value : element.innerText)")
        return "" if value in {"\n", "\r\n"} else value

    async def verify(self, expected: dict, caption: str, privacy: str):
        authenticated_url(self.page.url)
        editor = await self.control("editor")
        await verify_personal_login(self.context, expected)
        if await self.caption_text(editor) != caption:
            raise PublishError("CAPTION_MISMATCH", "Editor caption differs from the exact supplied text.", 3)
        audience = (await (await self.control("public_selected", editor)).inner_text()).splitlines()[0].strip()
        if privacy != "public" or audience != "Public":
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
        text = self.page.get_by_text(caption, exact=True)
        if await text.count() != 1:
            more = self.page.get_by_role("button", name="See more", exact=True)
            if await more.count() == 1:
                await more.click()
        if await text.count() != 1 or await (await self.control("post_public")).get_attribute("aria-label") != "Shared with Public":
            raise PublishError("UNCONFIRMED_POST", "Reel author, exact caption and Public audience are not confirmed.", 4)

    async def confirm(self, before: set[str], expected: dict, caption: str) -> str:
        """Observe the actor's own Reels tab; never infer a result from the background feed."""
        page = await self.context.new_page()
        try:
            target = expected["profile_url"]
            separator = "&" if urlsplit(target).query else "?"
            for _ in range(24):
                await page.goto(target + separator + "sk=reels_tab", wait_until="domcontentloaded")
                await page.get_by_role("tab", name="Reels", exact=True).first.wait_for(state="visible")
                candidates = (await self._reel_links(page)) - before
                if len(candidates) > 1:
                    raise PublishError("UNCONFIRMED_POST", "Multiple new personal Reels appeared after submission; inspect them manually.", 4)
                if candidates:
                    url = candidates.pop()
                    self.post_url = url
                    await self.verify_post(url, expected, caption)
                    return url
                await asyncio.sleep(5)
        finally:
            await page.close()
        raise PublishError("UNCONFIRMED_POST", "No new Reel appeared on the personal profile after submission.", 4)

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
