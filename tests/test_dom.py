"""Optional real DOM checks; every browser request is fulfilled locally."""

from argparse import Namespace
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import pytest

from facebook_automation.browser import SELECTORS, browser_session
from facebook_automation.errors import PublishError
from facebook_automation.service import accept_ui
from facebook_automation.state import State

BINARY = os.environ.get("FACEBOOK_TEST_CAMOUFOX_EXECUTABLE_PATH")
PAGE = "https://www.facebook.com/caribbeanawesome"
ALIAS = "button[data-action='submit-reel']"


@pytest.mark.skipif(not BINARY, reason="Set FACEBOOK_TEST_CAMOUFOX_EXECUTABLE_PATH for isolated DOM checks")
@pytest.mark.parametrize("control", ["steps_before_caption", "steps_after_caption", "public_button",
                                    "public_option", "actor_menu_button", "nested-post-label",
                                    "missing-post-control", "ambiguous-post-control"])
async def test_alternate_selector_cannot_post_during_accept_ui(tmp_path, monkeypatch, control):
    import facebook_automation.service as service
    monkeypatch.setattr(service, "probe_video", lambda _: None)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", BINARY)
    selectors = {key: "#" + key for key in SELECTORS}
    selectors["actor_menu_button"] = ""
    contract = {"schema_version": "1.0.0", "target": {"kind": "page", "page_url": PAGE, "display_name": "Caribbean Awesome"},
                "editor_url": "https://www.facebook.com/reels/create", "selectors": selectors,
                "steps_before_caption": [], "steps_after_caption": []}
    if control in {"steps_before_caption", "steps_after_caption"}:
        contract[control] = [ALIAS]
    elif control == "nested-post-label":
        contract["steps_before_caption"] = [".post-label"]
    elif control in {"missing-post-control", "ambiguous-post-control"}:
        selectors["post_button"] = "#missing-post" if control == "missing-post-control" else "button"
        contract["steps_before_caption"] = ["#public_button"]
    else:
        selectors[control] = ALIAS
    contract_file, video = tmp_path / "contract.json", tmp_path / "source.mp4"
    contract_file.write_text(json.dumps(contract))
    video.write_bytes(b"controlled file-input bytes")
    state = State(tmp_path / "state")
    state.bind("brand", PAGE, "Caribbean Awesome")
    args = Namespace(account="brand", video=str(video), caption_file=None, metadata_file=None,
                     privacy="public", timeout_seconds=30, ui_contract=str(contract_file))
    html = """
        <a id=current_actor_link href='/caribbeanawesome'>Caribbean Awesome</a>
        <button id=page_management_marker>Manage Page</button>
        <section id=editor>
        <a id=composer_actor_link href='/caribbeanawesome'>Caribbean Awesome</a>
        <input id=upload type=file><div id=upload_ready>Ready</div><textarea id=caption></textarea>
        <button id=public_button data-clicks=0 onclick="this.dataset.clicks=String(Number(this.dataset.clicks)+1);document.getElementById('public_option').hidden=false">Audience</button>
        <span id=public_selected>Public</span>
        <button id=post_button data-action=submit-reel data-clicks=0 onclick='this.dataset.clicks=String(Number(this.dataset.clicks)+1)'><span class=post-label>Post</span></button>
        </section><button id=public_option hidden>Public</button>"""
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    async with browser_session(profile, True) as context:
        async def local_page(route):
            await route.fulfill(status=200, content_type="text/html", body=html)
        await context.route("**/*", local_page)
        @asynccontextmanager
        async def controlled_session(*_):
            yield context
        with pytest.raises(PublishError) as error:
            await accept_ui(state, args, controlled_session)
        expected_code = "UI_CHANGED" if control in {"missing-post-control", "ambiguous-post-control"} else "UNSAFE_PRE_SUBMIT_CONTROL"
        assert error.value.code == expected_code
        assert await context.pages[-1].locator("#post_button").get_attribute("data-clicks") == "0"
        if control in {"missing-post-control", "ambiguous-post-control"}:
            assert await context.pages[-1].locator("#public_button").get_attribute("data-clicks") == "0"
        assert state.account("brand")["ui_contract"] is None
