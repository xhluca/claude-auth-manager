# Claude Auth Manager

Use several Claude subscriptions and several API keys from one Claude Code
installation. Every saved account or key gets its own exact route in Claude Code's
`/model` picker, so two keys that expose the same upstream model remain separate
choices.

Supported credentials:

- Claude.ai subscriptions, including multiple isolated OAuth logins
- OpenRouter API keys
- Google Gemini API keys (direct, through Google's OpenAI-compatible endpoint)
- Anthropic API keys

All configured credentials can be active at the same time. One request uses one
route, but foreground sessions, subagents, and background agents can concurrently
use different routes and keys.

## Install

Requirements: Python 3.10+ or `uv`. Claude Code is required to use CAM; the
installer installs it from its official installer if Claude is missing.

```sh
curl -fsSL https://raw.githubusercontent.com/xhluca/claude-auth-manager/main/install.sh | sh
```

The installer uses `uv tool install` when available. Otherwise it creates a private
Python environment under `~/.local/share/claude-auth-manager/tool` and links `cam`
and `claude-auth-manager` into `~/.local/bin`. It never uses sudo or modifies the
system Python. PyPI is tried first; a checksum-pinned GitHub release wheel is the
fallback if PyPI is unavailable. No GitHub login is required.

The final setup step registers the currently logged-in Claude account. For agents,
CI, or updating an existing setup without touching accounts or model choices:

```sh
curl -fsSL https://raw.githubusercontent.com/xhluca/claude-auth-manager/main/install.sh \
  | sh -s -- --install-only
```

Add `--skip-claude-install` to fail instead of installing a missing Claude Code.
To inspect the script first, download it with `curl -fsSLO` and run
`sh install.sh --install-only` after reviewing it.

Prefer `uv` directly?

```sh
uv tool install claude-auth-manager
cam account add --current
```

For a pinned installation: `uv tool install 'claude-auth-manager==0.0.3'`.
Ensure `~/.local/bin` is on your PATH (`uv tool update-shell` can help for uv installs).

### Update, reset, and uninstall

```sh
cam update
cam reset
cam uninstall
```

`cam update` installs the latest stable PyPI version using the package manager
that owns the running CAM installation (uv, pipx, or its private Python environment).
It keeps accounts, keys, and selected models. Development checkouts are protected
against replacement by an older registry version.

`cam reset` stops the router, restores backed-up Claude settings, and deletes
CAM's saved accounts, keys, catalogs, and state; the CLI stays installed.
`cam uninstall` performs that reset and removes the owning uv/pipx/private-environment
installation. It does not uninstall Claude Code or guess at removing a development
or shared-system Python install. Export anything you need before reset/uninstall.

Installer overrides: `CLAUDE_AUTH_MANAGER_PYPI_INDEX_URL` selects a package index;
`CLAUDE_AUTH_MANAGER_INSTALL_BASE_URL` selects the fallback wheel directory
(the pinned checksum still applies); `CLAUDE_AUTH_MANAGER_TOOL_DIR` selects the
private environment; `XDG_BIN_HOME` selects its command-link directory. Standard
`UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` and XDG data/config locations are respected.

## Add accounts and keys

Register the Claude account currently used by `claude`:

```sh
cam account add --current
```

Sign in to another Claude account without replacing the native login:

```sh
cam account add
```

Claude subscription names default to the authenticated email; no separate nickname
is required. `cam account add` runs Claude Code's official login flow with an
isolated `CLAUDE_CONFIG_DIR`, where Claude asks which email to use. Add `--current`
to register the already-active native login instead, or `--name NAME` to assign an
optional nickname. OAuth refresh remains delegated to the installed Claude Code
CLI.

Signing in to an already-saved account **updates that account**, keeping its route
ID, nickname, and selected models. You do not need to invent a new name. If the
account was registered with `--current`, the new login becomes an isolated managed
profile; the native Claude login is left untouched. Login is staged privately, so
cancelling or signing into a different account cannot overwrite the saved login.

CAM opens Claude's **hosted code login**, not the automatic login link that
redirects the browser to `localhost`. Sign in and paste the complete code from
Claude's page into CAM. This is the default on every machine, including SSH,
containers, and WSL; it does not depend on detecting where your browser runs.
If the browser does not open, open the sign-in link printed in the terminal.
If no code is displayed, paste the **entire hosted callback URL** from the address
bar instead; CAM accepts that URL directly too.

Input is masked with one `*` per character and checked against the current login.
No port forwarding, second terminal, or extra command is needed. As a fallback,
the prompt also accepts the entire URL of a failed `localhost` callback from an
older login flow. Do not share callback URLs or login codes.

You can also add a long-lived token created with `claude setup-token`. Paste it
at the masked prompt; no nickname is required:

```sh
claude setup-token
cam account add --token
```

For secret pipes, use `cam account add --token-stdin`. The manager derives a stable,
non-secret identifier from unnamed tokens. `--name NAME` is available when a human
nickname would be useful. Token values are deliberately never accepted as command-line
arguments, where shell history and process listings could expose them.

Add as many keys as needed. Secret input is masked by default. `--key-path PATH` is
the preferred noninteractive form for agents and secret mounts; `--key-stdin` works
for pipes. Inline `--key KEY` is also supported when convenience outweighs the risk
of shell-history and process-list exposure.

```sh
cam key add personal --provider openrouter
cam key add personal --provider openrouter --key 'YOUR_OPENROUTER_KEY'
cam key add personal --provider openrouter --key-path /run/secrets/openrouter
cam key add lab --provider openrouter
cam key add google-work --provider google
cam key add google-personal --provider google
cam key add anthropic-team --provider anthropic-api
```

List credentials together, or select one metadata view. These commands print only
metadata, never secret values:

```sh
cam list
cam list --account
cam list --key
cam list --model --account primary@example.com sonnet --json
cam list --model --key personal qwen --json
cam list --route
cam list --config
cam check
```

## Select routes

`cam select` refreshes every OpenRouter key through OpenRouter's authenticated,
guardrail-filtered model endpoint before resolving or displaying routes. This
also updates models added or removed since the key was first indexed. A failed
refresh aborts selection instead of trusting stale access. Other providers keep
their explicit `cam index` refresh behavior. Then select any mixture of accounts
and keys:

```sh
cam index
cam search gemini --tools
cam search qwen --key personal

# Start on one subscription and omit other subscriptions from this edit:
cam select --account primary@example.com

# Account filtering also disambiguates short model IDs in scripted selection:
cam select --account primary@example.com claude-sonnet-5

cam select \
  'anthropic/primary@example.com/claude-sonnet-5' \
  'anthropic/secondary@example.com/claude-sonnet-5' \
  openrouter/personal/qwen/qwen3-coder \
  openrouter/lab/qwen/qwen3-coder \
  google/google-work/gemini-3.8-flash
```

The interactive picker has one Source dropdown for everything: All, each Claude
subscription, and each OpenRouter, Google, or direct Anthropic API key. Press
`Tab`, type to search accounts or key nicknames, then press `Enter` to filter the
model list. Search and Source are shown on separate lines, and selections remain
selected while their source is hidden.

`--account ACCOUNT` is optional and always takes a value. It limits which Claude
subscriptions participate in the edit and starts on that subscription when only
one is supplied; API-key sources remain available in the same dropdown. Favorites
from other Claude subscriptions are preserved, while visible API-key favorites
remain editable. Account IDs and labels are both accepted, and `--account` can be
repeated. Routes no longer exposed by refreshed provider catalogs are removed.

Then run normal Claude Code and use `/model`:

```sh
claude
```

There is no single global active profile. Every selected route remains active.
A Claude process starts with its saved default (or `--model`), and `/model`
hot-switches the live conversation beginning with its next request while also
saving that choice as the default for new sessions. CAM routes every request from
its current model field; it does not pin a session to its launch model. Earlier
turns remain in the conversation and were still produced by their earlier models.
To choose before launch, pass the exact managed route to Claude directly:

```sh
claude --model cam/openrouter/personal/qwen/qwen3-coder
claude --model 'cam/anthropic/primary@example.com/claude-sonnet-5[1m]'
```

Starting a second Claude process with another route does not switch or stop the
first one. `cam select` changes which routes are allowed and restarts the local
router, so avoid changing the selection during an in-flight request: retained
routes reconnect and continue, while a removed route is rejected on its next
request instead of falling back to another credential. Existing Claude processes
are not killed. Reopen `/model` to refresh its picker; reopen `/agents` when you
want Claude Code to refresh generated subagent definitions.

Both `cam select` and Claude's `/model` picker keep labels compact: each row starts
with only the model name, such as `Gemini 3.8 Flash`, `Sonnet 5`, or `Opus 4.8`.
For Sonnet and Opus routes configured for 1M, the name includes `(1M context)`
to distinguish the extended window, matching Claude Code's picker. Fable keeps
its plain name because its 1M window is standard. Picker selections and generated
presets include Claude's `[1m]` annotation where needed; the router still receives
the same account/key route and upstream model ID. Fable also opts into 1M
internally for gateway sessions, without adding the suffix to its visible name. See
[Claude Code's context configuration](https://code.claude.com/docs/en/model-config#extended-context).
Every custom description uses one compact provenance convention:

```text
Claude · Max 20x (account@example.com) via claude-auth-manager
Google · Gemini API (google-work) via claude-auth-manager
Z.AI · OpenRouter (personal) via claude-auth-manager · 0.075 in / 0.25 out / 0.015 cached ($/M) · z-ai/glm-5.3-flash
```

The first field is the model maker, followed by the credential type and its email
or nickname. The subscription multiplier comes from saved login metadata; when it
is unavailable, the description does not guess it. Other reported plans, such as
`Plus`, are shown as-is, and token-only accounts with no plan metadata show
`Subscription`. Pricing is included only when the catalog publishes input and
output rates; `—` marks an unpublished cached-input rate. The exact model slug is
appended only for OpenRouter. Prices are rounded to at most three decimals for
input/output and four for cached input, with redundant trailing zeroes removed.
Tools and context stay out of descriptions. Full
credential IDs and `cam/...` routes stay out of the interactive selector and its
save confirmation. The confirmation uses the same short model name and provenance
as the picker.

Picker IDs are fail-closed and credential-specific:

```text
cam/anthropic/primary@example.com/claude-sonnet-5
cam/openrouter/personal/qwen/qwen3-coder
cam/openrouter/lab/qwen/qwen3-coder
cam/google/google-work/gemini-3.8-flash
```

If only one credential exposes a model, `cam select qwen/qwen3-coder` is accepted.
When several keys expose it, `cam` reports the ambiguity and asks for an exact
provider/key/model route.

Model-specific subagent definitions are generated alongside the picker entries.
This allows a parent on one credential to invoke a named subagent pinned to a
different credential. The pre-tool hook prevents Claude Code from silently
replacing that exact subagent model with `sonnet`.

## Commands

### Automatic fallback

Each selected model route can have one fallback to another selected route, even
on a different account, key, or provider. In `cam select`, highlight a selected
model and press `Ctrl-F`. Type to search the other selected models, then press
Enter to apply a fallback or choose **No fallback**. The edit is saved with `s`;
cancelling the picker discards it. Models hidden by an account filter remain
available as fallback destinations. Self-links and cycles are excluded.

For example, link an Opus subscription to another account's Sonnet, then link
that Sonnet to an OpenRouter model. You configure each next step separately:

```sh
cam select --fallback \
  'cam/anthropic/primary@example.com/claude-opus-5' \
  'cam/anthropic/backup@example.com/claude-sonnet-5'
cam select --fallback \
  'cam/anthropic/backup@example.com/claude-sonnet-5' \
  'cam/openrouter/personal/z-ai/glm-5.3-flash'
cam list --config --json
cam select --clear-fallback 'cam/anthropic/primary@example.com/claude-opus-5'
```

Use `cam list --route --json` to discover your exact selected routes. Link-only
edits take effect on the running router's next request; no service or Claude
restart is needed. Removing a favorite removes its incoming and outgoing links.
Fallback is opt-in: CAM never chooses another paid key or account by itself.

When a route returns insufficient credits (402), a usage limit (429), a temporary
server error (500/502/503/504/529), a timeout (408), or a connection failure, CAM
tries its next link with that route's credentials. It can walk through several
unavailable accounts in one request. Authentication, permission/guardrail, and
invalid-request errors are returned directly. No undeclared route is tried.

Provider `Retry-After` and rejected Anthropic limit-reset headers determine when
to try a route again. Without a reset hint, defaults are 60 seconds for outages,
5 minutes for usage limits, and 15 minutes for exhausted credits. Cooldowns are
saved across router restarts. Each request starts from its originally selected
route, skips routes still cooling down, and automatically returns to an earlier
route once it succeeds again. Other Claude sessions retain their own selection.

While fallback is in use, CAM inserts a visible **CAM fallback active** banner in
the response and updates the original `/model` entry's description with the actual
destination. The selected row remains the primary so recovery remains automatic;
the upstream response and routing notice identify the model that really answered.
Claude's settings watcher may take a few seconds to refresh the description.
`cam list --config --json` exposes configured links, current activations, failure
reasons, and retry deadlines for agents. The picker annotation is shared across
sessions using that primary route; the banner describes each actual response.

Early errors inside an HTTP-200 stream can also fall back. Once any content or
tool call has reached Claude, CAM does not replay it: it reports the interrupted
stream, marks that route unavailable, and uses the fallback on Claude's next retry
or request. This avoids duplicating tool work. A chain with no healthy destination
returns an explicit error after at most one attempt per route. Fallback does not
silently truncate a conversation to fit a smaller model's context or remove user
images; the destination must support the request. Built-in aliases such as `opus`
are not tied to a saved account link; choose the account-specific CAM picker row.

Every public command, argument, and option is listed below. Run bare `cam`,
`cam account`, or `cam key` to see the corresponding help without an error.

### General

```text
cam [-h] [--version] COMMAND ...
```

- `-h`, `--help` — show help for the current command and exit.
- `--version` — print the installed CAM version and exit.

### `cam list`

```text
cam list [QUERY...] [--model] [--account [ACCOUNT] | --key [KEY]]
         [--provider PROVIDER]... [--tools] [--offline] [--json]
cam list [--route | --config] [--check-confirmation {ask,never}] [--json]
```

- No view flag — list non-secret metadata for all saved accounts and provider keys.
- `--account` without `--model` — list only Claude subscription accounts.
- `--key` without `--model` — list only provider API keys.
- `--model` — statically list available credential-scoped model routes; refreshes each OpenRouter key's current guardrail-filtered catalog first.
- `QUERY...` — with `--model`, filter IDs, names, descriptions, providers, and source names using case-insensitive terms or glob patterns; any query may match.
- `--model --account` — list models across all Claude subscriptions; add `ACCOUNT` to select one account by ID, email, or label.
- `--model --key` — list models across all API keys; add `KEY` to select one key by ID or label.
- `--provider PROVIDER` — with `--model`, keep `anthropic`, `anthropic-api`, `google`, or `openrouter`; repeat to include several.
- `--tools` — with `--model`, keep only models advertising tool support.
- `--offline` — with `--model`, use saved catalogs without network refresh.
- `--route` — list only the currently selected `/model` favorites.
- `--config` — show the default model, router port, check-confirmation mode, and routes.
- `--check-confirmation ask|never` — with `--config`, require or disable confirmation for billable route checks.
- `--json` — emit the selected view as JSON instead of a table.

Model JSON includes an exact `route` value that can be passed directly to
`cam select`; it also retains the upstream `id`, provider, credential, and model
metadata.

### `cam account`

```text
cam account add [--current | --token | --token-stdin] [--name NAME]
cam account remove NAME
```

- `cam account add` — open Claude's hosted OAuth flow, or renew the matching saved account.
- `--current` — register the native login currently used by Claude without copying its refresh token.
- `--token` — securely prompt for a long-lived value generated by `claude setup-token`.
- `--token-stdin` — read a setup token from standard input for a pipe or agent workflow.
- `--name NAME` — assign an optional nickname; hosted and native logins otherwise use their email.
- `cam account remove NAME` — remove an account by ID or nickname; selected accounts must first be removed with `cam select`.

The three add-mode flags are mutually exclusive. Setup tokens are intentionally
not accepted as command-line values.

### `cam key`

```text
cam key add NAME --provider PROVIDER
            [--label LABEL]
            [--key KEY | --key-path PATH | --key-stdin]
            [--no-validate]
cam key remove NAME
```

- `NAME` — required unique key nickname used in credential-scoped model routes.
- `--provider`, `-p` — required provider: `openrouter`, `google`, or `anthropic-api`.
- `--label LABEL` — set a display label distinct from the route nickname.
- No secret-input flag — read the key from a masked interactive prompt.
- `--key KEY` — take the key inline; this can expose it in shell history and process listings.
- `--key-path PATH` — read the key from a file, the preferred form for agents and secret mounts.
- `--key-stdin` — read the key from standard input.
- `--no-validate` — store without validation or model indexing; use `cam index --key NAME` later.
- `cam key remove NAME` — remove a named key; selected keys must first be removed with `cam select`.

The three secret-input flags are mutually exclusive. Without `--no-validate`, CAM
validates the credential and immediately indexes the models available through it.

### `cam index`

```text
cam index [--key NAME] [--json]
```

- No `--key` — refresh every saved provider-key catalog and include subscription routes.
- `--key NAME` — refresh only the catalog belonging to that named provider key.
- `--json` — emit indexed model metadata as JSON instead of a table.

### `cam search`

```text
cam search QUERY... [--key NAME] [--tools] [--offline] [--json]
```

- `QUERY...` — search case-insensitive terms or glob patterns across model IDs, names, providers, and credential labels; any query may match.
- `--key NAME` — search only models belonging to one named provider key.
- `--tools` — keep only models that advertise tool support.
- `--offline` — search saved catalogs without refreshing them over the network.
- `--json` — emit matching model metadata as JSON instead of a table.

Without `--offline`, `cam search` refreshes the applicable catalogs before searching.

### `cam select`

```text
cam select [ROUTE...] [--account ACCOUNT]... [--port PORT]
           [--fallback FROM TO]... [--clear-fallback FROM]...
```

- No `ROUTE` — open the interactive multi-select picker.
- `ROUTE...` — run non-interactively and select exact `cam/...` routes, `provider/credential/model` specs, or unambiguous model IDs.
- `--account ACCOUNT` — include only this Claude subscription in the edit and initially filter to it when singular; API keys remain in the Source menu, and other subscription favorites are preserved; repeatable.
- `--port PORT` — save and use a local router port from 1–65535; otherwise reuse the configured port or `9427`.
- `--fallback FROM TO` — set one next-hop link between selected routes; repeatable; alone edits links without opening the picker or refreshing catalogs.
- `--clear-fallback FROM` — remove a selected route's fallback link; repeatable; applies before `--fallback` edits.

Interactive picker keys:

- Typing — filter model IDs, names, and descriptions within the active source.
- `Tab`, `Shift-Tab` — open the searchable Source dropdown containing All, subscriptions, and API keys.
- In the Source dropdown, type to filter account labels, emails, key nicknames, or providers; use `↑`/`↓` and `Enter` to apply, or `Esc` to return.
- `↑`, `↓` or `k`, `j` — move through results; moving above the first result returns to search.
- `Enter`, `Space` — toggle the highlighted model; `Enter` from search enters result browsing.
- `Ctrl-F` — while browsing a selected model, open its searchable fallback menu; Enter applies and Esc returns; save the main picker to commit.
- `Esc` — return to search; `/` clears the search and returns to it from result browsing.
- `s` or `S` — save while browsing; in search, use `Ctrl-S` or `Shift-S` because lowercase `s` is searchable text.
- `q` or `Ctrl-C` — cancel without changing favorites.

The dependency-free fallback picker uses result numbers to toggle, `f` to search
accounts and keys, `b NUMBER` to edit that selected model's fallback, `/` to search
models again, `s` to save, and `q` to cancel.

### `cam check`

```text
cam check [ROUTE] [-y | --yes] [--json]
cam check --all [--json]
cam check --account [ACCOUNT] [--json]
cam check --key [KEY] [--json]
```

- No `ROUTE` — perform a non-billable local check of configuration, credentials, and router health.
- `ROUTE` — send a real, potentially billable request and verify a complete `Glob` tool round-trip.
- `--all` — check every saved account and key, including those with no selected models, using non-billable provider endpoints.
- `--account [ACCOUNT]` — check all subscriptions, or one exact account ID/display label.
- `--key [KEY]` — check all API keys, or one exact key ID/display label.
- `-y`, `--yes` — send the live route probe without its confirmation prompt.
- `--json` — emit health or live-probe details as JSON.

Use `cam list --config --check-confirmation ask|never` to change the default
confirmation behavior for future live checks.

The credential checks do not send prompts or switch accounts/routes. Claude
subscriptions show available 5-hour/7-day usage windows (and reset timestamps in
JSON); expired OAuth tokens may refresh through Claude Code. OpenRouter shows the
key's remaining spending budget, not the underlying account's credit balance.
Google and Anthropic API keys are checked against their model-list endpoints;
their remaining quota is reported as unknown. A valid metadata response does not
guarantee that an inference request, model, or tool is available.

The Claude usage endpoint is the same endpoint used by Claude Code, but is not a
stable public API and may deny setup tokens with insufficient scope. Failures are
reported separately per credential, without raw provider messages or tokens.
Exit status is 1 if any credential check fails or reports a reached limit, and 0
otherwise (including an empty list). These checks don't change fallback cooldowns.

### Service and lifecycle

```text
cam serve [--host HOST] [--port PORT]
cam reset
cam uninstall
cam update
```

- `cam serve` — run the credential router in the foreground instead of through CAM's user service.
- `--host HOST` — bind `cam serve` to `127.0.0.1`, `localhost`, or `::1`; non-loopback hosts are rejected.
- `--port PORT` — make `cam serve` listen on a port from 1–65535; the default is `9427`.
- `cam reset` — stop the router, restore pre-CAM Claude settings, and delete CAM accounts, keys, catalogs, and state while preserving native Claude credentials.
- `cam uninstall` — run `cam reset`, then remove CAM when the current uv, pipx, or fallback installation can be identified safely.
- `cam update` — update CAM in place using the package manager that owns the current installation, then restart configured routing.

## Security and behavior

- The router binds only to `127.0.0.1` (default port `9427`) and requires a random
  local header token.
- Provider credentials, managed OAuth profiles, the registry, and backups are
  stored with owner-only permissions under the XDG config/state directories.
- Registering the current Claude account records a reference to its native
  credential file; it does not copy its refresh token.
- Every managed model is checked against the selected route allowlist. Incoming
  `Authorization` and `X-Api-Key` values are stripped before the exact route's
  credential is injected.
- Provider keys never appear in Claude settings, model labels, generated agents, or
  diagnostics. They appear in command-line arguments only when you explicitly use
  `--key KEY`; the masked prompt, `--key-path`, and `--key-stdin` avoid that exposure.
- `cam reset` stops the service, restores the pre-manager Claude settings, and
  removes manager-owned accounts, keys, catalogs, and state. A referenced native
  Claude credential is never deleted.

Claude Code connectors and account-scoped UI features still belong to its native
login. Changing the inference route does not impersonate a second account for
those features.

## Development and testing

```sh
uv run --with pytest pytest -q
uv run --with ruff ruff check .
```

The local integration suite uses fake upstreams to verify credential isolation,
streaming, tool translation, duplicate models across multiple keys, and concurrent
routing. The optional Docker check uses real Claude, OpenRouter, and Gemini
credentials from read-only secret mounts:

```sh
scripts/live-docker-check.sh \
  /path/to/openrouter-key \
  /path/to/gemini-key \
  ~/.claude
```

The live test copies the native Claude OAuth file into a temporary container home
and removes that home when it exits.

Fallback tests use a separate fault-injecting HTTP upstream, never a hidden
production debug endpoint. `tests/test_fallback.py` exercises five synthetic
accounts/keys, credits and quota exhaustion, outages, recovery, streaming errors,
tool indexes, concurrent requests, persistent cooldowns, and cyclic/removed links.
`scripts/test-fallback-tui.py` tests the real Claude TUI in Docker with only
synthetic credentials. `docker/Dockerfile.fallback` extends the existing UI test
image with pytest, pexpect, and pyte; mount this checkout at `/src` and the Claude
binary at `/usr/local/bin/claude`, then run the script with network disabled.
Pass `--late` to test Claude's automatic retry after a partial streamed response.

`uv run python scripts/test-fallback-live.py` is billable: it uses each selected
credential as a real destination for a Claude Code Glob tool round-trip, with
synthetic exhausted predecessors in a separate loopback router. It prints only
redacted test results and leaves production routing choices unchanged. An expired
credential is reported as blocked rather than counted as a successful live test.

`uv run python scripts/test-install-lifecycle.py` checks the published curl
installer, direct PyPI installation, checksum-verified GitHub fallback, update,
and uninstall in disposable homes and tool stores. It uses no real Claude login
and does not modify the caller's installed CAM or settings.

### Publishing a release

Update the version in `pyproject.toml`, `__init__.py`, `uv.lock`, and `install.sh`;
run the tests, then `bash scripts/build-release.sh`. Copy the wheel's SHA-256 into
`install.sh` and rebuild so the source archive includes the correct pin. Commit
and tag the release, upload the wheel, source archive, and `SHA256SUMS` to GitHub,
and publish the same package files with
`uv run --with twine twine upload dist/*.whl dist/*.tar.gz`.
Supply publishing credentials through your local PyPI configuration or a secure
credential store, never repository files. Re-run the public lifecycle smoke test
after publication. Published package versions must not be replaced.

## License

MIT
