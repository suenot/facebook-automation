import json
from pathlib import Path

import pytest

from facebook_automation.browser import SELECTORS, accepted_contract, reel_url, validate_contract
from facebook_automation.errors import PublishError

PAGE = {"page_url": "https://www.facebook.com/caribbeanawesome", "display_name": "Caribbean Awesome"}


def test_shipped_live_selectors_are_not_accepted():
    template = Path(__file__).parents[1] / "ui-contract.example.json"
    with pytest.raises(PublishError) as error:
        validate_contract(json.loads(template.read_text()), PAGE)
    assert error.value.code == "UI_NOT_VERIFIED"


def test_contract_rejects_personal_profile_and_wrong_actor():
    template = json.loads((Path(__file__).parents[1] / "ui-contract.example.json").read_text())
    template["target"]["kind"] = "profile"
    with pytest.raises(PublishError) as error:
        validate_contract(template, PAGE)
    assert error.value.code == "ACCOUNT_MISMATCH"


@pytest.mark.parametrize("value", ["https://evil.example/reel/123", "https://www.facebook.com/caribbeanawesome", "https://www.facebook.com/watch?v=123", "/reel/not-a-number", "http://www.facebook.com/reel/123"])
def test_result_requires_concrete_facebook_reel(value):
    with pytest.raises(PublishError):
        reel_url(value)


def test_reel_tracking_query_is_canonicalized():
    assert reel_url("/reel/123/?tracking=1") == "https://www.facebook.com/reel/123"


def test_acceptance_does_not_silently_survive_adapter_change():
    with pytest.raises(PublishError) as error:
        accepted_contract({**PAGE, "ui_contract": json.dumps({"adapter_revision": "old", "runtime": {}, "evidence_sha256": "x"})})
    assert error.value.code == "UI_NOT_VERIFIED"


async def test_submission_has_no_second_click():
    from facebook_automation.browser import FacebookReels
    class Click:
        calls = 0
        async def click(self, **kwargs):
            self.calls += 1
            raise TimeoutError()
    button = Click()
    adapter = object.__new__(FacebookReels)
    adapter.submitted = False
    adapter.prepared_request = ({}, "", "public")
    async def control(name, scope=None):
        return button
    adapter.control = control
    async def verified(*_):
        return None
    adapter.verify = verified
    with pytest.raises(TimeoutError):
        await adapter.submit()
    with pytest.raises(PublishError) as error:
        await adapter.submit()
    assert error.value.code == "DUPLICATE_SUBMIT" and button.calls == 1


async def test_persistent_launch_reuses_dict_fingerprint(tmp_path, monkeypatch):
    from facebook_automation.browser import browser_session
    import camoufox.async_api
    import camoufox.fingerprints
    executable = tmp_path / "browser" / "camoufox"
    executable.parent.mkdir()
    executable.touch()
    (executable.parent / "version.json").write_text(json.dumps({"version": "156.0.1", "build": "beta.34"}))
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(executable))
    calls = []
    generated = []
    def generate(**kwargs):
        generated.append(kwargs)
        return {"screen": {}, "navigator": {"userAgent": "Firefox"}}
    class Context:
        def set_default_timeout(self, timeout):
            assert timeout == 30000
    class Camoufox:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        async def __aenter__(self):
            return Context()
        async def __aexit__(self, *_):
            return None
    monkeypatch.setattr(camoufox.fingerprints, "generate_fingerprint", generate)
    monkeypatch.setattr(camoufox.async_api, "AsyncCamoufox", Camoufox)
    profile = tmp_path / "account" / "profile"
    profile.mkdir(parents=True)
    async with browser_session(profile, True):
        pass
    async with browser_session(profile, True):
        pass
    assert len(generated) == 1 and calls[0]["fingerprint"] == calls[1]["fingerprint"]
    assert calls[0]["persistent_context"] is True and calls[0]["user_data_dir"] == str(profile)
    assert calls[0]["humanize"] == 1.0
    assert (profile.parent / "fingerprint.json").stat().st_mode & 0o777 == 0o600


def test_expired_session_is_login_error():
    from facebook_automation.browser import authenticated_url
    with pytest.raises(PublishError) as error:
        authenticated_url("https://www.facebook.com/checkpoint/?next=page")
    assert error.value.code == "LOGIN_REQUIRED" and error.value.exit_code == 2
