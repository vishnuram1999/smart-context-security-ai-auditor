"""Provider requests are mocked: no network or paid inference."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lucid import context, llm


class LLMTests(unittest.TestCase):
    def client(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"findings": []}')),
        ])
        return client

    def test_model_default_and_per_call_override(self):
        client = self.client()
        with patch.dict(os.environ, {"OPENAI_MODEL": "provider/default"}), patch("lucid.llm._client", return_value=client):
            llm.complete("system", "source")
            self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "provider/default")
            llm.complete("system", "source", model=" anthropic/selected ", json_mode=False)
            args = client.chat.completions.create.call_args.kwargs
            self.assertEqual(args["model"], "anthropic/selected")
            self.assertNotIn("response_format", args)
            self.assertNotIn("temperature", args)
            self.assertNotIn("reasoning_effort", args)
            self.assertEqual(llm.default_model(), "provider/default")

    def test_json_and_reasoning_opt_in(self):
        client = self.client()
        with patch("lucid.llm._client", return_value=client):
            llm.complete("system", "source", model="provider/model", reasoning_effort="high")
        args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args["response_format"], {"type": "json_object"})
        self.assertEqual(args["reasoning_effort"], "high")

    def test_invalid_ids(self):
        for model in ("", "  ", "a b", "a\nb", "a\x00b", "a" * 257):
            with self.assertRaises(ValueError):
                llm.validate_model(model)

    def test_model_list_only_returns_unique_valid_ids(self):
        client = self.client()
        client.models.list.return_value = SimpleNamespace(data=[
            SimpleNamespace(id="b/model", secret="private"),
            SimpleNamespace(id="a/model"), SimpleNamespace(id="b/model"),
            SimpleNamespace(id="bad model"),
        ])
        with patch("lucid.llm._client", return_value=client) as factory:
            self.assertEqual(llm.list_models(), [{"id": "a/model"}, {"id": "b/model"}])
        factory.assert_called_once_with(timeout=10.0)
        client.chat.completions.create.assert_not_called()

    def test_client_uses_backend_credentials_and_disables_retries(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_BASE_URL": "https://router.requesty.ai/v1"}), patch("lucid.llm.OpenAI") as sdk:
            llm._client(timeout=10.0)
        sdk.assert_called_once_with(api_key="fake", base_url="https://router.requesty.ai/v1", timeout=10.0, max_retries=0)

    def test_context_forwards_selected_model_without_json_mode(self):
        with patch("lucid.context.llm.complete", return_value="Context") as complete:
            self.assertEqual(context.build_context("source", model="provider/model", reasoning_effort="low"), "Context")
        self.assertEqual(complete.call_args.kwargs, {"json_mode": False, "model": "provider/model", "reasoning_effort": "low"})


if __name__ == "__main__":
    unittest.main()
