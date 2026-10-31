# Facebook personal Reels CLI

facebook-publish publishes one finished local video as a **personal-profile Reel** through persistent Camoufox. It never targets a Page. A calling service supplies the account alias, file, audience, request ID and run time. Facebook documents [computer uploads and per-Reel Public audience selection](https://www.facebook.com/help/www/2862139500770200).

The Key West Reel was published through this CLI on the bound personal profile with Public audience and Add AI label enabled: https://www.facebook.com/reel/1114289364465915 . A cookie-free browser opened the exact URL and played the video.

The CLI, journal and controlled browser checks are implemented. Each account's current Facebook editor selectors require a visible acceptance run. The blank ui-contract.example.json is deliberately not a usable selector set: until accept-ui succeeds, publication stops with UI_NOT_VERIFIED before Post.

## Install

Use Python 3.12, uv, ffmpeg/ffprobe, the pinned [metadata-automation](https://github.com/suenot/metadata-automation), and the paired Camoufox browser:

~~~sh
uv sync --locked --python 3.12
uv run --locked python -m camoufox fetch
uv run --locked facebook-publish --version
~~~

Dependencies are pinned to camoufox 0.5.7, playwright 1.60.0 and browser official/156.0.1-beta.34. Set CAMOUFOX_EXECUTABLE_PATH only for an executable with that build's version.json. On Linux --headless uses a virtual display; macOS uses ordinary headless mode. Verify the browser in each target environment before unattended use.

## Bind a personal profile

--profile-url must be the exact canonical personal URL returned by Facebook's authenticated /me route, either a handle or profile.php?id=... . --display-name records the expected visible name for later editor checks. The alias is local; it does not select another Facebook identity.

To reuse an authorized Firefox session, provide its profile directory to login --import-firefox. This snapshots **only Facebook cookies** from cookies.sqlite with SQLite's online backup, seeds a separate Camoufox profile and checks /me inside Camoufox before binding the alias. Cookie values remain in memory and never enter stdout, diagnostics or Git. The command skips Firefox containers/partitions, rejects unsupported cookie schemas and a Camoufox profile already in use. It does not copy the entire Firefox profile: a newer Firefox profile can be incompatible with the pinned Camoufox engine.

~~~sh
uv run --locked facebook-publish login \
  --account personal --profile-url "$PERSONAL_PROFILE_URL" \
  --display-name "$PERSONAL_PROFILE_NAME" \
  --import-firefox "$FIREFOX_PROFILE_DIR"
uv run --locked facebook-publish accounts list
~~~

The importer needs read access to Firefox's profile and a consistent SQLite snapshot. If macOS denies access or Firefox holds an exclusive lock, it returns a sanitized error; it never copies an active DB and WAL separately. Firefox session-only cookies and local storage are not included, so /me verification can still require an ordinary visible Camoufox login. For that path, omit --import-firefox and complete sign-in or 2FA in the Camoufox window before pressing Enter in the terminal.

Each alias has its own persistent profile and stable device fingerprint. The profile lock covers login, UI acceptance, publication and reconciliation. A bound alias cannot silently switch to another profile. Existing state from the old Page-only release is rejected rather than reinterpreted as a personal profile. The new default state is .local/personal/; keep that directory and its journal together when moving or backing up the installation.

## Accept the current editor

Copy ui-contract.example.json to a private file, replace the target with the exact personal URL/name, and record selectors observed in the current Facebook editor. The adapter resolves `/me` both when opening the owned profile and immediately before Post; `profile_owner_marker` must prove control of that profile. The editor's caption, Public audience and final button selectors must resolve uniquely. The example's editor URL also requires live verification.

~~~sh
mkdir -p .local/personal
chmod 700 .local/personal
cp ui-contract.example.json .local/personal/facebook-ui.json
chmod 600 .local/personal/facebook-ui.json

uv run --locked facebook-publish accept-ui \
  --account personal --video /absolute/path/video.mp4 --privacy public \
  --metadata-file /absolute/path/metadata.json \
  --ai-generated \
  --ui-contract .local/personal/facebook-ui.json
~~~

accept-ui uploads a private validated copy, checks the actor, exact caption, Public audience, requested Add AI label state and final button actionability, then records a private screenshot. It **does not click Post**. `--ai-generated` enables the observed Add AI label switch and requires its checked state again immediately before Post. Preparation selectors cannot activate the final Post button, an associated label or another HTML form submit control. Unknown UI state fails before publication. The acceptance revision changes when identity or selector rules change. The CLI verifies Public for each Reel; it does not change the account's default audience.

## Publish from another service

~~~sh
uv run --locked facebook-publish publish-reel \
  --account personal --request-id word-quiz-a-miniature-001 \
  --video /absolute/path/video.mp4 --privacy public \
  --ai-generated \
  --metadata-file /absolute/path/metadata.json --timeout-seconds 1800

uv run --locked facebook-publish status \
  --account personal --request-id word-quiz-a-miniature-001
~~~

--caption-file is an alternative to --metadata-file; an omitted source means an empty caption. A metadata file must be a complete validated metadata-automation envelope with schema 1.0.0 or 2.0.0. The publisher uses platforms.facebook.caption exactly, without appending hashtags. Metadata never chooses the account, file, privacy or publication time. Public is required explicitly; a locked personal profile or unavailable Public option stops before Post. The AI flag is part of a new request's fingerprint; omitting it preserves the argument format of earlier journal records.

Every command emits one JSON object on one stdout line; diagnostics go to stderr. For publication, exit 0 means confirmed or cached published, 2 means invalid input or authentication, 3 means failure before Post, and 4 means the result after the submission boundary is uncertain. Results contain platform, account, request_id, status, post_url and error.

SQLite stores a unique (account, request_id) with the normalized arguments and SHA-256 of the uploaded private copy. The journal commits submitting before the **only** Post click. A timeout or lost confirmation afterward is uncertain; the same ID never clicks again. Confirmation looks for one new concrete /reel/ID on the bound profile's own Reels tab, then checks its author, exact caption and Public audience. status reads the journal without media or browser. A failed preflight may retry only with the same arguments and bytes.

After manually matching an uncertain Reel's personal author, video, caption and Public audience, reconcile its exact URL:

~~~sh
uv run --locked facebook-publish reconcile \
  --account personal --request-id word-quiz-a-miniature-001 \
  --post-url https://www.facebook.com/reel/123456789 \
  --confirm-matching-reel
~~~

reconcile never opens the editor or clicks Post. Do not create a new request ID to bypass an uncertain result.

## Local data and verification

Profiles, cookie-derived session data, journal and screenshots stay in Git-ignored .local/ with private permissions. Back up the whole state directory with all CLI processes stopped. Failure screenshots can contain personal information; clean old diagnostics manually. Neither cookies nor full DOM enter normal logs.

~~~sh
uv run --locked --group dev pytest -q
~~~

Contract tests cover JSON/exit codes, cookie snapshot filtering, account binding, journal transitions, one-click safety and metadata. Optional controlled Camoufox DOM tests use intercepted local pages; set FACEBOOK_TEST_CAMOUFOX_EXECUTABLE_PATH to the paired browser to run them. The live selector contract is accepted separately on each account. This local CLI has no documented server deployment.
