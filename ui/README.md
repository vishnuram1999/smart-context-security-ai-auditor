# Lucid local UI

React + Vite frontend for the FastAPI backend in `lucid/web.py`, which listens on `127.0.0.1:8765`. Run `.venv/bin/python -m lucid.web` from the repository root, then open http://127.0.0.1:8765 to use the built dashboard.

## Commands

On this Mac, prefix npm commands with:

```sh
PATH=/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/bin:/bin npm install --cache .npm-cache --registry=https://registry.npmjs.org
PATH=/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/bin:/bin npm run dev
```

Run from `ui`. Vite binds to loopback and proxies `/api` to the backend. `npm run build` writes production assets to `ui/dist`; `npm test` runs utility regression tests and a mocked React workflow integration test using the test-only `jsdom` dependency. No backend or paid provider calls are required.

Production assets are served by the backend from `ui/dist`. Rebuild after frontend changes before restarting the backend. `vite preview` previews assets only; it does not configure an API proxy.

## Behavior and assumptions

- Hash routes (`#/new`, `#/audit/{id}`, `#/example/{id}`) support browser Back/Forward and direct links without server-side route rewrites.
- Audit history refreshes every five seconds; the selected live job refreshes every 2.5 seconds until completed or failed. Requests are aborted and timers cleared on navigation/unmount. Failed polling retries without restarting the audit.
- Resetting the form does not cancel or delete jobs. Active-job links and backend history remain available. There is no cancellation endpoint in the contract.
- Single mode plans one call; context mode plans two; loop mode plans one context call plus one call per round. Provider retries may add calls. Non-loop requests send `rounds: 1`. Consent resets when source, strategy, model, JSON mode, reasoning effort, or backend model/provider/key configuration changes, and after submission.
- Uploaded files stay in browser memory until sent in the audit request. Folder selection includes only `.sol` files and preserves relative paths. File picker selections replace the previous selection. Total Unicode code-point count is checked against `max_chars`; the backend remains authoritative for any prompt overhead or additional limits.
- GitHub source accepts public repository root URLs (`https://github.com/owner/repo`), an optional ref (default branch when empty), and an optional repository subdirectory (for example `contracts/src`). **Preview repository** posts `{url, ref, subdirectory}` to `/api/github/preview`, fetching source over the network without model calls or a provider key. The preview request allows 75 seconds for a fetch that may take 60 seconds. It shows a pinned commit link, manifest, and expandable plain-text source with line numbers. Any URL/ref/subdirectory edit or source switch invalidates the preview, aborts pending work, and clears paid consent; reset and unmount also cancel pending previews. Stale responses cannot restore an old snapshot.
- A GitHub live audit requires a valid preview and fresh paid consent, and sends `target: 'github'`, `github_preview_id`, and `files: []` to `/api/audits`. The backend audits the pinned snapshot. Reports show returned `source` repository, commit, ref, and subdirectory metadata, also included in report JSON export. All audit requests also send the resolved `model`, `json_mode`, and `reasoning_effort` settings. There are no GitHub token, credentials, or server-path inputs.
- Model ID is a searchable text input with optional datalist suggestions. Blank uses `/api/config`'s `model` default; manual values are trimmed. Missing/invalid resolved IDs block consent and submission. **Load models** explicitly fetches `GET /api/models` (`{models: [{id: string}]}`) through the bounded request helper; there are no automatic model-list/provider calls. Loading can contact the configured provider, but does not request an audit. Errors hide raw provider details, and manual entry remains usable. Reset/unmount abort pending loads and stale responses are ignored.
- JSON mode defaults to enabled; disabling it does not bypass backend parsing/validation of finding responses. Reasoning effort defaults to `null` (provider default), with `low`, `medium`, and `high` options. All context and finding rounds use the same model. Capabilities, pricing, JSON/reasoning support, and context limits vary by provider/model; the UI does not request automatic paid compatibility retries. Reset restores a blank model override, JSON mode enabled, and provider-default reasoning. Settings are disabled during submission.
- The browser never reads, accepts, or stores provider credentials. `key_configured` gates live scans; saved examples remain accessible without a key. Live source and generated context may leave the Mac through the backend's configured AI provider.
- POST requests set `X-Lucid-Request: local-ui`. Requests are not automatically retried; after an ambiguous submission failure, inspect history before authorizing another paid scan.
- List endpoints are assumed to return arrays. Saved examples are rendered directly from their original JSON, with `_meta`, optional context, and findings, and are never shown as live completed jobs.
- Findings and protocol context are rendered as plain text, not injected HTML. AI findings require human verification; an empty report is not proof of security.

No external fonts, analytics, icon package, or UI library are used. Icons are inline SVG. Layout adapts to mobile, uses native accessible inputs and disclosure elements, includes keyboard focus styles and a skip link, and respects reduced-motion preferences.
