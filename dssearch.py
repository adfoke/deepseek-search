#!/usr/bin/env python3
"""dssearch - one command to search the live web, or read a page as Markdown.

Wraps DeepSeek's Search / Fetch APIs so an agent (or a shell) can get web
results without a browser and without parsing HTML.

    dssearch search "DeepSeek V4 release" -n 5
    dssearch search 'foo site:github.com lang:en after:2025'
    dssearch fetch https://example.com/article
    dssearch doctor --probe

The API key is resolved from the first of these that exists:
    1. --api-key
    2. $DEEPSEEK_API_KEY
    3. ~/.config/dssearch/api-key
    4. ~/.config/alma/secrets/deepseek-api-key
    5. the DeepSeek provider already configured in Alma (loopback API)

Only the Python standard library is used (Python 3.9+).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.1.0"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 2
# Wall clock budget for one call, retries and backoff included. 2 attempts at a
# 30s timeout would otherwise be able to block a caller for over a minute.
DEFAULT_DEADLINE = 45.0
MAX_QUERY_CHARS = 128
MAX_COUNT = 50

# The bearer token may only ever be delivered to DeepSeek's own endpoint. A
# --base-url accepted from a prompt is a key-exfiltration path: an injected
# instruction only has to append --base-url and the key travels with it.
ALLOWED_HOSTS = frozenset({"api.deepseek.com"})

# Where a key may live, in priority order after --api-key and $DEEPSEEK_API_KEY.
KEY_FILES: List[str] = [
    os.path.expanduser("~/.config/dssearch/api-key"),
    os.path.expanduser("~/.config/alma/secrets/deepseek-api-key"),
]
ALMA_SPEC = os.path.expanduser("~/.config/alma/api-spec.md")
ALMA_FALLBACK_PORT = 23001

# Remembering which key worked is a convenience, so it lives in its own dir
# instead of writing into Alma's config tree.
STATE_FILE = os.path.expanduser("~/.config/dssearch/state.json")
CACHE_DIR = os.path.expanduser("~/.config/dssearch/cache")
DEFAULT_CACHE_TTL = 300.0

# Exit codes: stable enough for a caller (me) to branch on.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_RATE_LIMIT = 4
EXIT_UPSTREAM = 5
EXIT_EMPTY = 6
EXIT_NETWORK = 7


class DsError(Exception):
    """A failure that should be reported to the user as-is."""

    def __init__(self, message: str, exit_code: int = EXIT_UPSTREAM, status: int = 0):
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status


# --------------------------------------------------------------------------
# Input guards
# --------------------------------------------------------------------------


def validate_base_url(base_url: str, allow_custom: bool = False) -> str:
    """Pin the endpoint to DeepSeek unless the caller explicitly opts out."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DsError("--base-url must be an absolute http(s) URL", EXIT_USAGE)
    host = (parsed.hostname or "").lower()
    if host in ALLOWED_HOSTS or allow_custom:
        return base_url.rstrip("/")
    raise DsError(
        "--base-url %s is not an allowed host (%s): the API key would be sent to "
        "it. Pass --allow-custom-host if that is really what you want."
        % (base_url, ", ".join(sorted(ALLOWED_HOSTS))),
        EXIT_USAGE,
    )


# CommonMark: a backslash before one of these ASCII punctuation chars is an escape.
_ESCAPABLE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")


def unescape_markdown(text: str) -> str:
    """Undo CommonMark backslash escapes.

    DeepSeek's Fetch returns text where every punctuation mark that could carry
    meaning in Markdown is escaped (``2026\\-10\\-18``, ``Source\\: Xinhua``).
    That is legal Markdown, but viewers show the backslashes and it costs tokens
    for nothing. Only the chars CommonMark defines as escapable are touched, so
    a genuine backslash survives.
    """
    return _ESCAPABLE.sub(r"\1", text)


# --------------------------------------------------------------------------
# Response cache (search only: a repeated query is the common waste)
# --------------------------------------------------------------------------


def _cache_path(parts: List[Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    return os.path.join(os.environ.get("DSSEARCH_CACHE_DIR") or CACHE_DIR, digest + ".json")


def cache_get(parts: List[Any], ttl: float) -> Optional[Dict[str, Any]]:
    if ttl <= 0:
        return None
    try:
        with open(_cache_path(parts), "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("at") or 0) > ttl:
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def cache_put(parts: List[Any], data: Dict[str, Any], ttl: float) -> None:
    if ttl <= 0:
        return
    path = _cache_path(parts)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "data": data}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def cache_disabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_cache", False):
        return True
    return str(os.environ.get("DSSEARCH_CACHE", "")).lower() in ("0", "off", "false", "no")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _build_opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    if proxy:
        handler: urllib.request.BaseHandler = urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}
        )
    else:
        # Respect http_proxy/https_proxy/no_proxy from the environment.
        handler = urllib.request.ProxyHandler()
    return urllib.request.build_opener(handler)


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    proxy: Optional[str] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """POST JSON, return (status, headers, body). Never raises for HTTP status."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = _build_opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - defensive
            pass
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, body


def _error_message(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return body.decode("utf-8", "replace").strip()[:500]
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or ""
        code = err.get("code") or err.get("type") or ""
        return "%s%s" % (msg, " (%s)" % code if code else "")
    return json.dumps(parsed, ensure_ascii=False)[:500]


def _status_to_error(status: int, message: str, what: str) -> DsError:
    if status in (401, 402, 403):
        return DsError("%s: %s" % (what, message or "unauthorized"), EXIT_AUTH, status)
    if status == 429:
        return DsError("%s: rate limited - %s" % (what, message), EXIT_RATE_LIMIT, status)
    if status == 404:
        return DsError("%s: %s" % (what, message or "not retrievable"), EXIT_EMPTY, status)
    return DsError("%s: HTTP %d - %s" % (what, status, message), EXIT_UPSTREAM, status)


def call_api(
    endpoint: str,
    payload: Dict[str, Any],
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: Optional[str] = None,
    retries: int = DEFAULT_RETRIES,
    deadline: float = DEFAULT_DEADLINE,
    sleep=None,
) -> Dict[str, Any]:
    """POST to /v1/tools/<endpoint> with retries on 408/429/5xx and transport errors."""
    url = base_url.rstrip("/") + "/v1/tools/" + endpoint
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "dssearch/1.0 (+alma)",
    }
    what = "DeepSeek %s" % endpoint
    attempts = max(1, retries)
    sleep = sleep or time.sleep
    last: Optional[DsError] = None
    start = time.monotonic()

    for attempt in range(attempts):
        if attempt:
            elapsed = time.monotonic() - start
            if elapsed + timeout > deadline:
                raise DsError(
                    "%s: out of time after %.0fs (--deadline %.0fs)"
                    % (what, elapsed, deadline),
                    (last.exit_code if last else EXIT_NETWORK),
                )
        delay = min(8.0, 0.6 * (2 ** attempt)) * (0.8 + random.random() * 0.4)
        try:
            status, resp_headers, body = post_json(url, payload, headers, timeout, proxy)
        except urllib.error.URLError as exc:
            last = DsError("%s: network error - %s" % (what, exc.reason), EXIT_NETWORK)
            if attempt + 1 < attempts:
                sleep(delay)
                continue
            raise last
        except (TimeoutError, OSError) as exc:
            last = DsError("%s: network error - %s" % (what, exc), EXIT_NETWORK)
            if attempt + 1 < attempts:
                sleep(delay)
                continue
            raise last

        if status == 200:
            try:
                return json.loads(body.decode("utf-8"))
            except Exception as exc:
                raise DsError("%s: unreadable response (%s)" % (what, exc), EXIT_UPSTREAM)

        message = _error_message(body)
        retryable = status in (408, 429) or 500 <= status < 600
        if retryable and attempt + 1 < attempts:
            retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
            if retry_after:
                try:
                    delay = max(delay, min(30.0, float(retry_after)))
                except ValueError:
                    pass
            sleep(delay)
            last = _status_to_error(status, message, what)
            continue
        raise _status_to_error(status, message, what)

    raise last or DsError("%s: failed" % what, EXIT_UPSTREAM)


# --------------------------------------------------------------------------
# API key resolution
# --------------------------------------------------------------------------


def _alma_port() -> int:
    try:
        with open(ALMA_SPEC, "r", encoding="utf-8") as fh:
            match = re.search(r"localhost:(\d+)", fh.read())
        if match:
            return int(match.group(1))
    except OSError:
        pass
    return ALMA_FALLBACK_PORT


def alma_key_candidates(timeout: float = 2.0) -> List[Tuple[str, str]]:
    """Every usable DeepSeek key Alma knows about, most-specific first."""
    url = "http://127.0.0.1:%d/api/providers" % _alma_port()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            providers = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(providers, list):
        return []

    def rank(provider: Dict[str, Any]) -> int:
        base = str(provider.get("baseURL") or "")
        host = urllib.parse.urlparse(base).netloc.lower() if base else ""
        if provider.get("type") == "deepseek" or provider.get("id") == "deepseek":
            return 0
        if host == "api.deepseek.com":
            return 1
        return 9

    found: List[Tuple[int, str, str]] = []
    for provider in providers:
        if not isinstance(provider, dict) or not provider.get("apiKey"):
            continue
        if rank(provider) >= 9:
            continue
        key = str(provider["apiKey"]).strip()
        if key:
            found.append((rank(provider), key, "Alma provider %s" % provider.get("id")))
    found.sort(key=lambda item: item[0])
    return [(key, label) for _rank, key, label in found]


def key_from_alma(timeout: float = 2.0) -> Optional[str]:
    candidates = alma_key_candidates(timeout)
    return candidates[0][0] if candidates else None


def _state_path() -> str:
    return os.environ.get("DSSEARCH_STATE") or STATE_FILE


def _read_state() -> Dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_working(label: str) -> None:
    """Remember which source last worked, so the next run does not re-probe."""
    state = _read_state()
    if state.get("last_good") == label:
        return
    state["last_good"] = label
    path = _state_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _warn_if_loose(path: str) -> None:
    """A key file readable by anyone else is worth one line on stderr."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print(
            "warning: %s is group/world accessible (mode %o); run: chmod 600 %s"
            % (path, mode, path),
            file=sys.stderr,
        )


def resolve_api_keys(
    explicit: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    use_alma: bool = True,
) -> List[Tuple[str, str]]:
    """Return ordered (key, label) candidates; the caller tries them in order."""
    env = os.environ if env is None else env
    if explicit and explicit.strip():
        # An explicit key is the caller's decision: never silently swap it for another.
        return [(explicit.strip(), "--api-key")]
    candidates: List[Tuple[str, str]] = []

    def add(key: Optional[str], label: str) -> None:
        key = (key or "").strip()
        if key and all(key != existing for existing, _ in candidates):
            candidates.append((key, label))

    add(env.get("DEEPSEEK_API_KEY"), "$DEEPSEEK_API_KEY")
    for path in KEY_FILES:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                stored = fh.read()
        except OSError:
            continue
        if stored.strip():
            _warn_if_loose(path)
        add(stored, path)
    if use_alma:
        alma = alma_key_candidates()
        last_good = _read_state().get("last_good")
        for key, label in alma:
            add(key, label)
        if last_good:
            # Promote the source that worked last time.
            for index, (key, label) in enumerate(candidates):
                if label == last_good:
                    candidates.insert(0, candidates.pop(index))
                    break
    if not candidates:
        raise DsError(
            "no DeepSeek API key found. Set $DEEPSEEK_API_KEY, write "
            "%s, or configure a DeepSeek provider in Alma." % KEY_FILES[0],
            EXIT_AUTH,
        )
    return candidates


def resolve_api_key(
    explicit: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    use_alma: bool = True,
) -> Tuple[str, str]:
    """Backwards-compatible single-key view (first candidate)."""
    return resolve_api_keys(explicit, env, use_alma)[0]


def call_with_fallback(candidates: List[Tuple[str, str]], call) -> Tuple[Any, str]:
    """Run call(key) until one key works; auth failures move to the next key."""
    last: Optional[DsError] = None
    for index, (key, label) in enumerate(candidates):
        try:
            result = call(key)
        except DsError as exc:
            last = exc
            if exc.exit_code == EXIT_AUTH and index + 1 < len(candidates):
                continue
            raise
        _remember_working(label)
        return result, label
    raise last or DsError("no usable DeepSeek API key", EXIT_AUTH)


def mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return key[:5] + "..." + key[-4:]


# --------------------------------------------------------------------------
# Output shaping
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit].rstrip() + " …[truncated %d chars]" % (len(text) - limit)
    return text


def format_search(
    result: Dict[str, Any],
    max_summary: int = 800,
    max_content: int = 0,
    unescape: bool = True,
) -> str:
    items: List[Dict[str, Any]] = result.get("results") or []
    if not items:
        return "No results."
    blocks = []
    for index, item in enumerate(items, 1):
        score = item.get("rerank_score")
        head = "%d. %s" % (index, item.get("title") or "(untitled)")
        if isinstance(score, (int, float)):
            head += "  [score %.2f]" % score
        lines = [head, "   " + (item.get("url") or "")]
        meta = []
        if item.get("date"):
            meta.append(str(item["date"]))
        if item.get("site_name"):
            meta.append(str(item["site_name"]))
        authority = item.get("authority")
        if isinstance(authority, int) and authority >= 0:
            meta.append("authority %d" % authority)
        if meta:
            lines.append("   " + " · ".join(meta))
        summary = item.get("summary") or item.get("snippet") or ""
        if summary:
            lines.append("   " + _clip(summary, max_summary).replace("\n", " "))
        content = item.get("content") or ""
        if max_content and content:
            if unescape:
                content = unescape_markdown(content)
            lines.append("   --- content ---")
            lines.append(_clip(content, max_content))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def empty_exit(args: argparse.Namespace) -> int:
    """Exit code for "worked, but found nothing" (6, unless the caller overrides)."""
    override = getattr(args, "empty_exit", None)
    if override is None:
        raw = os.environ.get("DSSEARCH_EMPTY_EXIT")
        if raw is None or raw.strip() == "":
            return EXIT_EMPTY
        try:
            override = int(raw)
        except ValueError:
            return EXIT_EMPTY
    return override if 0 <= override <= 125 else EXIT_EMPTY


def cmd_search(args: argparse.Namespace, candidates: List[Tuple[str, str]]) -> int:
    query = args.query.strip()
    if not query:
        raise DsError("query is empty", EXIT_USAGE)
    if len(query) > MAX_QUERY_CHARS:
        raise DsError("query is %d chars, max %d" % (len(query), MAX_QUERY_CHARS), EXIT_USAGE)
    if not 1 <= args.count <= MAX_COUNT:
        raise DsError("--count must be 1..%d" % MAX_COUNT, EXIT_USAGE)

    payload: Dict[str, Any] = {"query": query, "count": args.count}
    if args.need_content:
        payload["need_content"] = True
    if args.ori_question:
        payload["ori_question"] = args.ori_question
    if args.localization:
        payload["localization"] = args.localization

    ttl = 0.0 if cache_disabled(args) else args.cache_ttl
    cache_parts = [
        "search",
        args.base_url,
        query,
        args.count,
        bool(args.need_content),
        args.ori_question or "",
        args.localization or "",
    ]
    data = cache_get(cache_parts, ttl)
    if data is None:
        data, _label = call_with_fallback(
            candidates,
            lambda key: call_api(
                "search",
                payload,
                key,
                base_url=args.base_url,
                timeout=args.timeout,
                proxy=args.proxy,
                retries=args.retries,
                deadline=args.deadline,
            ),
        )
        if data.get("results"):
            cache_put(cache_parts, data, ttl)
    items = data.get("results") or []
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(
            format_search(
                data,
                args.max_summary_chars,
                args.max_content_chars if args.need_content else 0,
                unescape=not args.no_unescape,
            )
        )
    if not items:
        return empty_exit(args)
    return EXIT_OK


def cmd_fetch(args: argparse.Namespace, candidates: List[Tuple[str, str]]) -> int:
    url = args.url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DsError("url must be an absolute http(s) URL", EXIT_USAGE)
    if parsed.username or parsed.password:
        raise DsError("url must not contain credentials", EXIT_USAGE)

    data, _label = call_with_fallback(
        candidates,
        lambda key: call_api(
            "fetch",
            {"url": url},
            key,
            base_url=args.base_url,
            timeout=args.timeout,
            proxy=args.proxy,
            retries=args.retries,
            deadline=args.deadline,
        ),
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    title = data.get("title") or ""
    content = data.get("content") or ""
    if not args.no_unescape:
        title = unescape_markdown(title)
        content = unescape_markdown(content)
    if args.max_content_chars:
        content = _clip(content, args.max_content_chars)
    if title:
        print("# " + title)
        print()
    print(content)
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        candidates = resolve_api_keys(args.api_key, use_alma=not args.no_alma)
    except DsError as exc:
        print("key: NOT FOUND")
        print("  " + str(exc))
        return exc.exit_code
    print("base url: %s" % args.base_url.rstrip("/"))
    for key, label in candidates:
        print("candidate: %s  (from %s)" % (mask(key), label))
    if not args.probe:
        return EXIT_OK

    def probe(key: str) -> Dict[str, Any]:
        print("probe with %s ..." % mask(key))
        return call_api(
            "search",
            {"query": "DeepSeek", "count": 1},
            key,
            base_url=args.base_url,
            timeout=args.timeout,
            proxy=args.proxy,
            retries=1,
            deadline=args.deadline,
        )

    data, label = call_with_fallback(candidates, probe)
    items = data.get("results") or []
    print("probe OK via %s: %d result(s)" % (label, len(items)))
    if items:
        print("  " + (items[0].get("title") or "(untitled)"))
        print("  " + (items[0].get("url") or ""))
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-key", help="DeepSeek API key (default: resolve automatically)")
    common.add_argument("--base-url", default=DEFAULT_BASE_URL, help="default %(default)s")
    common.add_argument(
        "--allow-custom-host",
        action="store_true",
        help="permit a --base-url that is not api.deepseek.com (the key goes there!)",
    )
    common.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds, default %(default)s")
    common.add_argument("--proxy", default=os.environ.get("DSSEARCH_PROXY"), help="e.g. http://127.0.0.1:7897")
    common.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="attempts, default %(default)s")
    common.add_argument(
        "--deadline", type=float, default=DEFAULT_DEADLINE,
        help="total wall clock budget in seconds, default %(default)s",
    )
    common.add_argument("--no-unescape", action="store_true", help="keep CommonMark backslash escapes")
    common.add_argument("--json", action="store_true", help="print the raw API JSON")

    parser = argparse.ArgumentParser(
        prog="dssearch",
        description="DeepSeek Search / Fetch for agents.",
        epilog="Docs: https://api-docs.deepseek.com/guides/search/",
    )
    parser.add_argument("--version", action="version", version="dssearch " + VERSION)
    sub = parser.add_subparsers(dest="command")

    search = sub.add_parser("search", parents=[common], help="find web pages")
    search.add_argument("query")
    search.add_argument("-n", "--count", type=int, default=10, help="1..50, default %(default)s")
    search.add_argument("-c", "--content", dest="need_content", action="store_true", help="ask for page content")
    search.add_argument("--ori-question", help="the user's original question (helps query understanding)")
    search.add_argument("--localization", help="an IP to localize results")
    search.add_argument("--max-summary-chars", type=int, default=800, help="default %(default)s, 0 = no limit")
    search.add_argument("--max-content-chars", type=int, default=2000, help="only with -c, default %(default)s")
    search.add_argument(
        "--empty-exit", type=int, default=None,
        help="exit code when nothing is found, default %d (env: DSSEARCH_EMPTY_EXIT)" % EXIT_EMPTY,
    )
    search.add_argument("--no-cache", action="store_true", help="skip the %ds response cache" % DEFAULT_CACHE_TTL)
    search.add_argument(
        "--cache-ttl", type=float, default=DEFAULT_CACHE_TTL,
        help="seconds to reuse a previous identical search, 0 disables, default %(default)s",
    )
    search.set_defaults(func=cmd_search)

    fetch = sub.add_parser("fetch", parents=[common], help="read one page as markdown")
    fetch.add_argument("url")
    fetch.add_argument("--max-content-chars", type=int, default=0, help="0 = full page")
    fetch.set_defaults(func=cmd_fetch)

    doctor = sub.add_parser("doctor", parents=[common], help="check key resolution and connectivity")
    doctor.add_argument("--no-alma", action="store_true", help="do not read Alma's provider config")
    doctor.add_argument("--probe", action="store_true", help="make one real search request")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        # Before a key is even read: refuse to aim it at somebody else's host.
        args.base_url = validate_base_url(args.base_url, getattr(args, "allow_custom_host", False))
        if args.command == "doctor":
            return cmd_doctor(args)
        candidates = resolve_api_keys(args.api_key)
        return args.func(args, candidates)
    except DsError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
