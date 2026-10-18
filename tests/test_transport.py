#!/usr/bin/env python3
"""Transport-level tests: a real socket, a real HTTP server, real headers.

Every other test mocks `post_json`, so until now the urllib path (status codes,
response headers, timeouts) had never been executed. These tests stand up a
throwaway HTTP server on loopback and let the CLI talk to it.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import http.server
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dssearch  # noqa: E402

STATE = "/tmp/dssearch-test-state.json"
CACHE = "/tmp/dssearch-test-cache"


class Stub:
    """A loopback HTTP server whose replies a test scripts in advance."""

    def __init__(self):
        self.requests = []          # (path, headers, body)
        self.replies = []           # list of (status, headers, body, delay)
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                stub.requests.append(
                    (self.path, dict(self.headers.items()), raw)
                )
                if stub.replies:
                    status, headers, body, delay = stub.replies.pop(0)
                else:
                    status, headers, body, delay = 200, {}, b'{"results": []}', 0
                if delay:
                    time.sleep(delay)
                payload = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


SEARCH_BODY = {
    "results": [
        {
            "rerank_score": 2.4,
            "title": "DeepSeek V4 model released",
            "url": "https://example.com/deepseek-v4",
            "summary": "DeepSeek has released the V4 model.",
            "site_name": "Example News",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        }
    ]
}


def run(argv, api_key="sk-test-1234567890abcdef", env=None):
    out, err = io.StringIO(), io.StringIO()
    for path in (STATE,):
        if os.path.exists(path):
            os.remove(path)
    environment = {
        "DEEPSEEK_API_KEY": api_key,
        "DSSEARCH_CACHE": "0",
        "DSSEARCH_CACHE_DIR": CACHE,
        "DSSEARCH_STATE": STATE,
    }
    environment.update(env or {})
    with mock.patch.object(dssearch, "STATE_FILE", STATE), \
            mock.patch.object(dssearch, "CACHE_DIR", CACHE), \
            mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
            mock.patch.object(dssearch, "alma_key_candidates", return_value=[]), \
            mock.patch.dict(os.environ, environment, clear=False):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dssearch.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestBaseUrlGuard(unittest.TestCase):
    """A --base-url taken from a prompt must not be able to reroute the key."""

    def test_default_host_is_allowed(self):
        self.assertEqual(
            dssearch.validate_base_url("https://api.deepseek.com/"), "https://api.deepseek.com"
        )

    def test_foreign_host_is_refused(self):
        with self.assertRaises(dssearch.DsError) as ctx:
            dssearch.validate_base_url("https://evil.example")
        self.assertEqual(ctx.exception.exit_code, dssearch.EXIT_USAGE)
        self.assertIn("not an allowed host", str(ctx.exception))

    def test_lookalike_host_is_refused(self):
        for url in ("https://api.deepseek.com.evil.example",
                    "https://api.deepseek.com@evil.example",
                    "http://169.254.169.254"):
            with self.assertRaises(dssearch.DsError):
                dssearch.validate_base_url(url)

    def test_custom_host_allowed_only_on_request(self):
        self.assertEqual(
            dssearch.validate_base_url("http://127.0.0.1:9", allow_custom=True),
            "http://127.0.0.1:9",
        )

    def test_non_http_scheme_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com", "not a url"):
            with self.assertRaises(dssearch.DsError):
                dssearch.validate_base_url(url, allow_custom=True)

    def test_key_never_reaches_a_foreign_host(self):
        with Stub() as stub:
            code, _, err = run(["search", "x", "--base-url", stub.url])
            self.assertEqual(code, dssearch.EXIT_USAGE)
            self.assertIn("not an allowed host", err)
            self.assertEqual(stub.requests, [], "a request was sent despite the guard")


class TestRealTransport(unittest.TestCase):
    def test_authorization_header_and_body_arrive(self):
        with Stub() as stub:
            stub.replies = [(200, {"Content-Type": "application/json"},
                             json.dumps(SEARCH_BODY), 0)]
            code, out, _ = run(["search", "hello", "-n", "3",
                                "--base-url", stub.url, "--allow-custom-host"])
        self.assertEqual(code, 0)
        path, headers, body = stub.requests[0]
        self.assertEqual(path, "/v1/tools/search")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-1234567890abcdef")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body.decode()), {"query": "hello", "count": 3})
        self.assertIn("DeepSeek V4 model released", out)

    def test_real_401_maps_to_auth_exit(self):
        with Stub() as stub:
            stub.replies = [(401, {}, '{"error":{"message":"Authentication Fails"}}', 0)]
            code, _, err = run(["search", "x", "--base-url", stub.url, "--allow-custom-host"])
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertIn("Authentication Fails", err)

    def test_real_429_then_success_uses_retry_after(self):
        with Stub() as stub:
            stub.replies = [
                (429, {"Retry-After": "0"}, '{"error":{"message":"slow down"}}', 0),
                (200, {}, json.dumps(SEARCH_BODY), 0),
            ]
            started = time.monotonic()
            code, out, _ = run(["search", "x", "--base-url", stub.url,
                                "--allow-custom-host", "--retries", "3"])
            elapsed = time.monotonic() - started
        self.assertEqual(code, 0)
        self.assertEqual(len(stub.requests), 2)
        self.assertLess(elapsed, 10, "Retry-After should not be multiplied into minutes")

    def test_real_500_exhausts_attempts(self):
        with Stub() as stub:
            stub.replies = [(500, {}, '{"error":{"message":"boom"}}', 0)] * 4
            code, _, err = run(["search", "x", "--base-url", stub.url,
                                "--allow-custom-host", "--retries", "2"])
        self.assertEqual(code, dssearch.EXIT_UPSTREAM)
        self.assertEqual(len(stub.requests), 2)
        self.assertIn("boom", err)

    def test_unreadable_json_body(self):
        with Stub() as stub:
            stub.replies = [(200, {}, "this is not json", 0)]
            code, _, err = run(["search", "x", "--base-url", stub.url, "--allow-custom-host"])
        self.assertEqual(code, dssearch.EXIT_UPSTREAM)
        self.assertIn("unreadable response", err)

    def test_real_timeout(self):
        with Stub() as stub:
            stub.replies = [(200, {}, json.dumps(SEARCH_BODY), 2.0)] * 4
            code, _, err = run(["search", "x", "--base-url", stub.url,
                                "--allow-custom-host", "--timeout", "0.4", "--retries", "1"])
        self.assertEqual(code, dssearch.EXIT_NETWORK)
        self.assertIn("network error", err)

    def test_connection_refused_is_a_network_error(self):
        # Port 9 on loopback: nothing listens there.
        code, _, err = run(["search", "x", "--base-url", "http://127.0.0.1:9",
                            "--allow-custom-host", "--retries", "1"])
        self.assertEqual(code, dssearch.EXIT_NETWORK)

    def test_fetch_end_to_end_unescapes(self):
        escaped = {"title": "A \\& B",
                   "content": "2026\\-10\\-18 09\\:24 Source\\: Xinhua\\, Beijing\\."}
        with Stub() as stub:
            stub.replies = [(200, {"Content-Type": "application/json"},
                             json.dumps(escaped), 0)]
            code, out, _ = run(["fetch", "https://example.com/a",
                                "--base-url", stub.url, "--allow-custom-host"])
            path, _, body = stub.requests[0]
        self.assertEqual(code, 0)
        self.assertEqual(path, "/v1/tools/fetch")
        self.assertEqual(json.loads(body.decode()), {"url": "https://example.com/a"})
        self.assertIn("# A & B", out)
        self.assertIn("2026-10-18 09:24 Source: Xinhua, Beijing.", out)
        self.assertNotIn("\\-", out)

    def test_fetch_can_keep_escapes(self):
        payload = {"title": "T", "content": "a\\-b"}
        with Stub() as stub:
            stub.replies = [(200, {}, json.dumps(payload), 0)]
            code, out, _ = run(["fetch", "https://example.com/a", "--base-url", stub.url,
                                "--allow-custom-host", "--no-unescape"])
        self.assertEqual(code, 0)
        self.assertIn("a\\-b", out)

    def test_search_content_is_unescaped_with_content_flag(self):
        payload = {"results": [{"title": "T", "url": "https://e.com",
                                "content": "a\\-b c\\.d"}]}
        with Stub() as stub:
            stub.replies = [(200, {}, json.dumps(payload), 0)]
            code, out, _ = run(["search", "q", "-c", "--base-url", stub.url,
                               "--allow-custom-host"])
        self.assertEqual(code, 0)
        self.assertIn("a-b c.d", out)
        self.assertNotIn("\\-b", out)


class TestDeadline(unittest.TestCase):
    def test_deadline_stops_before_a_hopeless_retry(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return 500, {}, b'{"error":{"message":"boom"}}'

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            with mock.patch.object(dssearch.time, "sleep", return_value=None):
                # start = 0.0, then the check before attempt 2 sees 100s elapsed
                with mock.patch.object(dssearch.time, "monotonic",
                                       side_effect=[0.0, 100.0]):
                    with self.assertRaises(dssearch.DsError) as ctx:
                        dssearch.call_api("search", {"query": "q"}, "k",
                                          retries=3, timeout=30, deadline=45)
        self.assertEqual(len(calls), 1, "retried despite having no time left")
        self.assertIn("out of time", str(ctx.exception))

    def test_first_attempt_is_always_allowed(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, b'{"results": []}')):
            data = dssearch.call_api("search", {"query": "q"}, "k",
                                     timeout=30, deadline=0.001)
        self.assertEqual(data, {"results": []})


class TestMarkdownUnescape(unittest.TestCase):
    def test_commonmark_punctuation(self):
        cases = {
            r"Search \/ Fetch": "Search / Fetch",
            r"available\, powered": "available, powered",
            r"in\-house engine\.": "in-house engine.",
            r"Source\: Xinhua": "Source: Xinhua",
            r"2026\-10\-18 09\:24\:15": "2026-10-18 09:24:15",
            r"\*literal\*": "*literal*",
            r"\[not a link\]": "[not a link]",
            r"\|pipe\|": "|pipe|",
        }
        for raw, expected in cases.items():
            self.assertEqual(dssearch.unescape_markdown(raw), expected, raw)

    def test_leaves_non_escapable_backslashes(self):
        self.assertEqual(dssearch.unescape_markdown(r"C:\temp\new"), r"C:\temp\new")
        self.assertEqual(dssearch.unescape_markdown(r"a \b"), r"a \b")
        self.assertEqual(dssearch.unescape_markdown("代码\\不转义"), "代码\\不转义")

    def test_empty_and_plain_text(self):
        self.assertEqual(dssearch.unescape_markdown(""), "")
        self.assertEqual(dssearch.unescape_markdown("no escapes here"), "no escapes here")

    def test_idempotent_on_clean_text(self):
        once = dssearch.unescape_markdown(r"a\-b")
        self.assertEqual(dssearch.unescape_markdown(once), once)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.parts = ["search", "https://api.deepseek.com", "q", 10, False, "", ""]
        for path in (STATE,):
            if os.path.exists(path):
                os.remove(path)

    def tearDown(self):
        for name in os.listdir(CACHE) if os.path.isdir(CACHE) else []:
            os.remove(os.path.join(CACHE, name))

    def test_second_identical_search_hits_cache(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            with mock.patch.object(dssearch, "CACHE_DIR", CACHE):
                with mock.patch.object(dssearch, "STATE_FILE", STATE):
                    with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent"]):
                        with mock.patch.object(dssearch, "alma_key_candidates", return_value=[]):
                            with mock.patch.dict(os.environ,
                                                 {"DEEPSEEK_API_KEY": "sk-x",
                                                  "DSSEARCH_CACHE": "1",
                                                  "DSSEARCH_CACHE_DIR": CACHE}):
                                run(["search", "q"], env={"DSSEARCH_CACHE": "1"})
                                run(["search", "q"], env={"DSSEARCH_CACHE": "1"})
        self.assertEqual(len(calls), 1, "the second identical search should be served from cache")

    def test_no_cache_flag_bypasses(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            with mock.patch.object(dssearch, "CACHE_DIR", CACHE):
                run(["search", "q", "--no-cache"], env={"DSSEARCH_CACHE": "1"})
                run(["search", "q", "--no-cache"], env={"DSSEARCH_CACHE": "1"})
        self.assertEqual(len(calls), 2)

    def test_expired_entry_is_ignored(self):
        parts = ["search", "https://api.deepseek.com", "old", 10, False, "", ""]
        with mock.patch.object(dssearch, "CACHE_DIR", CACHE):
            dssearch.cache_put(parts, SEARCH_BODY, ttl=60)
            self.assertIsNotNone(dssearch.cache_get(parts, ttl=60))
            self.assertIsNone(dssearch.cache_get(parts, ttl=0))

    def test_corrupt_cache_file_is_a_miss_not_a_crash(self):
        parts = ["search", "https://api.deepseek.com", "broken", 1, False, "", ""]
        with mock.patch.object(dssearch, "CACHE_DIR", CACHE):
            path = dssearch._cache_path(parts)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertIsNone(dssearch.cache_get(parts, ttl=300))

    def test_empty_results_are_not_cached(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return 200, {}, b'{"results": []}'

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            run(["search", "nothing"], env={"DSSEARCH_CACHE": "1"})
            run(["search", "nothing"], env={"DSSEARCH_CACHE": "1"})
        self.assertEqual(len(calls), 2, "an empty result must not be cached as an answer")


class TestEmptyExitCode(unittest.TestCase):
    def test_default_is_six(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, b'{"results": []}')):
            code, out, _ = run(["search", "nothing"])
        self.assertEqual(code, dssearch.EXIT_EMPTY)
        self.assertIn("No results.", out)

    def test_flag_overrides(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, b'{"results": []}')):
            code, _, _ = run(["search", "nothing", "--empty-exit", "0"])
        self.assertEqual(code, 0)

    def test_env_overrides(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, b'{"results": []}')):
            code, _, _ = run(["search", "nothing"], env={"DSSEARCH_EMPTY_EXIT": "1"})
        self.assertEqual(code, 1)

    def test_out_of_range_falls_back_to_six(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, b'{"results": []}')):
            for bad in ("300", "-4", "abc"):
                code, _, _ = run(["search", "nothing"], env={"DSSEARCH_EMPTY_EXIT": bad})
                self.assertEqual(code, dssearch.EXIT_EMPTY, bad)


class TestKeyFilePermissions(unittest.TestCase):
    def test_world_readable_key_warns(self):
        path = "/tmp/dssearch-loose-key"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("sk-loose")
        os.chmod(path, 0o644)
        try:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(dssearch, "KEY_FILES", [path]), \
                    mock.patch.object(dssearch, "STATE_FILE", STATE), \
                    mock.patch.object(dssearch, "alma_key_candidates", return_value=[]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    dssearch.resolve_api_keys(env={})
        finally:
            os.remove(path)
        self.assertIn("chmod 600", err.getvalue())
        self.assertIn(path, err.getvalue())

    def test_tight_key_file_is_quiet(self):
        path = "/tmp/dssearch-tight-key"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("sk-tight")
        os.chmod(path, 0o600)
        try:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(dssearch, "KEY_FILES", [path]), \
                    mock.patch.object(dssearch, "STATE_FILE", STATE), \
                    mock.patch.object(dssearch, "alma_key_candidates", return_value=[]):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    dssearch.resolve_api_keys(env={})
        finally:
            os.remove(path)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
