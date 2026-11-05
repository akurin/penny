# Penny

Private Russian-language Telegram price bot for two family members.

Penny checks five stores near home for every request: М.Косметик, Магнит,
Дикси, Пятёрочка and Лента. The branch (address, store code and verification
tokens) is deliberately **not** in the repository — it would reveal where the
family lives. A local `config/stores.local.json` (gitignored; see
`config/stores.example.json`) holds it, and the bot verifies the branch each
store returns before showing prices. Without it, the HTTP stores fail closed
rather than label another branch's prices. М.Косметик and Магнит are cookie-less
HTTP calls — the webgate accepts anonymous callers, and the branch travels in the
request body. Дикси uses session cookies from `state/dixy.json`, which the
captcha flow creates. Пятёрочка and Лента use
(ServicePipe/Qrator) and run through bounded Camoufox workers: one search at a
time, one browser process per display mode on its own playwright thread.
Пятёрочка runs in plain headless — ServicePipe's block page rejects the Xvfb
«virtual» mode (verified 2026-10-31) — while Лента needs a real WebGL context:
`headless="virtual"` (Xvfb) on Linux, a headed window on macOS.
`PENNY_BROWSER_HEADLESS` forces one mode for both, so override it only for
experiments. Лента renumbers pickup store ids occasionally while catalogue
aliases stay stable: if the configured id is rejected, the bot resolves the
current id through the pickup-store list by alias and caches it for the process.

Дикси deliberately stays on a plain HTTP transport. A flagged proxy exit gets
Yandex SmartCaptcha: Dixy's 403 page shows a «Я не робот» checkbox, a click
escalates to a slider, and pulling that slider escalates to an image-text
challenge (verified live 2026-10-31). The last step is a real visual captcha
and is not solved by design — no OCR, no model, no solving services. While the
exit stays flagged the transport fails fast, sits out a 30-minute cooldown,
and the reply carries an honest «проверка не удалась» note while other stores
answer. A human can unblock it from Telegram — the reply offers a «Пройти
капчу Дикси» button, and `/dixy_captcha` does the same (see below).

## Run

Use [uv](https://docs.astral.sh/uv/getting-started/installation/) on both
macOS and Linux. On macOS you can install it with `brew install uv`.
Python 3.13 is the supported runtime, recorded in `.python-version`; uv downloads
it if necessary. `uv.lock` records exact dependency resolutions and belongs in Git.
The browser stores need the Camoufox binary once: `./scripts/uv run --locked python -m camoufox fetch`
(and `apt install xvfb` on a Linux server for Лента).

From the project directory:

```sh
./scripts/uv sync --locked
# On first setup only: copy .env.example to .env and fill it in.
./scripts/uv run --locked penny
```

Keep an existing `.env`; do not overwrite it. The wrapper picks an environment
by OS and architecture, such as `.venv-macos-arm64` or `.venv-linux-aarch64`.
Use `./scripts/uv` instead of bare `uv` for this shared macOS/Orb checkout.
Old `.venv` and `.venv-macos` directories are no longer used and may be removed
manually once the new setup works. The VM’s old environment also contains the uv tool used for migration.
Never copy an environment to another OS or machine: recreate it from the lockfile.
Stop a running Penny with Ctrl+C: it prints that it is stopping and exits within
two seconds even if a store lookup or browser search is in flight. A second
Ctrl+C exits immediately instead of waiting for the grace period.

The same commands work on the home server. The SOCKS proxy address must be
reachable from the environment running Penny: `127.0.0.1` refers to that
environment, not necessarily your Mac. Configuration and secrets stay local.

To run Penny as a pod on a k3s node (for example a spare laptop joined to an
existing cluster), see `deploy/README.md`: GitHub Actions builds the image and
deploys over the same SSH tunnel as inbucket, with one replica, PVCs for `data/`
and `state/`, and the node label/taint that pins it.

To deliberately update dependencies, run `./scripts/uv lock --upgrade`, then
`./scripts/uv sync --locked`, run the tests, and review/commit `uv.lock`.
Normal runs use `--locked` so they fail rather than silently changing the lockfile.

Create a Telegram bot with BotFather and put its token in `.env`. Set
ALLOWED_USER_IDS to your and your wife's numeric Telegram IDs (not usernames).
Set DEEPSEEK_API_KEY and STORE_PROXY. Never commit or share `.env`.
STORE_PROXY carries store requests (`socks5h` keeps DNS at the proxy). Telegram
and DeepSeek use the normal network connection; set TELEGRAM_PROXY to send Bot
API calls through a SOCKS5 proxy when `api.telegram.org` is filtered.
No incoming port is needed for polling.
Keep the process running on your home server; only one polling instance per bot.

Send «Сколько стоит зубная нить Splat?» in a private chat. Expect matching
products from the available stores with their prices, a store label per card, and a
search link per store. Out-of-stock items are hidden instead of shown as
unavailable. Links are explicitly search links, not invented product URLs.
Try an unrelated Telegram account: it should receive no reply.

## Local store configuration

Branch addresses, store codes and the tokens used to verify the returned
branch are not committed. Copy the example and fill it in before the first run:

```sh
cp config/stores.example.json config/stores.local.json
```

`config/stores.local.json` is gitignored and excluded from the Docker image.
`PENNY_STORES_CONFIG` can point at another file. In k3s the same file is mounted
from the `penny-stores` Secret (see `deploy/README.md`), so the location never
reaches GitHub. If the file is missing, the store transports fail closed instead
of showing prices for another branch.

## Boundaries and costs

Each accepted question permits at most three DeepSeek calls, capped at 400
output tokens (1000 for product selection), and a bounded store-research window
across five stores. DeepSeek calls retry once on transient 422/429/5xx; the Dixy
HTTP transport retries its own listing request. Price budgets
(«до 200 рублей») are parsed by the model and enforced in code: items above the
budget never render. At startup Penny
points curl_cffi at certifi's CA bundle (`CURL_CA_BUNDLE`): the macOS
curl-impersonate build lacked the GlobalSign Root R46 used by Dixy since 2025.
No unbounded retries, paid web research, browser fallback, or arbitrary tools.
One in-flight question per user, with a ten-second minimum interval.
Telegram sends retry once on TelegramNetworkError (a 20-second blip used to
turn an answered request into a false DeepSeek failure message).
Unsupported queries clear the cooldown; busy and cooldown rejections are logged
and have distinct replies (including remaining seconds for a cooldown). Use a small
prepaid DeepSeek balance; there is no monthly monetary cap in this stage.
Model and product-selection errors produce a generic failure, never guessed
prices. Search results are capped at 36 candidates and five displayed matches;
no exhaustive cheapest-price claim is made. Card requirements are shown when
reported in store badges; missing metadata is not proof of unconditional price.

Penny keeps the last successful shopping intent for each user in local SQLite for
30 minutes. The model decides whether a message is a new product or a continuation:
after «зубная нить Splat» you can say «а подешевле?», «только Splat» or «до 300
рублей». A complete product request starts a new session automatically; there is
no `/recent` workflow. Complete store results are reused for 10 minutes and clearly
labelled with their check time; say «обнови цены» to bypass the cache. Incomplete
or captcha-blocked checks are recorded as observations but never reused as complete
answers. Configure the paths/windows with `PENNY_DB_FILE`, `PENNY_SESSION_TTL` and
`PENNY_CACHE_TTL`.

Questions about the previous result are routed separately from catalogue searches.
Penny calculates requested aggregates and lists in Python from the primary products
actually shown; the model only describes the operation. Store listings do not contain
reliable ingredients or origin data, so such questions explain the source boundary
instead of inventing facts or showing fuzzy catalogue matches again.

Every fresh search also records its prices, so a card whose price changed since the
last observation shows «↘ было 99,90 ₽». `/watch` sends the products of the last
reply as buttons; picking one subscribes to it — when a later search sees the same
product cheaper, Penny messages the watcher with the old and new price and moves the
target to the new price. Products already under watch are not offered in the chooser,
so a target is never moved silently. `/history` uses the same chooser, lists the last
observations and the all-time minimum and maximum with their dates; a price-drop
alert adds that the new price is the all-time minimum. Besides the opportunistic
checks during normal lookups, active watches are checked in the background every
three hours (`PENNY_WATCH_INTERVAL`, `0` disables; `PENNY_WATCH_START_DELAY`,
`PENNY_WATCH_LIMIT`): Penny searches only the watched product's own store, skips
stores in a cooldown, records the observation in price history and messages a drop —
no DeepSeek, at most 20 products per pass. `/unwatch` lists subscriptions with
remove buttons; `/watch 2` and `/history 2` still work if you prefer typing. The
commands are registered with Telegram, so they appear in the chat's «/» menu instead
of having to be memorised.

SQLite stores the numeric allowed-user ID, the self-contained shopping request,
and store observations. Operational events are logged by default; conversation content is opt-in
(see below). User queries and candidate products are sent to DeepSeek. Private chats only.
The model never writes prices into replies: Python renders validated store data.
Product sizes appear in names when supplied; unambiguous unit prices are compared.

## Verification

CI runs Ruff and the offline tests on Linux and macOS. A successful `Tests`
run for a push to `master` triggers deployment of that exact commit; manual
deployments run their own Linux checks first. Ruff is a locked development
dependency, excluded from the production image.

```sh
./scripts/uv run --locked ruff check penny tests
```

Shared product, search-result, and section shapes live in `penny/contracts.py`.
These are incremental type hints at module boundaries, not a whole-project type
checking gate.

The code separates catalogue access from product calculations and replies:

- `penny/store_config.py` owns the shared store registry and private branch configuration.
- `penny/store.py` handles HTTP transports, normalization, branch checks and cooldowns;
  `penny/browser.py` handles browser-backed stores.
- `penny/store_execution.py` dispatches synchronous lookups to the appropriate worker,
  propagates request context and browser deadlines, and cancels queued browser work.
  Research and watch checks share it while retaining their own retry and timeout policies.
- `penny/products.py` contains transport-independent availability, unit-price calculations
  and section ordering shared by selection metadata and replies.
- `penny/rendering.py` formats validated products as Telegram HTML without modifying
  its inputs. Saved primary products follow their display order, including price
  and unit-price sorting, so watch/history choices use the same order.
- `penny/research.py` coordinates bounded searches, `penny/service.py` handles model
  intent and selection, and `penny/bot.py` connects them to Telegram and memory.

Import helpers from their owning modules in new code. The previous imports from
`penny.store` remain available for existing scripts and integrations.

```sh
./scripts/uv run --locked python -m unittest discover -s tests -v
```

GitHub Actions runs these checks on macOS and Linux with Python 3.13.
Tests use fixtures/mocks and do not spend money or contact stores. A real Telegram,
DeepSeek and residential-proxy smoke test requires your local credentials.
The async tests require local socket-pair writes: asyncio uses them to wake its
event loop after a worker completes. A sandbox that denies those writes can make
otherwise completed lookups wait until their deadline; run the offline tests with
local IPC permitted in that case.

To exercise conversational state against the real model without Telegram or stores:

```sh
./scripts/uv run --locked python -m tests.live_dialogues
```

This runs stateful dialogues across fresh searches, continuations, topic changes,
budgets, store restrictions, refreshes, result calculations, product information,
acknowledgements and unsupported messages. It also checks that similarly named products
do not leak into a brand result. It uses synthetic products and spends only DeepSeek calls.
Set `PENNY_DIALOGUE_ROUNDS=3` for repeated stability checks and
`PENNY_DIALOGUE_VERBOSE=1` to print every turn.

## Live end-to-end regression

```sh
STORE_PROXY=socks5h://127.0.0.1:10807 ./scripts/uv run --locked python -m tests.live_e2e
```

Runs a set of real queries (unsupported, foreign store, brand-with-form-word,
hypoallergenic split, cheapest intent, cross-store brand, out-of-stock, protein
bars) through the real stores and DeepSeek and checks invariants: query
extraction, store grouping, footer subset, prices rendered by code, no filler
phrases, per-section ascending price for cheapest requests. Costs a handful of
DeepSeek calls and store requests; needs a proxy reachable from the running OS
(the same STORE_PROXY as the bot).

## Next agreed stages

1. ~~Try the Telegram demo with your family.~~
2. ~~Add Магнит, SQLite conversational memory/cache, and unit-price comparisons.~~
3. ~~Add Пятёрочка, Лента and Дикси.~~ Пятёрочка and Лента share one bounded
   Camoufox worker; Дикси uses plain HTTP and degrades honestly while
   SmartCaptcha blocks the configured proxy exit.
4. Add bounded, cited product research. Price-drop alerts are checked in the
   background every three hours within a bounded pass, and opportunistically on
   normal lookups.

Reference API documentation:
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://docs.aiogram.dev/en/latest/dispatcher/long_polling.html

## Diagnose a failed lookup

Run this in the same OS and shell environment as the bot:

```sh
./scripts/uv run --locked python -m penny.diagnostics
```

It checks only the store via the configured proxy; it does not call DeepSeek or
send Telegram messages. `transport_code=7` means a connection could not be
established. The proxy address has to be reachable from wherever Penny runs: a
localhost address works only on the same machine, a VM needs an address
reachable from inside it. Environment variables supplied on the command line
take precedence over `.env`.

## Unblock Дикси by hand

`dixy.ru` blocks a flagged proxy exit at the edge with a «Я не робот» page. The
SmartCaptcha chain ends at an image-text challenge, so Penny does not solve it —
a person reads the image and types the text. Two ways:

**From Telegram (works remotely, uses the bot's proxy):**

When Дикси needs a check, the reply carries a **«Пройти капчу Дикси»** button —
the command `/dixy_captcha` does the same thing if the button has scrolled away.
Penny opens Дикси, walks the widget to the image challenge, sends the picture
to the chat and waits up to three minutes. Reply with the text exactly as
shown (case matters); wrong answers get a fresh picture, up to three tries.
On success the session cookies are saved over `state/dixy.json` (old file kept
as `state/dixy.json.bak`), the cooldown is cleared, and Penny polls one real
search until the HTTP transport answers. The edge allow applies asynchronously,
so this can take up to a minute; if it still lags, the bot asks you to retry in
a couple of minutes instead of hammering the store. If the widget never reaches
the image challenge, the bot points you to the Mac helper.

**On the Mac (real display, same proxy exit):**

```sh
STORE_PROXY=socks5://127.0.0.1:10807 ./scripts/uv run --locked python -m penny.dixy_unblock
```

The window opens Дикси through the proxy. Solve the captcha when it appears;
the script saves the refreshed storage state over `state/dixy.json` (the old
file is kept as `state/dixy.json.bak`) and verifies one real search. If the
session ends up pointing at the wrong store, re-run
`examples/dixy_select_store.py` from `../shopping` to pin the branch configured
in `config/stores.local.json`. Afterward the ordinary bot fetches Дикси over
HTTP again — the state file is re-read on every search, so no restart is needed
for the cookies.

The relay never reads the image itself: no OCR, no vision model, no solving
service. The human solve is the point. When nobody is around, Penny keeps the
30-minute captcha cooldown and shows the honest «проверка не удалась» note;
constant retries only refresh the flag.

## Diagnostic logging

Logs go to the terminal and `logs/penny.log` by default. Files rotate at 2 MB
with three backups; the directory is gitignored. Service logs, operator
messages and diagnostics are English; Telegram replies to the family stay
Russian, as do the store notes rendered inside them. Each accepted lookup has a
request ID, also included in Telegram error replies. Search that ID to follow
query extraction, store responses, selection, and Telegram delivery. Timings,
HTTP/transport codes, model finish reasons/token counts, and stack locations
help identify failures. Exception messages, locals, headers, and raw HTTP bodies
are never dumped. Diagnostics cannot recover details of earlier failures.

Add to `.env` and restart Penny:

```dotenv
PENNY_LOG_LEVEL=DEBUG
PENNY_LOG_FILE=logs/penny.log
PENNY_LOG_CONTENT=0
```

Set `PENNY_LOG_CONTENT=1` temporarily to include bounded user messages, extracted
queries, product candidates, model output and final replies. Known configured
credentials and URLs are redacted, but this mode still contains private shopping
conversations: review logs before sharing them. Set it back to `0` afterward.
Set `PENNY_LOG_FILE=` for terminal-only output.

```sh
tail -f logs/penny.log
```

## Exact matches and alternatives

Penny shows exact matches first. If there are none, it can show up to three
same-product alternatives from other brands, plus one separately labelled
alternative format (for example, floss picks instead of a spool of floss).
Unrelated items such as toothpaste returned for a floss query are omitted.
Requests such as «только Splat» or «без аналогов» suppress alternatives.
The original request is passed to classification so restrictions are not lost
when extracting the shorter store query. The model classifies product indices;
code validates bounds, group sizes and duplicates, then renders store prices.
Classification is still model-based and can be imperfect; it never supplies
prices. If the five-card section cap would hide the best-value direct candidate,
code swaps it in for the last card, so the value line always refers to a shown
product. A single-word category query also rejects name traps in code — for
example «Жевательная резинка Виноград» never passes as «вино» — while real
inflections and diminutives («вином», «сырок», «хлебцы») still match.
Research adds at most one planning call; empty combined results skip classification.

## Bounded store research

After the initial lookup, one model call chooses up to two targeted follow-up
queries, or stops if the results adequately answer a specific request. Queries
are deduplicated and capped in code. At most 108 candidates are merged; selection
sees at most 30 in-stock candidates in store order. Cheapest- and priciest-intent requests
(самый дешевый, бюджет, самый дорогой) use ascending- or descending-price store sort so the cheapest items
are actually in the results; otherwise popularity sort is used. Duplicate product IDs are merged (name fallback); the first observation is retained
with its own timestamp and source. Follow-up failures preserve earlier results
and mark the answer incomplete. No arbitrary browsing or automatic retries.

Пятёрочка's in-page fetch is capped at 12 seconds (the ServicePipe challenge
settles in about two), so a stuck challenge cannot eat the round budget that
Лента and the follow-up queries share. Its browser context and page stay alive
between searches: the ServicePipe clearance is bound to this browser's
fingerprint, so re-solving the challenge on every query is what got the exit
rate-limited. Clearance cookies are written back to `state/5ka.json`, and a
blocked or failed page is discarded so the next attempt starts clean. A static
ServicePipe block page («Проблемы со связью…») is recognised at once: the reply
says the site is temporarily blocking this connection and the store rests for
ten minutes instead of being probed on every query. A store sitting out a
failure cooldown is named in the reply with the remaining minutes instead of
disappearing silently; Дикси's captcha pause shows as «капча — пауза N мин.».

Unit prices are calculated from unambiguous g/kg/ml/l sizes in names. Ambiguous
multipacks are skipped. Cost per wash is shown only when the name states a wash
count. Comparable exact matches are ordered by unit price only when in stock,
using the same unit and card conditions, and not labelled as concentrates.
This is a comparison of selected results, not an exhaustive cheapest claim.
Crossed-out prices are not interpreted as non-card prices. Required properties
must be supported by the product name; no allergy-safety guarantees are inferred.

Replies use Telegram HTML with escaped product names, short source links and
no link previews. A short model-written summary may follow the cards (in
italics); it may add only information the cards do not show and never contains
prices. Code drops any summary sentence that names a product or brand outside
the shown cards. A code-rendered best-value line then names the cheapest (or
dearest, for price-sorted requests) shown option by unit or package price
(«выгоднее за кг среди показанных…»), scoped to the shown results only. Check
times and branch are grouped in the footer; separate observation times are
preserved when searches span different minutes. Research
query counts stay in logs. Equal unit prices are described as a tie, rather than
calling the first product cheaper. Long replies split at complete paragraphs.

Product cards use a bold numbered title, a separate package-size line when the
name ends with an unambiguous size, and bold ruble prices with Russian decimal
commas. Unknown availability is noted on its own line; in-stock items carry no
availability line and out-of-stock items are hidden entirely. Ambiguous package
descriptions stay in the original title. Cards are separated by blank lines;
the address has its own footer line.
