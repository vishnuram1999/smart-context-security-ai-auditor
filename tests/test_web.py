"""Offline backend tests; provider calls are always mocked."""

import json
import os
import re
import time
import unittest
from threading import Event
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from lucid.web import (
    AuditRequest,
    AuditStore,
    SourceFile,
    app,
    parse_findings,
    uploaded_codebase,
)

HEADERS = {"X-Lucid-Request": "local-ui"}
FINDING = {
    "title": "Example", "severity": "high", "location": "Token.sol:1",
    "description": "Example logic", "impact": "Example impact", "exploit_steps": ["Example step"],
}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client_context = TestClient(app, base_url="http://localhost:8765")
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def wait(self, job_id):
        for _ in range(100):
            job = self.client.get(f"/api/audits/{job_id}").json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.01)
        self.fail("Mock audit did not finish")

    def test_config_and_examples(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://user:secret@example.com/v1"}):
            config = self.client.get("/api/config").json()
        self.assertEqual(config["provider"], "example.com")
        self.assertNotIn("secret", json.dumps(config))
        examples = self.client.get("/api/examples").json()
        self.assertEqual(len(examples), 13)
        self.assertIn("findings", self.client.get(f"/api/examples/{examples[0]['id']}").json())
        self.assertEqual(self.client.get("/api/examples/not-found").status_code, 404)

    def test_request_guards(self):
        body = {"confirmed_paid": True}
        self.assertEqual(self.client.post("/api/audits", json=body).status_code, 403)
        headers = HEADERS | {"Origin": "https://evil.example"}
        self.assertEqual(self.client.post("/api/audits", json=body, headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/audits", json={}, headers=HEADERS).status_code, 400)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.client.post("/api/audits", json=body, headers=HEADERS).status_code, 400)
        self.assertEqual(self.client.post("/api/audits", content=b"x" * 2_000_001, headers=HEADERS).status_code, 413)

    def test_upload_validation(self):
        code = uploaded_codebase([SourceFile(path="B.sol", content="b"), SourceFile(path="a/A.sol", content="a")])
        self.assertEqual(code, "// FILE: B.sol\n1: b\n\n// FILE: a/A.sol\n1: a\n")
        unicode_source = uploaded_codebase([SourceFile(path="A.sol", content="// comment\u2028separator\r\ncontract A {}\n")])
        self.assertIn("1: // comment\u2028separator\n2: contract A {}", unicode_source)
        for name in ("../A.sol", "/A.sol", "A.txt", "a\nA.sol", "C:/A.sol"):
            with self.assertRaises(ValueError):
                uploaded_codebase([SourceFile(path=name, content="a")])
        with self.assertRaises(ValueError):
            uploaded_codebase([SourceFile(path="A.sol", content="a")] * 2)
        with self.assertRaises(ValueError):
            uploaded_codebase([SourceFile(path="A.sol", content="x" * 400_000)])

    def test_bad_model_output_is_not_clean(self):
        for raw in ("not json", "[]", '{"findings": null}', '{"findings": [{}]}'):
            with self.assertRaises(ValueError):
                parse_findings(raw)
        findings, warnings = parse_findings(json.dumps({"findings": [FINDING, {}]}))
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(warnings), 1)

    def test_loop_progress_and_exclusion(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.context.build_context", return_value="Protocol context"), patch("lucid.web.llm.complete", side_effect=[json.dumps({"findings": [FINDING]}), '{"findings": []}']) as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": "loop", "rounds": 2})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["completed_rounds"], 2)
            self.assertEqual(job["context"], "Protocol context")
            self.assertEqual(len(job["findings"]), 1)
            self.assertIn("Already found", complete.call_args_list[1].args[1])
        self.assertEqual(len(self.client.get("/api/audits").json()), 1)

    def test_provider_failure_redacted(self):
        for mode, target, error in (
            ("single", "lucid.web.llm.complete", RuntimeError("secret-token")),
            ("single", "lucid.web.llm.complete", ValueError("secret-token")),
            ("context", "lucid.web.context.build_context", ValueError("secret-token")),
        ):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch(target, side_effect=error):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": mode})
                job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "failed")
            self.assertNotIn("secret-token", job["error"])

    def test_shutdown_stops_additional_calls(self):
        store = AuditStore()
        entered, release = Event(), Event()
        def complete(*args, **kwargs):
            entered.set()
            release.wait(2)
            return '{"findings": []}'
        with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            try:
                job = store.start(AuditRequest(mode="loop", rounds=2), "contract A {}")
                self.assertTrue(entered.wait(2))
                with self.assertRaises(HTTPException) as conflict:
                    store.start(AuditRequest(), "contract A {}")
                self.assertEqual(conflict.exception.status_code, 409)
                store.close()
            finally:
                release.set()
                store.executor.shutdown(wait=True)
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(store.get(job["id"])["status"], "failed")

    def test_single_and_parse_failure(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete", return_value="bad JSON"):
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "target": "upload", "files": [{"path": "A.sol", "content": "contract A {}"}]})
            self.assertEqual(self.wait(response.json()["id"])["status"], "failed")

    def test_github_preview_and_pinned_audit(self):
        snapshot = {
            "repository_url": "https://github.com/owner/repo", "repository": "owner/repo",
            "ref": "main", "commit": "a" * 40, "subdirectory": "src",
            "files": [{"path": "src/A.sol", "content": "contract A {}"}],
            "file_count": 1, "total_chars": 13,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("lucid.web.fetch_repository", return_value=snapshot) as fetch, patch("lucid.web.llm.complete") as complete:
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"], "subdirectory": "src"})
            self.assertEqual(response.status_code, 200)
            preview = response.json()
            self.assertEqual(preview["commit"], "a" * 40)
            complete.assert_not_called()
            self.assertEqual(fetch.call_count, 1)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.fetch_repository") as fetch, patch("lucid.web.llm.complete", return_value='{"findings": []}') as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"target": "github", "github_preview_id": preview["id"], "confirmed_paid": True})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["source"]["commit"], preview["commit"])
            self.assertEqual(job["target"], "owner/repo")
            self.assertIn("// FILE: src/A.sol", complete.call_args.args[1])
            fetch.assert_not_called()

    def test_github_preview_errors_and_expiry(self):
        from lucid.github import GitHubSourceError
        with patch("lucid.web.fetch_repository", side_effect=GitHubSourceError("No Solidity files")):
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": "https://github.com/owner/repo"})
            self.assertEqual(response.status_code, 400)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete") as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"target": "github", "github_preview_id": "missing", "confirmed_paid": True})
            self.assertEqual(response.status_code, 400)
            complete.assert_not_called()
        store = app.state.previews
        snapshot = {"repository_url": "url", "repository": "owner/repo", "ref": "main", "commit": "a" * 40, "subdirectory": ""}
        for _ in range(12):
            store.add(snapshot, "code")
        self.assertEqual(len(store.snapshots), 10)
        with patch("lucid.web.time.monotonic", return_value=time.monotonic() + 901), self.assertRaises(HTTPException):
            store.get(next(iter(store.snapshots)))

    def test_model_catalog_is_explicit_and_redacts_errors(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("lucid.web.llm.list_models") as catalog:
            self.assertEqual(self.client.get("/api/models").status_code, 400)
            catalog.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", return_value=[{"id": "provider/model"}]) as catalog, patch("lucid.web.llm.complete") as complete:
            self.assertEqual(self.client.get("/api/models").json(), {"models": [{"id": "provider/model"}]})
            catalog.assert_called_once()
            complete.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", side_effect=RuntimeError("secret-token")):
            response = self.client.get("/api/models")
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("secret-token", response.text)

    def test_model_selection_is_pinned_for_context_and_every_round(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_MODEL": "provider/default"}), patch("lucid.web.context.build_context", return_value="Context") as build, patch("lucid.web.llm.complete", return_value='{"findings": []}') as complete:
            self.assertEqual(self.client.get("/api/config").json()["model"], "provider/default")
            response = self.client.post("/api/audits", headers=HEADERS, json={
                "confirmed_paid": True, "mode": "loop", "rounds": 2,
                "model": " provider/selected ", "json_mode": False, "reasoning_effort": "medium",
            })
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["model"], "provider/selected")
            self.assertFalse(job["json_mode"])
            self.assertEqual(job["reasoning_effort"], "medium")
            self.assertEqual(build.call_args.kwargs, {"model": "provider/selected", "reasoning_effort": "medium"})
            self.assertEqual(complete.call_count, 2)
            for call in complete.call_args_list:
                self.assertEqual(call.kwargs, {"model": "provider/selected", "json_mode": False, "reasoning_effort": "medium"})

    def test_invalid_model_settings_never_start_inference(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete") as complete:
            for settings in ({"model": "bad model"}, {"model": ""}, {"model": "x" * 257}, {"reasoning_effort": "unsupported"}):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True} | settings)
                self.assertEqual(response.status_code, 422)
            with patch.dict(os.environ, {"OPENAI_MODEL": ""}):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True})
                self.assertEqual(response.status_code, 400)
            complete.assert_not_called()

    def test_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, "Build the UI before running dashboard tests")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', response.text)
        self.assertGreaterEqual(len(assets), 2)
        for asset in assets:
            self.assertEqual(self.client.get(asset).status_code, 200)


if __name__ == "__main__":
    unittest.main()
