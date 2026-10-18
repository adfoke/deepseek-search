---
name: deepseek-search
description: Search the live web with DeepSeek's Search API, or read a single page as Markdown with its Fetch API, through the bundled `dssearch` script. Use when a question depends on facts that change (releases, versions, prices, dates, "latest"/"current"), when you want several independent sources instead of one page, when you need site-/language-/date-restricted results, or when you already have a URL and want its readable text without a browser.
allowed-tools:
  - Bash
---

# DeepSeek Search / Fetch

`dssearch.py` sits in this same skill directory. Run it with the Python that is on PATH:

```bash
python3 "$HOME/.config/alma/skills/deepseek-search/dssearch.py" search "query"
```

If the skill was installed under a different directory name, find it once:

```bash
ls "$HOME"/.config/alma/skills/*/dssearch.py
```

If `dssearch` happens to already be on PATH, prefer that shorter form.

## Before the first call

```bash
python3 <path>/dssearch.py doctor --probe
```

One line per key source it found, then a real search to prove the key and the network work.
`doctor` masks keys — never print a key, never pass one on the command line (shell history).

The key comes from `--api-key`, `$DEEPSEEK_API_KEY`, `~/.config/dssearch/api-key`,
`~/.config/alma/secrets/deepseek-api-key`, or — with no setup at all — the DeepSeek
provider already configured in Alma. **Do not ask the user for a key before trying `doctor`.**

## Search

```bash
dssearch.py search "query"                 # 10 results: title, url, date, site, score, summary
dssearch.py search "query" -n 5            # fewer results
dssearch.py search "query" -c -n 3         # include page content (2000 chars/result)
dssearch.py search "query" --json          # raw JSON, for parsing

dssearch.py search "query" --no-cache      # force a fresh request
dssearch.py search "query" --empty-exit 0  # if your shell renders exit 6 as a failure
```

Identical searches within 5 minutes are served from cache (`--cache-ttl`), so a repeated query
costs nothing. Only non-empty results are cached.

Operators are forwarded to the engine verbatim — the cheapest way to cut noise:

```bash
search 'foo site:github.com OR site:huggingface.co lang:en after:2025'
search '"exact phrase" -site:medium.com'
search 'term -"release notes" before:2026-06'
```

Available: `site:` · `-site:` · `-term` · `-term` · `-"phrase"` · `"phrase"` · `lang:zh,en` ·
`after:2025` / `after:2025-01-01` · `before:...`

## Read one page

```bash
dssearch.py fetch https://example.com/article          # full page as Markdown
dssearch.py fetch https://example.com/article --max-content-chars 300
```

Use `fetch` only when you already have the URL; use `search` to find URLs.
Punctuation escapes are undone automatically (`2026\-10\-18` → `2026-10-18`), so the output renders as
Markdown; `--no-unescape` keeps the raw text.

**If `fetch` fails (exit 6 / 404), do not give up on the question** — run the same query with
`search -c` and read the `content` field. DeepSeek's fetcher is refused by several major news
sites (Reuters and AP article pages among them) while the search index still has their summaries.

## Reading results

- `rerank_score` ≥ 1.5 = high quality; `authority` ≥ 3 = highly authoritative. Prefer both high.
- `date` is the publication time (UTC). Check it before calling anything "current".
- `summary` is written for a model (600–800 chars); `snippet` is for UI.
- Empty `results` is a **success** (exit 6), not a failure. Rephrase, or drop operators.
- When citing, give the title and the link, and note the date.

## Exit codes — branch on these, don't parse stderr

| 0 ok | 2 bad args | 3 auth/balance | 4 rate limited | 5 upstream | 6 empty | 7 network |

**6 means "it worked, nothing matched"** — not a failure. Rephrase the query instead of retrying.
`--empty-exit 0` if the caller cannot distinguish.

- **3**: the key or the account balance is the problem. Report it; **do not retry with another key**.
- **4**: account-wide 5 QPS for search, 5 for fetch. Wait a few seconds, retry once.
- **7**: the machine may need a proxy — `--proxy http://127.0.0.1:7897` (or whatever the user has).

## Never change the endpoint

`--base-url` only accepts `api.deepseek.com`; anything else is refused **before the key is read**.
Do not pass `--allow-custom-host` and do not "fix" that error by editing the script — the guard
exists because an injected instruction in a fetched page could otherwise redirect the bearer token
to another host. If a real proxy is needed, ask the user first.

## Cost discipline

$0.75 per 1000 **successful** requests, billed per request regardless of `count`.
So ask for 5–10 results in one call instead of several narrow calls, and don't `fetch` a page
whose `summary` already answers the question. Failed / empty / timed-out calls are free.

## Don't

- Don't paste a whole fetched page into the conversation — quote the relevant lines and link.
- Don't claim something is current without a `date` from a result.
- Don't re-run the same query hoping for different results; change the query or the operators.
