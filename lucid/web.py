"""Local-only web API and dashboard. Run with ``python -m lucid.web``."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import agent, context, llm
from .codebase import MAX_CHARS, load_codebase
from .github import GitHubSourceError, fetch_repository
from .schema import Finding

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "src"
DIST = ROOT / "ui" / "dist"
EXAMPLES = ROOT / "target" / "runs"
MAX_REQUEST_BYTES = 2_000_000
ALLOWED_ORIGINS = {
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in (8765, 5173)
}


class SourceFile(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=MAX_CHARS)


class AuditRequest(BaseModel):
    mode: Literal["single", "context", "loop"] = "single"
    rounds: int = Field(default=5, ge=1, le=10)
    confirmed_paid: bool = False
    model: str | None = Field(default=None, max_length=256)
    json_mode: bool = True
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        return llm.validate_model(value) if value is not None else None
    target: Literal["bundled", "upload", "github"] = "bundled"
    files: list[SourceFile] = Field(default_factory=list, max_length=200)
    github_preview_id: str = Field(default="", max_length=32)


class GitHubPreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=256)
    ref: str = Field(default="", max_length=255)
    subdirectory: str = Field(default="", max_length=240)


def uploaded_codebase(files: list[SourceFile]) -> str:
    """Match the CLI's input format without writing user-supplied paths to disk."""
    if not files:
        raise ValueError("Select at least one Solidity file.")
    normalized: dict[str, str] = {}
    for file in files:
        name = file.path.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or ":" in name
            or any(ord(char) < 32 for char in name)
            or path.suffix != ".sol"
        ):
            raise ValueError("Upload paths must be relative .sol paths without traversal.")
        if name in normalized:
            raise ValueError(f"Duplicate source path: {name}")
        normalized[name] = file.content
    if not any(content.strip() for content in normalized.values()):
        raise ValueError("Selected source files are empty.")
    blocks = []
    for name, content in sorted(normalized.items()):
        # Match the source preview/editor: Unicode separators inside comments
        # are not new source lines; only LF (including CRLF) advances a line.
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        numbered = "\n".join(
            f"{index}: {line.removesuffix(chr(13))}" for index, line in enumerate(lines, 1)
        )
        blocks.append(f"// FILE: {name}\n{numbered}\n")
    codebase = "\n".join(blocks)
    if len(codebase) > MAX_CHARS:
        raise ValueError(f"Annotated source exceeds {MAX_CHARS:,} characters.")
    return codebase


class PreviewStore:
    """Bounded session snapshots so audits use precisely the previewed source."""

    def __init__(self):
        self.lock = Lock()
        self.download_lock = Lock()
        self.snapshots: dict[str, tuple[float, dict, str]] = {}

    def prune(self):
        now = time.monotonic()
        self.snapshots = {
            key: value for key, value in self.snapshots.items() if now - value[0] < 900
        }

    def add(self, preview: dict, codebase: str) -> dict:
        with self.lock:
            self.prune()
            while len(self.snapshots) >= 10:
                del self.snapshots[next(iter(self.snapshots))]
            preview = preview | {"id": uuid4().hex}
            self.snapshots[preview["id"]] = (time.monotonic(), preview, codebase)
            return preview

    def get(self, preview_id: str) -> tuple[dict, str]:
        with self.lock:
            self.prune()
            if preview_id not in self.snapshots:
                raise HTTPException(400, "GitHub preview expired or was removed. Preview the repository again; no paid call has started.")
            _, preview, codebase = self.snapshots[preview_id]
            source = {key: preview[key] for key in ("repository_url", "repository", "ref", "commit", "subdirectory")}
            return source, codebase


class AuditOutputError(ValueError):
    """A safe, application-generated message that may be displayed to the user."""


def parse_findings(raw: str) -> tuple[list[dict], list[str]]:
    """Do not represent an unusable model response as a successful clean audit."""
    try:
        data = llm.extract_json(raw)
    except ValueError as exc:
        raise AuditOutputError("Model returned unreadable JSON. Retry or check model compatibility.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise AuditOutputError("Model response must contain a findings array.")
    findings, warnings = [], []
    for index, item in enumerate(data["findings"], 1):
        try:
            findings.append(Finding.model_validate(item).model_dump())
        except ValidationError:
            warnings.append(f"Finding {index} was skipped because it did not match the schema.")
    if data["findings"] and not findings:
        raise AuditOutputError("All model findings failed validation; this is not a clean audit.")
    return findings, warnings


class AuditStore:
    def __init__(self):
        self.lock = Lock()
        self.stopping = Event()
        self.jobs: dict[str, dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lucid-audit")

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def check_shutdown(self):
        if self.stopping.is_set():
            raise AuditOutputError("Server is shutting down; no further audit rounds will start.")

    def update(self, job_id: str, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "Audit not found. History resets when the server restarts.")
            return json.loads(json.dumps(self.jobs[job_id]))

    def list(self) -> list[dict]:
        with self.lock:
            return [
                {key: value for key, value in job.items() if key not in ("context", "findings")}
                | {"findings_count": len(job["findings"])}
                for job in reversed(list(self.jobs.values()))
            ]

    def start(self, request: AuditRequest, codebase: str, source: dict | None = None) -> dict:
        model = request.model if request.model is not None else llm.default_model()
        with self.lock:
            if any(job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise HTTPException(409, "An audit is already running. Wait for it to finish.")
            while len(self.jobs) >= 50:
                del self.jobs[next(iter(self.jobs))]
            job_id = uuid4().hex
            rounds = request.rounds if request.mode == "loop" else 1
            job = {
                "id": job_id, "status": "queued", "stage": "Queued",
                "mode": request.mode, "rounds": rounds, "completed_rounds": 0,
                "findings": [], "context": None, "error": None, "warnings": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "json_mode": request.json_mode, "reasoning_effort": request.reasoning_effort,
                "target": source["repository"] if source else "SecondSwap" if request.target == "bundled" else "Uploaded contracts",
                "source": source,
            }
            self.jobs[job_id] = job
        self.executor.submit(
                    self.run, job_id, codebase, request.mode, rounds,
                    model, request.json_mode, request.reasoning_effort,
                )
        return self.get(job_id)

    def run(
        self, job_id: str, codebase: str, mode: str, rounds: int,
        model: str, json_mode: bool, reasoning_effort: str | None,
    ):
        try:
            self.check_shutdown()
            self.update(job_id, status="running", stage="Preparing audit")
            protocol_context = ""
            if mode != "single":
                self.update(job_id, stage="Building protocol context")
                self.check_shutdown()
                protocol_context = context.build_context(
                                    codebase, model=model, reasoning_effort=reasoning_effort,
                                )
                if not protocol_context.strip():
                    raise AuditOutputError("Model returned an empty protocol context. Retry the audit.")
                self.update(job_id, context=protocol_context)
            findings, warnings = [], []
            for index in range(rounds):
                self.update(job_id, stage=f"Auditing · round {index + 1} of {rounds}")
                if mode == "single":
                    prompt = agent.USER_PROMPT_TEMPLATE.format(codebase=codebase)
                elif findings:
                    prior = "\n".join(f"- {f['title']}: {f['description']}" for f in findings)
                    prompt = agent.EXCLUSION_USER_PROMPT_TEMPLATE.format(
                        codebase=codebase, context=protocol_context, already_found=prior
                    )
                else:
                    prompt = agent.CONTEXT_AWARE_USER_PROMPT_TEMPLATE.format(
                        codebase=codebase, context=protocol_context
                    )
                self.check_shutdown()
                new, notices = parse_findings(llm.complete(
                                    agent.SYSTEM_PROMPT, prompt, json_mode=json_mode,
                                    model=model, reasoning_effort=reasoning_effort,
                                ))
                findings.extend(new)
                warnings.extend(f"Round {index + 1}: {notice}" for notice in notices)
                self.update(job_id, findings=list(findings), warnings=list(warnings), completed_rounds=index + 1)
            self.update(job_id, status="completed", stage="Audit complete")
        except AuditOutputError as exc:
            self.update(job_id, status="failed", stage="Audit failed", error=str(exc))
        except Exception:  # noqa: BLE001 - provider failures must be redacted at this boundary.
            # Provider exception text can contain credentials or private source.
            self.update(
                job_id, status="failed", stage="Audit failed",
                error="Provider request failed. Check your backend API key, base URL, model, credits, and network. Earlier rounds remain available; retries may incur additional charges.",
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.audits = AuditStore()
    app.state.previews = PreviewStore()
    yield
    app.state.audits.close()


app = FastAPI(title="Lucid local auditor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Requests from this origin are not allowed."}, status_code=403)
    if request.method == "POST":
        if request.headers.get("x-lucid-request") != "local-ui":
            return JSONResponse({"detail": "Missing local UI request header."}, status_code=403)
        try:
            size = int(request.headers.get("content-length", "-1"))
        except ValueError:
            size = -1
        if size < 0 or size > MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "Request requires Content-Length and must be below 2 MB."}, status_code=413)
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                return JSONResponse({"detail": "Upload exceeds 2 MB."}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.get("/api/config")
def config():
    from urllib.parse import urlparse
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    return {
        "model": os.environ.get("OPENAI_MODEL", llm.MODEL), "provider": urlparse(base).hostname or "custom provider",
        "key_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "default_target": "SecondSwap", "default_file_count": len(list(TARGET.rglob("*.sol"))),
        "max_chars": MAX_CHARS,
    }


@app.get("/api/models")
def models():
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(400, "Configure OPENAI_API_KEY on the backend to load provider models. Manual model selection is still available.")
    try:
        return {"models": llm.list_models()}
    except Exception as exc:  # Never expose provider exception details.
        raise HTTPException(502, "Could not load provider models. Check your backend key, base URL, and network, or enter a model ID manually. No inference was requested.") from exc


@app.get("/api/examples")
def examples():
    return [
        {"id": path.stem, "title": path.stem.replace("-", " ").title(),
         "findings_count": len(json.loads(path.read_text())["findings"])}
        for path in sorted(EXAMPLES.glob("*.json"))
    ]


@app.get("/api/examples/{example_id}")
def example(example_id: str):
    paths = {path.stem: path for path in EXAMPLES.glob("*.json")}
    if example_id not in paths:
        raise HTTPException(404, "Saved example not found.")
    return json.loads(paths[example_id].read_text())


@app.get("/api/audits")
def audits(request: Request):
    return request.app.state.audits.list()


@app.get("/api/audits/{job_id}")
def audit(job_id: str, request: Request):
    return request.app.state.audits.get(job_id)


@app.post("/api/github/preview")
def preview_github(body: GitHubPreviewRequest, request: Request):
    store = request.app.state.previews
    if not store.download_lock.acquire(blocking=False):
        raise HTTPException(409, "A GitHub preview is downloading. Wait for it to finish and retry.")
    try:
        preview = fetch_repository(body.url, body.ref, body.subdirectory)
        codebase = uploaded_codebase([SourceFile.model_validate(file) for file in preview["files"]])
        return store.add(preview, codebase)
    except GitHubSourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, "GitHub source exceeds the annotated source limit or contains unsupported paths. Choose a smaller contracts subdirectory.") from exc
    finally:
        store.download_lock.release()


@app.post("/api/audits", status_code=202)
def create_audit(body: AuditRequest, request: Request):
    if not body.confirmed_paid:
        raise HTTPException(400, "Confirm paid API usage and source sharing before starting.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(400, "Set OPENAI_API_KEY in the backend .env file, then restart the server.")
    source = None
    try:
        if body.target == "github":
            source, codebase = request.app.state.previews.get(body.github_preview_id)
        else:
            codebase = load_codebase(str(TARGET)) if body.target == "bundled" else uploaded_codebase(body.files)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return request.app.state.audits.start(body, codebase, source)
    except ValueError as exc:
        raise HTTPException(400, "Configure a valid OPENAI_MODEL or select a model ID before starting. No paid call has started.") from exc


@app.get("/")
def dashboard():
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "Frontend not built. Run npm install and npm run build inside ui/.")
    return FileResponse(DIST / "index.html")


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def main():
    import uvicorn
    uvicorn.run("lucid.web:app", host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
