# deepseek-search

One command for **live web search** and **page-to-Markdown**, built on DeepSeek's
[Search and Fetch APIs](https://api-docs.deepseek.com/guides/search/).

Written for agents: clean text output, stable exit codes, no browser, no HTML parsing,
no SDK to install. Single file, Python standard library only.

```console
$ dssearch search "DeepSeek V4 release" -n 2
1. DeepSeek V4 model released  [score 2.40]
   https://example.com/deepseek-v4
   2026-06-01T00:00:00+00:00 · Example News · authority 3
   DeepSeek has released the V4 model with improved tool calling.
```

## Install

### As an Alma skill (recommended)

```bash
alma skill install adfoke/deepseek-search
```

That is the whole install. The skill tells Alma how to call the bundled script, and the
script picks up the DeepSeek key **that Alma already has configured** — no extra setup.
Then, in any chat: *"查一下 X 最新的情况"* and Alma will reach for it.

### As a plain CLI

```bash
git clone https://github.com/adfoke/deepseek-search
ln -sf "$PWD/deepseek-search/dssearch.py" ~/.local/bin/dssearch
dssearch doctor --probe
```

Requires Python 3.9+ (macOS, Linux). No dependencies, nothing to `pip install`.

## API key

Resolved from the first source that exists:

| # | Source | Note |
|---|---|---|
| 1 | `--api-key` | explicit: only this key is used, no fallback |
| 2 | `$DEEPSEEK_API_KEY` | |
| 3 | `~/.config/dssearch/api-key` | `chmod 600`, contents stripped |
| 4 | `~/.config/alma/secrets/deepseek-api-key` | |
| 5 | **the DeepSeek provider already configured in Alma** | read from Alma's loopback API; nothing to set up |

Source 5 is why an Alma user needs no configuration at all. Alma sometimes has more than one
DeepSeek provider configured, so the script keeps a candidate list (official provider first),
**retries with the next key on a 401**, and remembers which source worked in
`~/.config/dssearch/state.json`, so later runs send one request instead of probing.

Only keys you point at `api.deepseek.com` are considered: a third-party relay provider is
skipped on purpose, so its key is never sent to DeepSeek. If every candidate fails with 401,
the account is the problem, not the tool.

**Nothing leaves the machine except requests to `api.deepseek.com`.** Keys are never logged,
never written to the state file, and `doctor` prints them masked (`sk-ab...mnop`).

## Usage

```bash
# search
dssearch search "golang generics" -n 5
dssearch search "SQLite WAL mode" -c -n 3              # with page content
dssearch search "anything" --json | jq -r '.results[].url'

# search operators are forwarded verbatim
dssearch search 'foo site:github.com OR site:huggingface.co lang:en after:2025'
dssearch search '"exact phrase" -site:medium.com'

# read one page
dssearch fetch https://api-docs.deepseek.com/guides/search
dssearch fetch https://example.com/article --max-content-chars 300   # first 300 chars

# diagnose
dssearch doctor           # list key candidates, no network
dssearch doctor --probe   # make one real request
```

### Output

```
1. DeepSeek V4 model released  [score 2.40]
   https://example.com/deepseek-v4
   2026-06-01T00:00:00+00:00 · Example News · authority 3
   DeepSeek has released the V4 model with improved tool calling.
```

`--json` prints the raw API response. Page content is only shown with `-c` (search) or none
of it is inlined by default, so a summary never silently balloons into a whole page.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | bad arguments (empty/over-128-char query, `count` out of 1..50, invalid URL) |
| 3 | auth or balance (401 / 402 / 403) |
| 4 | rate limited (429 after retries) |
| 5 | upstream error (non-retryable 4xx / 5xx) |
| 6 | success but nothing found (empty results, or a page with no extractable text) |
| 7 | network error (unreachable, timeout) |

Retries use backoff on 408 / 429 / 5xx and network errors (3 attempts by default,
`Retry-After` honoured); other 4xx are **not** retried, so a bad request costs one call.
An empty result is exit 6 — a caller can tell "no matches" from "broken".

## Options

```
--api-key KEY          explicit key (disables fallback)
--base-url URL         default https://api.deepseek.com
--timeout SECS         default 30
--proxy URL            default $DSSEARCH_PROXY, else http_proxy/https_proxy
--retries N            default 3
--json                 raw API JSON
--version

search -n/--count N    1..50, default 10
       -c/--content     request page content (need_content=true)
       --ori-question   the user's original question, improves query understanding
       --localization   an IP used to localize results
       --max-summary-chars N   default 800, 0 = unlimited
       --max-content-chars N   default 2000, only with -c

fetch  --max-content-chars N  default 0 = whole page
```

## Upstream facts worth knowing

- Billed per **successful** request, $0.75 / 1000 — not per token, not per result.
  Failed calls, empty results and timeouts are free.
- Rate limit is **per account, 5 QPS** for search and 5 QPS for fetch (separate quotas).
  Using a different key does not raise it.
- `query` is 1–128 Unicode characters and may not be all whitespace.
- `fetch` takes http/https only, with a host and no credentials in the URL.
- `rerank_score` is 0–3 (≥ 1.5 = high quality); `authority` is 0–4 (≥ 3 = highly authoritative).

## Tests

Offline, no network, no API key, everything mocked:

```bash
python3 -m unittest discover -s tests -v     # 35 tests
```

Covers request bodies/headers, optional-field pass-through, empty-result exit code, error-code
mapping, 429 retry + `Retry-After`, no-retry on 400, network errors, key resolution order and
401 fallback (including "explicit key never silently falls back"), output truncation, JSON mode,
and URL validation.

## Layout

```
SKILL.md              how an agent should use this (read by Alma)
dssearch.py           the whole implementation, one file
tests/test_dssearch.py
README.md
```

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with DeepSeek. Search results come from DeepSeek's in-house search engine.
