#!/usr/bin/env python3
"""Offline tests for dssearch. No network, no real API key needed.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dssearch  # noqa: E402

SEARCH_BODY = {
    "results": [
        {
            "rerank_score": 2.4,
            "title": "DeepSeek V4 model released",
            "url": "https://example.com/deepseek-v4",
            "snippet": "DeepSeek V4 is now available.",
            "summary": "DeepSeek has released the V4 model with tool calling.",
            "content": "",
            "site_name": "Example News",
            "site_icon": None,
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        }
    ]
}

FETCH_BODY = {"title": "Example article", "content": "# Example article\n\nArticle content."}


STATE = "/tmp/dssearch-test-state.json"


CACHE = "/tmp/dssearch-test-cache"


def run(argv, api_key="sk-test-1234567890abcdef", alma_keys=None, key_files=None, env=None):
    """Run the CLI in a sandbox: env key, fake Alma candidates, temp state, no cache.

    The cache is off by default here so that each test really exercises the
    request path; tests/test_transport.py turns it back on to test it.
    """
    out, err = io.StringIO(), io.StringIO()
    if os.path.exists(STATE):
        os.remove(STATE)
    environment = {
        "DEEPSEEK_API_KEY": api_key,
        "DSSEARCH_CACHE": "0",
        "DSSEARCH_CACHE_DIR": CACHE,
        "DSSEARCH_EMPTY_EXIT": "",
    }
    environment.update(env or {})
    with mock.patch.object(dssearch, "STATE_FILE", STATE), \
            mock.patch.object(dssearch, "CACHE_DIR", CACHE), \
            mock.patch.object(dssearch, "KEY_FILES", key_files or ["/nonexistent/key"]), \
            mock.patch.object(dssearch, "alma_key_candidates", return_value=list(alma_keys or [])), \
            mock.patch.dict(os.environ, environment, clear=False):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dssearch.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestKeyResolution(unittest.TestCase):
    def test_explicit_wins(self):
        key, source = dssearch.resolve_api_key("sk-explicit", env={"DEEPSEEK_API_KEY": "sk-env"})
        self.assertEqual((key, source), ("sk-explicit", "--api-key"))

    def test_env_beats_file_and_alma(self):
        with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                mock.patch.object(dssearch, "STATE_FILE", STATE), \
                mock.patch.object(dssearch, "alma_key_candidates",
                                  return_value=[("sk-alma", "Alma provider deepseek")]):
            key, source = dssearch.resolve_api_key(env={"DEEPSEEK_API_KEY": "sk-env"})
        self.assertEqual((key, source), ("sk-env", "$DEEPSEEK_API_KEY"))

    def test_file_beats_alma(self):
        path = "/tmp/dssearch-test-key"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("  sk-fromfile\n")
        try:
            with mock.patch.object(dssearch, "KEY_FILES", [path]):
                with mock.patch.object(dssearch, "alma_key_candidates",
                                       return_value=[("sk-alma", "Alma provider deepseek")]), \
                        mock.patch.object(dssearch, "STATE_FILE", STATE):
                    key, source = dssearch.resolve_api_key(env={})
        finally:
            os.remove(path)
        self.assertEqual((key, source), ("sk-fromfile", path))

    def test_first_key_file_wins_over_second(self):
        first, second = "/tmp/dssearch-test-key-a", "/tmp/dssearch-test-key-b"
        with open(first, "w", encoding="utf-8") as fh:
            fh.write("sk-first")
        with open(second, "w", encoding="utf-8") as fh:
            fh.write("sk-second")
        try:
            with mock.patch.object(dssearch, "KEY_FILES", [first, second]), \
                    mock.patch.object(dssearch, "STATE_FILE", STATE), \
                    mock.patch.object(dssearch, "alma_key_candidates", return_value=[]):
                key, source = dssearch.resolve_api_key(env={})
        finally:
            os.remove(first)
            os.remove(second)
        self.assertEqual((key, source), ("sk-first", first))

    def test_alma_fallback(self):
        with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                mock.patch.object(dssearch, "STATE_FILE", STATE), \
                mock.patch.object(dssearch, "alma_key_candidates",
                                  return_value=[("sk-fromalma", "Alma provider deepseek")]):
            key, source = dssearch.resolve_api_key(env={})
        self.assertEqual((key, source), ("sk-fromalma", "Alma provider deepseek"))

    def test_alma_candidates_are_ordered_and_deduped(self):
        with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                mock.patch.object(dssearch, "STATE_FILE", STATE), \
                mock.patch.object(dssearch, "alma_key_candidates",
                                  return_value=[("sk-a", "Alma provider deepseek"),
                                                ("sk-a", "Alma provider custom"),
                                                ("sk-b", "Alma provider other")]):
            keys = [k for k, _ in dssearch.resolve_api_keys(env={})]
        self.assertEqual(keys, ["sk-a", "sk-b"])

    def test_last_good_key_is_promoted(self):
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump({"last_good": "Alma provider other"}, fh)
        try:
            with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                    mock.patch.object(dssearch, "STATE_FILE", STATE), \
                    mock.patch.object(dssearch, "alma_key_candidates",
                                      return_value=[("sk-a", "Alma provider deepseek"),
                                                    ("sk-b", "Alma provider other")]):
                keys = [k for k, _ in dssearch.resolve_api_keys(env={})]
        finally:
            os.remove(STATE)
        self.assertEqual(keys, ["sk-b", "sk-a"])

    def test_nothing_found_raises(self):
        with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                mock.patch.object(dssearch, "STATE_FILE", STATE), \
                mock.patch.object(dssearch, "alma_key_candidates", return_value=[]):
            with self.assertRaises(dssearch.DsError) as ctx:
                dssearch.resolve_api_key(env={})
        self.assertEqual(ctx.exception.exit_code, dssearch.EXIT_AUTH)

    def test_mask_hides_key(self):
        self.assertEqual(dssearch.mask("sk-abcdefghijklmnop"), "sk-ab...mnop")


class TestSearch(unittest.TestCase):
    def test_payload_and_output(self):
        calls = {}

        def fake_post(url, payload, headers, timeout, proxy):
            calls.update(url=url, payload=payload, headers=headers)
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            code, out, _ = run(["search", "DeepSeek V4 release", "-n", "5"])

        self.assertEqual(code, 0)
        self.assertTrue(calls["url"].endswith("/v1/tools/search"))
        self.assertEqual(calls["payload"], {"query": "DeepSeek V4 release", "count": 5})
        self.assertEqual(calls["headers"]["Authorization"], "Bearer sk-test-1234567890abcdef")
        self.assertEqual(calls["headers"]["Content-Type"], "application/json")
        self.assertIn("DeepSeek V4 model released", out)
        self.assertIn("https://example.com/deepseek-v4", out)
        self.assertIn("authority 3", out)

    def test_optional_fields_forwarded(self):
        seen = {}

        def fake_post(url, payload, headers, timeout, proxy):
            seen.update(payload)
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            run(["search", "q", "-c", "--ori-question", "orig", "--localization", "1.2.3.4"])

        self.assertEqual(seen["need_content"], True)
        self.assertEqual(seen["ori_question"], "orig")
        self.assertEqual(seen["localization"], "1.2.3.4")

    def test_optional_fields_absent_by_default(self):
        seen = {}

        def fake_post(url, payload, headers, timeout, proxy):
            seen.update(payload)
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            run(["search", "q"])
        self.assertNotIn("need_content", seen)
        self.assertNotIn("ori_question", seen)

    def test_empty_results_exit_code(self):
        with mock.patch.object(dssearch, "post_json", return_value=(200, {}, b'{"results": []}')):
            code, out, _ = run(["search", "nothing at all"])
        self.assertEqual(code, dssearch.EXIT_EMPTY)
        self.assertIn("No results.", out)

    def test_json_mode_emits_parsable_json(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, json.dumps(SEARCH_BODY).encode())):
            code, out, _ = run(["search", "q", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["results"][0]["title"], "DeepSeek V4 model released")

    def test_content_is_hidden_unless_requested(self):
        body = json.loads(json.dumps(SEARCH_BODY))
        body["results"][0]["content"] = "A" * 100
        with mock.patch.object(dssearch, "post_json", return_value=(200, {}, json.dumps(body).encode())):
            _, out, _ = run(["search", "q"])
            self.assertNotIn("A" * 100, out)
            _, out, _ = run(["search", "q", "-c", "--max-content-chars", "20"])
            self.assertIn("truncated 80 chars", out)
            self.assertNotIn("A" * 100, out)

    def test_count_out_of_range_is_usage_error(self):
        with mock.patch.object(dssearch, "post_json", side_effect=AssertionError("no network")):
            code, _, err = run(["search", "q", "-n", "51"])
        self.assertEqual(code, dssearch.EXIT_USAGE)
        self.assertIn("--count", err)

    def test_query_length_limit(self):
        with mock.patch.object(dssearch, "post_json", side_effect=AssertionError("no network")):
            code, _, err = run(["search", "x" * 129])
        self.assertEqual(code, dssearch.EXIT_USAGE)
        self.assertIn("max 128", err)


class TestErrors(unittest.TestCase):
    def test_401_maps_to_auth(self):
        body = json.dumps({"error": {"message": "bad key", "type": "invalid_request_error",
                                     "param": None, "code": "invalid_request_error"}}).encode()
        with mock.patch.object(dssearch, "post_json", return_value=(401, {}, body)):
            code, _, err = run(["search", "q"])
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertIn("bad key", err)

    def test_402_reports_balance(self):
        body = json.dumps({"error": {"message": "Insufficient Balance", "code": "insufficient_balance"}}).encode()
        with mock.patch.object(dssearch, "post_json", return_value=(402, {}, body)):
            code, _, err = run(["search", "q"])
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertIn("Insufficient Balance", err)

    def test_429_retried_then_succeeds(self):
        responses = [(429, {}, b'{"error":{"message":"slow down"}}'),
                     (200, {}, json.dumps(SEARCH_BODY).encode())]
        sleeps = []

        def fake_post(*a, **kw):
            return responses.pop(0)

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            with mock.patch.object(dssearch.time, "sleep", side_effect=sleeps.append):
                code, out, _ = run(["search", "q", "--retries", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(len(sleeps), 1)
        self.assertIn("DeepSeek V4 model released", out)

    def test_retry_after_header_is_honoured(self):
        responses = [(429, {"Retry-After": "7"}, b'{"error":{"message":"slow down"}}'),
                     (200, {}, json.dumps(SEARCH_BODY).encode())]
        sleeps = []
        with mock.patch.object(dssearch, "post_json", side_effect=lambda *a, **k: responses.pop(0)):
            with mock.patch.object(dssearch.time, "sleep", side_effect=sleeps.append):
                run(["search", "q", "--retries", "2"])
        self.assertGreaterEqual(sleeps[0], 7.0)

    def test_429_exhausted_is_rate_limit(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(429, {}, b'{"error":{"message":"qps"}}')):
            with mock.patch.object(dssearch.time, "sleep", return_value=None):
                code, _, err = run(["search", "q", "--retries", "2"])
        self.assertEqual(code, dssearch.EXIT_RATE_LIMIT)
        self.assertIn("rate limited", err)

    def test_400_is_not_retried(self):
        attempts = []

        def fake_post(*a, **kw):
            attempts.append(1)
            return 400, {}, b'{"error":{"message":"bad param"}}'

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            with mock.patch.object(dssearch.time, "sleep", return_value=None):
                code, _, err = run(["search", "q", "--retries", "3"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(code, dssearch.EXIT_UPSTREAM)
        self.assertIn("bad param", err)

    def test_network_error_reports_network(self):
        import urllib.error
        with mock.patch.object(dssearch, "post_json",
                               side_effect=urllib.error.URLError("connection refused")):
            with mock.patch.object(dssearch.time, "sleep", return_value=None):
                code, _, err = run(["search", "q", "--retries", "2"])
        self.assertEqual(code, dssearch.EXIT_NETWORK)
        self.assertIn("connection refused", err)


class TestKeyFallback(unittest.TestCase):
    def test_invalid_key_falls_through_to_next(self):
        seen = []

        def fake_post(url, payload, headers, timeout, proxy):
            seen.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer sk-dead":
                return 401, {}, b'{"error":{"message":"invalid api key"}}'
            return 200, {}, json.dumps(SEARCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            code, out, _ = run(["search", "q"], api_key="",
                               alma_keys=[("sk-dead", "Alma provider deepseek"),
                                          ("sk-live", "Alma provider custom")])

        self.assertEqual(code, 0)
        self.assertEqual(seen, ["Bearer sk-dead", "Bearer sk-live"])
        self.assertIn("DeepSeek V4 model released", out)
        with open(STATE, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["last_good"], "Alma provider custom")
        os.remove(STATE)

    def test_all_keys_invalid_still_auth_error(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(401, {}, b'{"error":{"message":"nope"}}')):
            code, _, err = run(["search", "q"], api_key="",
                               alma_keys=[("sk-a", "one"), ("sk-b", "two")])
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertIn("nope", err)

    def test_explicit_key_never_silently_falls_back(self):
        seen = []

        def fake_post(url, payload, headers, timeout, proxy):
            seen.append(headers["Authorization"])
            return 401, {}, b'{"error":{"message":"bad explicit"}}'

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            code, _, err = run(["search", "q", "--api-key", "sk-dead"], api_key="",
                               alma_keys=[("sk-live", "Alma provider")])
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertEqual(seen, ["Bearer sk-dead"])


class TestCliSurface(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                dssearch.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(dssearch.VERSION, out.getvalue())

    def test_no_command_prints_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dssearch.main([])
        self.assertEqual(code, dssearch.EXIT_USAGE)
        self.assertIn("dssearch", out.getvalue())


class TestFetch(unittest.TestCase):
    def test_payload_and_output(self):
        calls = {}

        def fake_post(url, payload, headers, timeout, proxy):
            calls.update(url=url, payload=payload)
            return 200, {}, json.dumps(FETCH_BODY).encode()

        with mock.patch.object(dssearch, "post_json", side_effect=fake_post):
            code, out, _ = run(["fetch", "https://example.com/article"])

        self.assertEqual(code, 0)
        self.assertTrue(calls["url"].endswith("/v1/tools/fetch"))
        self.assertEqual(calls["payload"], {"url": "https://example.com/article"})
        self.assertIn("# Example article", out)
        self.assertIn("Article content.", out)

    def test_truncation(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(200, {}, json.dumps(FETCH_BODY).encode())):
            _, out, _ = run(["fetch", "https://example.com/a", "--max-content-chars", "10"])
        self.assertIn("truncated", out)
        self.assertNotIn("Article content.", out)

    def test_rejects_non_http_and_credentials(self):
        with mock.patch.object(dssearch, "post_json", side_effect=AssertionError("no network")):
            self.assertEqual(run(["fetch", "ftp://example.com/a"])[0], dssearch.EXIT_USAGE)
            self.assertEqual(run(["fetch", "https://u:p@example.com/a"])[0], dssearch.EXIT_USAGE)

    def test_404_reports_not_retrievable(self):
        with mock.patch.object(dssearch, "post_json",
                               return_value=(404, {}, b'{"error":{"message":"no content"}}')):
            code, _, err = run(["fetch", "https://example.com/none"])
        self.assertEqual(code, dssearch.EXIT_EMPTY)
        self.assertIn("no content", err)


class TestDoctor(unittest.TestCase):
    def test_masks_key_and_skips_network(self):
        with mock.patch.object(dssearch, "post_json", side_effect=AssertionError("no network")):
            code, out, _ = run(["doctor"], api_key="sk-abcdefghijklmnop")
        self.assertEqual(code, 0)
        self.assertIn("sk-ab...mnop", out)
        self.assertNotIn("sk-abcdefghijklmnop", out)

    def test_missing_key_exit_auth(self):
        with mock.patch.object(dssearch, "KEY_FILES", ["/nonexistent/key"]), \
                mock.patch.object(dssearch, "STATE_FILE", STATE), \
                mock.patch.object(dssearch, "alma_key_candidates", return_value=[]), \
                mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            with mock.patch.object(dssearch, "post_json", side_effect=AssertionError("no network")):
                code, out, _ = run(["doctor"], api_key="")
        self.assertEqual(code, dssearch.EXIT_AUTH)
        self.assertIn("NOT FOUND", out)


if __name__ == "__main__":
    unittest.main()
