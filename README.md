# AI for Web3 Security: Zero to Hero

A hands-on course that takes you from *"how do I start with AI in web3 security?"*
to building, measuring, and improving your own AI bug-hunting harness around a
frontier model.

You don't get a finished tool. You become someone who builds and improves these
systems — and who keeps going long after the last module.

## The arc

Same target codebase (SecondSwap) scanned across all four modules. The detection
curve climbs from *a few* bugs to *most*.

- **Module 1 — your first harness.** One reasoning agent → structured findings.
  Honest baseline on the target: catches a few.
- **Module 2 — context + loop.** A protocol-context pass plus an exclusion
  loop that tells each round what earlier rounds already found.
- **Module 3 — an orchestrated harness.** Five specialist agents plus the
  general hunter, run in parallel, and a judge that checks every finding
  against the code. Catches more than half.
- **Module 4 — a measured harness + the launchpad** _(coming next)_. Recall against ground truth,
  honestly, plus the self-improvement loop handed over as a transferable method.

## The tool you build: `lucid`

One Python package that grows a module at a time. Built on the raw model API —
for pedagogy (learn the primitives), determinism (code doesn't improvise), and
ownership (you own the wrapper).

## Layout

- `lucid/` — the harness you build, module by module.
- `target/` — the SecondSwap contracts under test (constant across modules).
- `modules/` — the per-module build guides.

## Setup

```bash
python3 -m venv .venv          # some systems alias this as `python`
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows (PowerShell or cmd)
pip install -r requirements.txt
cp .env.example .env           # then paste your API key (the gateway URL is pre-filled)
```

Get an API key from [Requesty](https://requesty.ai) (the OpenAI-compatible
gateway the base URL is pre-filled for) and paste it into `OPENAI_API_KEY`.

Examples use Python and an OpenAI-compatible API. Set `OPENAI_MODEL` to a
currently available model ID from your provider; the legacy course fallback in
`lucid/llm.py` is not an availability guarantee. With Requesty, use its model
library or the dashboard's **Load models** button rather than guessing model
versions. `.env.example` supplies the gateway URL. To use another compatible
provider, change `OPENAI_BASE_URL`, the backend key, and the model ID.
`python -m lucid.check` confirms connectivity by making a real, paid model call.

## Run it

Run these from the repo root, so `lucid` resolves as a package:

```bash
python -m lucid.run                    # Module 1: one agent, one pass over the bundled SecondSwap
python -m lucid.run path/to/contracts  # same, on a directory you choose
python -m lucid.run --context          # Module 2: protocol context + one context-aware pass
python -m lucid.run --loop             # Module 2: context + 5-round exclusion loop (--loop 3 for fewer)
python -m lucid.run --specialists      # Module 3: 5 specialists + hunter, 10 rounds each, + judge (--specialists 5 = 5 rounds per lane)
```

Every run makes real, paid API calls. Upstream measured the bundled target at
about $0.13 for `--loop` and $1.30/~35 minutes for `--specialists`. These are
historical measurements, not estimates for your selected model or settings.

## Local web dashboard

The React dashboard runs with a FastAPI backend on your Mac. Python 3.10+
(and Node.js 22.12+ to build the frontend) are required.

```bash
# From the repository root, with the virtual environment activated:
pip install -r requirements.txt
npm --prefix ui ci
npm --prefix ui run build
python -m lucid.web
```

Open **http://127.0.0.1:8765**. The frontend has already been built on this Mac;
you can start it now with `.venv/bin/python -m lucid.web`.
If Homebrew's Node 24 executable is not on your PATH, prefix npm commands with
`PATH=/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/bin:/bin`.

- Browse the 15 saved examples without an API key or model calls.
- Scan bundled SecondSwap, upload `.sol` files/a contract folder, or preview a
  public GitHub repository.
- Choose a single pass, context-aware pass, or 1–10 exclusion rounds.
- Follow progress, filter findings, inspect evidence/context, and export JSON.
- Live scans require explicit consent: source and context are sent to the
  configured remote AI provider and incur usage charges. API keys stay on the
  backend; configure `.env` as above, then restart the server. Select a model
  per audit in the UI, or leave the field blank to use `OPENAI_MODEL`.

### Requesty and model selection

Set these values in your backend `.env` (never in frontend configuration):

```dotenv
OPENAI_API_KEY=your-requesty-api-key
OPENAI_BASE_URL=https://router.requesty.ai/v1
# OPENAI_MODEL=provider/model-id-from-your-requesty-model-library
```

The key variable is named `OPENAI_API_KEY` because the app uses the OpenAI SDK;
its value is your **Requesty** key. Uncomment and set `OPENAI_MODEL` if you want
a default for the UI and CLI, then restart the server. Otherwise, enter an
available model ID in the UI before starting each audit.

In **Choose your model**, click **Load models** to request the provider's current
model IDs through the backend, then type to search suggestions. Loading the
catalog sends no contract source and requests no inference. It is explicit,
not automatic, and uses your backend key; Requesty lists models allowed for
that key. Manual entry also supports gateway aliases or routing policy IDs.
A listed model is not a guarantee of compatibility or adequate context size.

The chosen model is pinned to the job and used for protocol context and every
finding round. Reports/history and JSON exports record the requested model,
JSON mode, and reasoning setting. If using a Requesty routing policy, that ID
is the requested route, not necessarily the actual underlying model.
Changing model settings clears paid consent. Sampling and reasoning are left
at provider defaults unless you explicitly select a reasoning effort. Disable
JSON mode for models that reject `response_format`; findings must still pass
JSON parsing and schema validation. Context generation always uses text mode.
No automatic SDK retries or paid parameter-fallback calls are made. These
provider-default settings also apply to CLI calls, replacing the earlier
hardcoded temperature/high-reasoning settings.

This is model-agnostic within the **OpenAI-compatible chat API**, not support
for every provider's native API. JSON/reasoning support, context limits, quality,
and cost vary. Source is shared with Requesty and its routed provider during
paid scans; configure spending limits in Requesty if needed.

### GitHub targets

Select **GitHub repository**, enter a repository root URL such as
`https://github.com/code-423n4/2024-12-secondswap`, and optionally choose a branch,
tag, or commit and a subdirectory such as `contracts`. Leave the ref blank to
use the default branch. File/tree/pull-request URLs are not supported.

Click **Preview repository** to download and review the Solidity manifest and
line-numbered source without an AI key or model charges. This contacts GitHub,
so normal network access and anonymous GitHub rate limits apply. Only public
repositories are supported; no GitHub credentials are accepted.

The preview resolves to an immutable commit. A paid scan uses that exact
in-memory source snapshot, not a fresh download of the branch. Repository URL,
commit, ref, and scope are included in the report/JSON export. Changing the
repository selection clears the preview and paid consent. Previews expire
in 15 minutes; only the latest 10 are kept, and restarting clears them.

Only regular `.sol` files in the selected directory are downloaded, with at most
200 files, bounded download sizes, and the existing 400,000-character annotated
source limit. No repository scripts run, dependencies are installed, or symlinks
or submodules followed. Missing external dependencies are not fetched; upload
additional source separately if needed. Large or incomplete trees are rejected
rather than silently auditing a partial target. Smaller scopes can omit relevant
cross-contract logic, so review the manifest before starting.

The server binds only to loopback. Do not expose it on your network: it is a
local tool, not an authenticated multi-user service. Uploaded source is kept
in memory and never written to a user-selected filesystem path. Audit history
is kept in memory (latest 50 jobs) and resets on restart; export reports you
want to keep. Only one live audit runs at a time. There is no cancellation:
leaving the page does not stop a paid scan. Model findings and generated context
require human verification; the exclusion loop is not guaranteed to deduplicate.

Offline validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
npm --prefix ui test
```

For frontend development, run `npm --prefix ui run dev` alongside the backend;
Vite serves on port 5173 and proxies `/api` to port 8765. See `ui/README.md`.
