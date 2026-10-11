"""P7-3 â€” real adapters over `requests`, tested with MOCKED HTTP only
(CI never needs a key or network; NEXUS_RUN_LLM_TESTS gates real calls,
which are NOT part of this file)."""
import json
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

import requests as _real_requests

import nexus_search.rag.provider as provider_mod
from nexus_search.rag.config import RagConfig
from nexus_search.rag.provider import (
    LLMResult, LLMStream, ProviderError, ProviderTimeout,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, sse_lines=None):
        self.status_code = status_code
        self._json = json_body
        self._sse = sse_lines or []

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_lines(self, decode_unicode=True):
        yield from self._sse


def _cfg(**env):
    base = {"NEXUS_ENV": "dev", "NEXUS_LLM_PROVIDER": "openai",
            "NEXUS_LLM_API_KEY": "test-key", "NEXUS_LLM_MAX_RETRIES": "2"}
    base.update(env)
    saved = {}
    for k, v in base.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield RagConfig.from_env()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _MockHTTP(unittest.TestCase):
    """Patches requests.post inside the provider module + silences jitter."""

    def setUp(self):
        self.calls = []
        self._patch_target = "nexus_search.rag.provider.requests.post"
        self._orig_post = provider_mod.requests.post
        self._orig_jitter = provider_mod._sleep_jitter
        provider_mod._sleep_jitter = lambda attempt: None  # no real sleeps

    def tearDown(self):
        provider_mod.requests.post = self._orig_post
        provider_mod._sleep_jitter = self._orig_jitter

    def _install(self, side_effects):
        """side_effects: list of callables/fakes consumed per attempt."""
        def fake_post(url, headers=None, json=None, timeout=None):
            self.calls.append({"url": url, "headers": headers,
                               "body": json, "timeout": timeout})
            eff = side_effects[min(len(self.calls) - 1, len(side_effects) - 1)]
            if isinstance(eff, Exception):
                raise eff
            return eff
        provider_mod.requests.post = fake_post


class TestOpenAIAdapter(_MockHTTP):
    def _provider(self, **env):
        cfg = next(_cfg(**env))
        return provider_mod.OpenAIProvider(cfg), cfg

    def test_non_streaming_completion(self):
        p, cfg = self._provider()
        self._install([FakeResponse(json_body={
            "choices": [{"message": {"content": "Paris is the capital."}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 5},
        })])
        r = p.complete([{"role": "user", "content": "capital of france?"}],
                       max_tokens=100)
        self.assertIsInstance(r, LLMResult)
        self.assertEqual(r.text, "Paris is the capital.")
        self.assertEqual(r.usage.tokens_in, 40)
        self.assertEqual(r.usage.tokens_out, 5)
        self.assertEqual(r.model, "gpt-4o-mini")
        # request shape: messages, temperature 0, max_tokens, NO tools key
        body = self.calls[0]["body"]
        self.assertEqual(body["messages"], [{"role": "user",
                                             "content": "capital of france?"}])
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["max_tokens"], 100)
        self.assertNotIn("tools", body)
        # the key travels ONLY in the auth header
        self.assertEqual(self.calls[0]["headers"]["Authorization"],
                         "Bearer test-key")

    def test_streaming_completion(self):
        p, _ = self._provider()
        self._install([FakeResponse(sse_lines=[
            'data: {"choices": [{"delta": {"content": "Hel"}}]}',
            'data: {"choices": [{"delta": {"content": "lo wor"}}]}',
            'data: {"choices": [{"delta": {"content": "ld"}}]}',
            'data: {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}',
            'data: [DONE]',
        ])])
        s = p.complete([{"role": "user", "content": "hi"}],
                       max_tokens=50, stream=True)
        self.assertIsInstance(s, LLMStream)
        deltas = list(s)
        self.assertEqual("".join(deltas), "Hello world")
        result = s.result()
        self.assertEqual(result.usage.tokens_in, 7)
        self.assertEqual(result.usage.tokens_out, 3)

    def test_streaming_estimates_usage_when_endpoint_omits_it(self):
        p, _ = self._provider()
        self._install([FakeResponse(sse_lines=[
            'data: {"choices": [{"delta": {"content": "abcd efgh"}}]}',
            'data: [DONE]',
        ])])
        s = p.complete([{"role": "user", "content": "hi"}],
                       max_tokens=50, stream=True)
        list(s)
        result = s.result()
        self.assertEqual(result.usage.tokens_out, 3)  # 9 chars -> ceil(9/4)

    def test_retry_on_429_then_success(self):
        p, _ = self._provider()
        self._install([FakeResponse(status_code=429),
                       FakeResponse(status_code=429),
                       FakeResponse(json_body={
                           "choices": [{"message": {"content": "ok"}}],
                           "usage": {}})])
        r = p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(r.text, "ok")
        self.assertEqual(len(self.calls), 3)  # 1 + 2 retries

    def test_retry_on_5xx_then_gives_up_cleanly(self):
        p, _ = self._provider()
        self._install([FakeResponse(status_code=503)] * 5)
        with self.assertRaises(ProviderError) as ctx:
            p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(ctx.exception.code, "provider_http_error_503")
        self.assertEqual(len(self.calls), 3)  # 1 + 2 retries, then fail

    def test_connect_error_retried_then_success(self):
        p, _ = self._provider()
        self._install([
            _real_requests.exceptions.ConnectionError("boom"),
            FakeResponse(json_body={"choices": [{"message": {"content": "ok"}}],
                                    "usage": {}}),
        ])
        r = p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(r.text, "ok")

    def test_read_timeout_is_not_retried(self):
        p, _ = self._provider()
        self._install([_real_requests.exceptions.Timeout("read timed out")])
        with self.assertRaises(ProviderTimeout):
            p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(len(self.calls), 1)  # fail fast: may have landed

    def test_401_maps_to_auth_error_without_body(self):
        p, _ = self._provider()
        self._install([FakeResponse(status_code=401,
                                    json_body={"error": "secret internal"})])
        with self.assertRaises(ProviderError) as ctx:
            p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(ctx.exception.code, "provider_auth_error")
        self.assertNotIn("secret internal", str(ctx.exception))

    def test_error_messages_never_carry_response_bodies(self):
        p, _ = self._provider()
        self._install([FakeResponse(status_code=500,
                                    json_body={"error": "DB conn string"})])
        with self.assertRaises(ProviderError) as ctx:
            p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertNotIn("DB conn string", str(ctx.exception))
        self.assertEqual(str(ctx.exception), "provider_http_error_500")


class TestAnthropicAdapter(_MockHTTP):
    def _provider(self, **env):
        merged = {"NEXUS_LLM_PROVIDER": "anthropic"}
        merged.update(env)
        cfg = next(_cfg(**merged))
        return provider_mod.AnthropicProvider(cfg), cfg

    def test_system_messages_are_split_into_the_system_field(self):
        p, _ = self._provider()
        self._install([FakeResponse(json_body={
            "content": [{"type": "text", "text": "42 is the answer."}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })])
        r = p.complete([
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "the question"},
        ], max_tokens=50)
        self.assertEqual(r.text, "42 is the answer.")
        self.assertEqual(r.usage.tokens_in, 12)
        body = self.calls[0]["body"]
        self.assertEqual(body["system"], "be terse")
        self.assertEqual(body["messages"], [{"role": "user",
                                            "content": "the question"}])
        self.assertEqual(body["max_tokens"], 50)
        self.assertNotIn("tools", body)
        self.assertEqual(self.calls[0]["headers"]["x-api-key"], "test-key")
        self.assertEqual(self.calls[0]["url"],
                         "https://api.anthropic.com/v1/messages")

    def test_streaming_events(self):
        p, _ = self._provider()
        self._install([FakeResponse(sse_lines=[
            'data: {"type": "content_block_delta", "delta": {"text": "Hel"}}',
            'data: {"type": "content_block_delta", "delta": {"text": "lo"}}',
            'data: {"type": "message_delta", "usage": {"input_tokens": 3, '
            '"output_tokens": 2}}',
            'data: {"type": "message_stop"}',
        ])])
        s = p.complete([{"role": "user", "content": "hi"}],
                       max_tokens=50, stream=True)
        deltas = list(s)
        self.assertEqual("".join(deltas), "Hello")
        result = s.result()
        self.assertEqual(result.usage.tokens_out, 2)

    def test_non_text_blocks_are_ignored(self):
        p, _ = self._provider()
        self._install([FakeResponse(json_body={
            "content": [{"type": "text", "text": "visible"},
                        {"type": "tool_use", "text": "ignored"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })])
        r = p.complete([{"role": "user", "content": "q"}], max_tokens=10)
        self.assertEqual(r.text, "visible")


if __name__ == "__main__":
    unittest.main()

