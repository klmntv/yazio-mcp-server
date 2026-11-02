# yazio-mcp-server

This personal fork adds a local login and native saved Meals to
[yazio-community/yazio-mcp-server](https://github.com/yazio-community/yazio-mcp-server).
It keeps the upstream recipe support and legacy HTTP transport.

## Local setup

Requires Python 3.10+ on macOS or Linux and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run --no-sync yazio-mcp --help
```

### Sign in with Apple

Apple login uses the **official YAZIO website and Apple popup**. The helper
captures the resulting YAZIO token response; it never collects an Apple password
or saves the Apple identity token. The YAZIO account is verified before the
session replaces any existing local login.

Browser login requires BrowserOS and a Playwriter wrapper configured for direct
CDP. Supply the explicit endpoint of the browser dedicated to this task; the
helper does not discover or connect to ordinary Chrome automatically.

```sh
uv run --no-sync yazio-mcp login --method apple \
  --playwriter /absolute/path/to/playwriter-wrapper \
  --cdp-endpoint http://127.0.0.1:PORT \
  --show-browser
```

The helper creates its own background tab without tab groups. `--show-browser`
explicitly shows that tab for human approval. It closes only its own login tab
and popup, then deletes its own Playwriter session.

If local DNS cannot resolve `www.yazio.com`, `--web-address PUBLIC_IP` optionally
uses a separately resolved public address for that host only. TLS still verifies
the `www.yazio.com` certificate; system DNS and browser settings are unchanged.
The address is preserved for web-session renewal. No TLS bypass is supported.

### Email/password or session import

```sh
# Interactive terminal prompt; the password is exchanged, never saved.
uv run --no-sync yazio-mcp login

# An existing session can be imported through stdin or a private 0600 file.
uv run --no-sync yazio-mcp import-session --file /private/path/session.json

uv run --no-sync yazio-mcp status
uv run --no-sync yazio-mcp logout
```

Sessions default to `$XDG_STATE_HOME/yazio-mcp` or `~/.local/state/yazio-mcp`.
Every command accepts `--state-dir`. Tokens are stored in a local, unencrypted
`session.json` file with mode 0600 inside a 0700 directory. Passwords and tokens
are not placed in MCP configuration or command arguments. Refresh rotation is
persisted atomically and serialized across processes; a renewed session must
still belong to the same account. Mobile and website tokens renew through their
respective issuers.

### Run in Codex or another local MCP client

Use the absolute path to `.venv/bin/yazio-mcp` as the server command and `serve`
as its argument. No authentication headers or secrets are needed in the client:

```sh
/absolute/path/to/repository/.venv/bin/yazio-mcp serve
```

This exposes only reads by default. Add `--allow-writes` when food recording is
desired. `--timezone` defaults to `Europe/Moscow`; `--language` defaults to
Russian. Pass the same `--state-dir` used at login if it differs from the default.

## Saved Meals and recording guarantees

- `list_saved_meals` reads existing saved food combinations from YAZIO's
  `GET /v22/user/meals`; `get_saved_meal` resolves products and quantities.
- `track_saved_meal` records one of those combinations, optionally scaled or
  with individual items omitted or changed for this consumption. Overrides
  require the version returned by `get_saved_meal`, so a template edited in the
  app cannot silently change the meaning of an item index. The saved template
  itself is not modified.
- `track_product`, `track_recipe` and `track_saved_meal` require a stable
  `request_id` for one user action. Retrying that action reconciles the original
  plan. A new identical meal must use a new ID and is allowed.
- Exact remote entry IDs and the interpreted date are saved in SQLite **before
  POST**. Success requires rereading and matching the diary. A lost response,
  process restart or partial result never triggers a blind second POST.
- Results explicitly distinguish `committed`, `partial`, `uncertain` and
  `failed`. `get_operation` returns the recorded result for the current account.
  An unknown result can be reconciled by calling the same food tool with the
  same arguments and request ID. Missing rows alone are not proof of failure.

This is not a claim of an atomic remote meal transaction or API-level
exactly-once delivery. Automatic completion of unconfirmed partial writes is
intentionally omitted. The durable ledger covers the three food-recording tools;
recipe creation/editing and the other legacy mutations retain upstream behavior.
Native saved-Meal creation/editing is not implemented.

Apple login, saved-Meal reads, product detail, diary and daily summary have been
checked against a real account. Food mutations and token-renewal failure cases
are tested with mocks; no live food-write acceptance is claimed.

```sh
uv run --no-sync ruff check src tests
uv run --no-sync pytest -q
```

## Upstream HTTP server and reference

An MCP server over the [YAZIO](https://www.yazio.com) food diary. Ask a model
what you have eaten today, log a meal, build a recipe from tracked products.

Hand-written, unlike the SDK it sits on: it exists to turn ~48 raw endpoints
into a couple of dozen tools a model can use without a manual, which is
judgement work a generator cannot do.

> [!IMPORTANT]
> Unofficial and unaffiliated. YAZIO does not publish, endorse or support this
> server, and the API it uses is private: it can change without notice, and
> using it is subject to YAZIO's terms of service.

Built on [yazio-sdk](https://github.com/yazio-community/yazio-sdk-python), which
is generated from
[yazio-api-specification](https://github.com/yazio-community/yazio-api-specification).

## Running it

```bash
pip install yazio-mcp-server
yazio-mcp-server
```

Or from a checkout:

```bash
nix-shell --run "make run"
```

Or in a container:

```bash
docker compose up -d
```

The endpoint is `http://127.0.0.1:8931/mcp`, speaking streamable HTTP. The port
avoids 8000 on purpose — that one collides with too much other tooling for a
server whose URL is registered somewhere in advance.

## In a container

The `Dockerfile` builds an Alpine image that runs the server as an unprivileged
user (uid 10001) and holds nothing but the interpreter and the installed
virtualenv — 60 MB on disk, 21 MB to pull. It binds `0.0.0.0`, because a
container's own loopback is reachable from nothing else.

One thing the image does that a plain `pip install` does not: it removes
`cryptography` after installing. That package is 15 MB and arrives through
`mcp` → `pyjwt[crypto]`, for OAuth and JWT verification paths a Basic-auth
server never takes — PyJWT falls back to its HMAC-only algorithm set, and TLS to
YAZIO goes through the standard library's `ssl` either way. Adding an OAuth path
to this server means dropping that line from the `Dockerfile` first.

Tagged releases are published to GHCR for amd64 and arm64, so there is usually
nothing to build:

```bash
docker run --rm -p 127.0.0.1:8931:8931 ghcr.io/yazio-community/yazio-mcp-server:latest
```

`v1.4.2` publishes `1.4.2`, `1.4` and `1`; `latest` follows final releases only,
never a prerelease. From a checkout:

```bash
docker build -t yazio-mcp-server .
docker run --rm -p 127.0.0.1:8931:8931 yazio-mcp-server
```

The one thing worth knowing before deploying it: **every hostname clients use to
reach the server has to be named.** The transport's DNS-rebinding guard accepts
loopback and nothing more, so a compose service name, a Kubernetes Service or an
Ingress host all come back as `421 Invalid Host header` until they are added:

```bash
docker run --rm -p 8931:8931 yazio-mcp-server \
  yazio-mcp-server --host 0.0.0.0 --allowed-host yazio.example.com
```

`docker-compose.yml` does exactly this for the service name and publishes the
port on loopback only — the credentials in play are a YAZIO password in
reversible encoding, so anything wider belongs behind TLS.

On Kubernetes the same argument goes in `args`, and probes have to be TCP: the
whole app sits behind Basic auth, so an unauthenticated HTTP probe reports 401
for a healthy server.

```yaml
containers:
  - name: yazio-mcp
    image: yazio-mcp-server
    args: ["yazio-mcp-server", "--host", "0.0.0.0", "--allowed-host", "yazio-mcp.default.svc.cluster.local:*"]
    ports:
      - containerPort: 8931
    readinessProbe:
      tcpSocket:
        port: 8931
    securityContext:
      runAsNonRoot: true
      readOnlyRootFilesystem: true
      allowPrivilegeEscalation: false
```

Nothing is written to disk and no state survives a restart — tokens are cached
in memory, per credential pair — so replicas need no coordination and can be
scaled freely.

## Using it from Claude Code

Registered at user scope, so it is available in every directory:

```bash
claude mcp add --transport http --scope user yazio http://127.0.0.1:8931/mcp \
  --header "Authorization: Basic $(printf '%s:%s' "$YAZIO_USERNAME" "$YAZIO_PASSWORD" | base64 -w0)"
```

The server has to be running for Claude Code to reach it; start it with the
command above, and it will show as failed in `claude mcp list` when it is not.

Note that this writes the Basic credential into `~/.claude.json`, where it sits
in a form that is trivially reversible — it is your YAZIO password, not a token
you can revoke on its own. `claude mcp remove yazio --scope user` takes it back
out.

## Authenticating

There is no separate account for this server: clients authenticate with the same
username and password they use to log in to YAZIO, sent as HTTP Basic
credentials — or, if the client cannot build those, as [two plain
headers](#without-the-base64-step). The server exchanges them for a YAZIO OAuth
token on the first tool call and caches it per credential pair until it expires.

```jsonc
{
  "mcpServers": {
    "yazio": {
      "type": "http",
      "url": "http://127.0.0.1:8931/mcp",
      "headers": {
        // base64 of "username:password"
        "Authorization": "Basic bWVAZXhhbXBsZS5jb206aHVudGVyMg=="
      }
    }
  }
}
```

Basic auth is not what the MCP specification prescribes for HTTP transports — it
expects OAuth — so clients have to be pointed at the server with an explicit
header rather than through a discovery flow.

### Without the base64 step

Some clients — LibreChat among them — let an administrator define custom headers
but pass the values through verbatim, so there is no way to have the client
assemble the base64 credential from a username and a password. For those, send
the two values as separate headers instead:

```jsonc
{
  "mcpServers": {
    "yazio": {
      "type": "http",
      "url": "http://127.0.0.1:8931/mcp",
      "headers": {
        "X-Auth-Username": "me@example.com",
        "X-Auth-Password": "hunter2"
      }
    }
  }
}
```

The two forms are equivalent — the same login, the same token cache. When a
request carries both, `Authorization` wins and the `X-Auth-*` headers are
ignored. Sending only one half of the pair is refused with `400 Bad Request`,
rather than the `401` an unauthenticated request gets, so a client that is only
missing a field can tell that apart from credentials YAZIO turned down. A blank
value is not a credential either: once one of the headers is on the request,
both have to carry something or the request is a `400`.

Basic credentials are encoded, not encrypted. Bind to loopback, or terminate TLS
in front of the server; never expose it over plain HTTP on a network you do not
control.

Behind a reverse proxy, name the public hostname or the transport's DNS-rebinding
guard will reject the request:

```bash
python3 -m yazio_mcp --host 0.0.0.0 --allowed-host yazio.example.com
```

## Tools

**Overview and nutrition**

| Tool | What it answers |
| --- | --- |
| `get_daily_summary` | How is today going: intake against goal, per meal, water, activity, weight |
| `get_diary` | What was actually eaten, item by item, with the `entry_id` needed to remove one |
| `get_nutrition_range` | Energy and macros per day across a range, plus averages |
| `get_goals` | The day's energy, macro, water, step and weight goals |
| `get_water` | Water intake against goal |
| `get_activity_summary` | Steps and exercise energy across a range |

**Products**

| Tool | What it does |
| --- | --- |
| `search_products` | Search the food database by name or barcode |
| `get_product` | One product in full, including its serving options |
| `get_suggested_products` | What this user usually eats at a given meal |

**Tracking**

| Tool | What it does |
| --- | --- |
| `track_product` | Log a product, by amount or by named serving |
| `track_recipe` | Log portions of a recipe |
| `untrack_item` | Remove a logged item by its `entry_id` |
| `log_water` | Set the day's water total |
| `log_weight` | Log a weight measurement |
| `log_exercise` | Log energy burned, steps and distance |

**Recipes**

| Tool | What it does |
| --- | --- |
| `list_my_recipes` | The user's own recipes |
| `get_recipe` | One recipe: ingredients, instructions, nutrients per portion |
| `create_recipe` | Build a recipe from tracked products |
| `delete_recipe` | Remove one of the user's own recipes; logged portions stay |
| `set_recipe_photo` | Upload a photo for one of the user's own recipes |
| `delete_recipe_photo` | Remove the photo from one of the user's own recipes |
| `browse_recipes` | YAZIO's editorial catalogue for a country |
| `get_favorite_recipes` | Recipes marked as favourites |

**Profile**

`get_profile` returns the goal direction, activity level, units, height and
birth date — context worth having before interpreting anyone's numbers.

## Units

Energy is in kilocalories and macros are in **grams**. Vitamins and minerals are
reported in **milligrams**: YAZIO stores them in grams too, but at that scale they
are values like 0.00012 that round away to nothing. Every nutrient block carries a
`units` map, so the mixture cannot be misread.

Micronutrients stay small even in milligrams, because product nutrients are per
one base unit (see below) rather than per 100. Numbers that would round to zero at
two decimals are reported to three significant digits instead, so a trace amount
never reads as an absent one.

Product nutrients are stored per **one** base unit, not per 100: olive oil reads
8.84 kcal per gram. This is the factor `create_recipe` scales by, so it is worth
knowing before touching that code.

## How `create_recipe` works

YAZIO's recipe endpoint stores what the client submits and derives nothing: a
recipe carries its own nutrients. So `create_recipe` resolves every ingredient
to a real product, scales that product's nutrients to the amount the recipe
uses, and sums them before posting. An ingredient that cannot be resolved fails
the call rather than silently contributing zero.

The stored `nutrients` field holds the values **for one portion**, not for the
whole dish — undocumented, but that is what the app writes and reads back. So
the summed dish total is divided by `portion_count` before it is sent, and
`get_recipe` multiplies the stored value back up to report the total. Changing
a recipe's `portion_count` through `update_recipe` rescales the stored
per-portion values, keeping the dish itself the same size.

Two of its rules come from the API rather than from taste, and both are enforced
before the request goes out because the API signals them badly:

- **At least two ingredients.** One is rejected with "This collection should
  contain 2 elements or more".
- **`portion_count` must be a whole number.** Not merely integral in value — it
  has to serialise without a decimal point. `2` is accepted; `2.0` and `2.5` are
  both answered with a bare `500` and no message.

## API behaviour this server works around

The full catalogue of undocumented API behaviour lives in the
[spec repo](https://github.com/yazio-community/yazio-api-specification#things-the-api-does-not-document).
These are the ones that shaped code here rather than the spec:

- **The client version is checked, and it lives in the User-Agent.** Anything
  unrecognised gets `403 {"error":"version_blocked"}` on every endpoint except
  the token exchange. `config.USER_AGENT` carries the captured app's string and
  will need bumping when YAZIO retires that version — a sudden wall of 403s is
  the symptom, and it is the single most likely reason this server stops
  working one day.
- **Product search requires `sex` and `countries`.** `search_products` fills
  both from the user's profile rather than making a caller supply them.
- **`untrack_item` cannot trust the delete endpoint.** `DELETE
  /v22/user/consumed-items` takes a body of `{"<bucket>": "<uuid>"}` — a single
  string, not a list — and it also accepts an `?id=` query parameter, answers
  `204`, and does nothing. So the tool looks the entry up first to learn its
  bucket, and reads the day back afterwards rather than believing the status
  code.
- **`create_recipe` validates before sending.** Two API rules are enforced
  client-side because the API signals them badly: fewer than two ingredients is
  rejected with a message about collections, and a `portion_count` that
  serialises with a decimal point (`2.0`, `2.5`) is answered with a bare `500`
  and no message at all.

## Development

```bash
nix-shell            # builds yazio_sdk from its PyPI release
make test            # mocked suite, never touches a real account
make lint
```

`yazio_sdk` comes from PyPI everywhere — the dev shell, CI and released builds
alike. The dev shell pins one version of it (see `shell.nix`); keep that inside
the range `pyproject.toml` declares when either moves.

The suite mocks every HTTP call, so it covers the shaping logic but cannot catch
the API contradicting the spec. If a *shape* looks wrong rather than the server's
handling of it, the problem is upstream — reproduce it against the
[spec repo](https://github.com/yazio-community/yazio-api-specification) and fix
it there.

## Licence

[MIT](LICENSE).
