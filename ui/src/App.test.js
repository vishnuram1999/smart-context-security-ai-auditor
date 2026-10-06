import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { transformWithEsbuild } from 'vite';

const source = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');
const transformed = await transformWithEsbuild(source, 'App.jsx', { jsx: 'automatic', loader: 'jsx' });
const code = transformed.code.replace(/from "([^"]+)"|from '([^']+)'/g, (_match, double, single) => {
  const specifier = double || single;
  return `from ${JSON.stringify(specifier.startsWith('.') ? new URL(specifier, import.meta.url).href : import.meta.resolve(specifier))}`;
});
const { default: App } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('GitHub previews are keyless, race-safe, reviewable, and require fresh consent for pinned audits', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  let keyConfigured = false;
  const snapshot = (id = 'preview-1') => {
    const content = 'contract Vault {}\n// <script>alert(1)</script>';
    return { id, repository_url: 'https://github.com/owner/repo', repository: 'owner/repo', ref: 'main', commit: 'a'.repeat(40), subdirectory: 'contracts/src', file_count: 1, total_chars: content.length, files: [{ path: 'contracts/src/Vault.sol', content }] };
  };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: 'test-model', provider: 'test-provider', key_configured: keyConfigured, max_chars: 1000 });
    if (path === '/api/examples' || (path === '/api/audits' && options.method !== 'POST')) return Response.json([]);
    if (path === '/api/github/preview') return new Promise((resolve, reject) => pending.push({ resolve, reject, options }));
    if (path === '/api/audits' || path === '/api/audits/github-job') return Response.json({ id: 'github-job', status: 'completed', target: 'github', mode: 'context', findings: [], source: snapshot() });
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  // React's onChange event plugin is initialized before this DOM exists. Its native
  // value tracker can dispatch controlled edits through the host input props in Node.
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const preview = async () => flush(() => button('Preview repository').click());
  const consent = () => document.getElementById('paid-consent');
  const auditPosts = () => calls.filter((call) => call.path === '/api/audits' && call.options.method === 'POST');
  try {
    await flush(() => root.render(React.createElement(App)));
    await flush(() => document.querySelector('input[value="github"]').click());
    await edit('github-url', 'https://github.com/owner/repo/tree/main');
    await preview();
    assert.match(document.body.textContent, /root URL/);
    assert.equal(pending.length, 0);
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    assert.equal(pending.length, 1, 'preview needs no model key');
    assert.equal(button('Previewing repository').disabled, true);
    assert.deepEqual(JSON.parse(pending[0].options.body), { url: 'https://github.com/owner/repo', ref: '', subdirectory: '' });
    assert.equal(pending[0].options.headers['X-Lucid-Request'], 'local-ui');
    await edit('github-ref', 'main');
    assert.equal(pending[0].options.signal.aborted, true);
    await preview();
    await flush(() => pending[1].resolve(Response.json(snapshot('new-preview'))));
    await flush(() => pending[0].resolve(Response.json(snapshot('stale-preview'))));
    assert.ok(document.querySelector('.github-preview'));
    assert.equal(auditPosts().length, 0);
    assert.equal(consent().disabled, true);
    assert.equal(button('Start live audit').disabled, true);
    assert.equal(document.querySelector('.repository-meta a[href$="/commit/' + 'a'.repeat(40) + '"]').textContent, 'a'.repeat(40));
    await flush(() => document.querySelector('.source-review summary').click());
    assert.equal(document.querySelector('.source-review details').open, true);
    assert.equal(document.querySelectorAll('.line-number').length, 2);
    assert.match(document.querySelector('.source-review pre').textContent, /<script>alert\(1\)<\/script>/);
    assert.equal(document.querySelector('.source-review script'), null);

    keyConfigured = true;
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    assert.equal(button('Start live audit').disabled, true, 'preview is not paid consent');
    await flush(() => consent().click());
    assert.equal(button('Start live audit').disabled, false);
    for (const [id, value] of [['github-url', 'https://github.com/owner/other'], ['github-ref', 'v2'], ['github-subdirectory', 'contracts/src']]) {
      await edit(id, value);
      assert.equal(consent().checked, false, `${id} resets consent`);
      assert.equal(document.querySelector('.github-preview'), null);
      assert.equal(button('Start live audit').disabled, true);
      await preview();
      await flush(() => pending.at(-1).resolve(Response.json(snapshot())));
      await flush(() => consent().click());
    }
    await preview();
    assert.equal(consent().checked, false, 're-preview resets consent');
    await flush(() => pending.at(-1).resolve(Response.json({ detail: 'Repository not found' }, { status: 404 })));
    assert.match(document.body.textContent, /Repository not found/);
    assert.equal(document.querySelector('.github-preview'), null);
    assert.equal(button('Start live audit').disabled, true);
    await preview();
    await flush(() => pending.at(-1).reject(new TypeError('Network unavailable')));
    assert.match(document.body.textContent, /Cannot reach the local API/);
    assert.equal(button('Preview repository').disabled, false);
    await preview();
    await flush(() => pending.at(-1).resolve(Response.json({ ...snapshot(), commit: 'main' })));
    assert.match(document.body.textContent, /invalid repository preview/);
    await preview();
    await flush(() => pending.at(-1).resolve(Response.json(snapshot('paid-preview'))));
    await flush(() => consent().click());
    await flush(() => button('Start live audit').click());
    assert.deepEqual(JSON.parse(auditPosts()[0].options.body), { mode: 'context', rounds: 1, model: 'test-model', json_mode: true, reasoning_effort: null, confirmed_paid: true, target: 'github', github_preview_id: 'paid-preview', files: [] });
    assert.equal(auditPosts().length, 1);
    assert.equal(window.location.hash, '#/audit/github-job');
    assert.match(document.body.textContent, /owner\/repo/);
    assert.match(document.body.textContent, /contracts\/src/);
    assert.ok(document.querySelector('.repository-meta a[href$="/commit/' + 'a'.repeat(40) + '"]'));

    await flush(() => button('New audit').click());
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.equal(document.getElementById('github-url').value, '');
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    const resetRequest = pending.at(-1);
    await flush(() => button('New audit').click());
    assert.equal(resetRequest.options.signal.aborted, true);
    await flush(() => resetRequest.resolve(Response.json(snapshot('reset-stale'))));
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.equal(document.querySelector('.github-preview'), null);
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    const switchedRequest = pending.at(-1);
    await flush(() => document.querySelector('input[value="bundled"]').click());
    assert.equal(switchedRequest.options.signal.aborted, true);
    await flush(() => switchedRequest.reject(new Error('stale failure')));
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.doesNotMatch(document.body.textContent, /stale failure/);
    await preview();
    const unmountedRequest = pending.at(-1);
    await act(async () => root.unmount());
    assert.equal(unmountedRequest.options.signal.aborted, true);
    await flush(() => unmountedRequest.resolve(Response.json(snapshot('unmounted-stale'))));
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('model settings load explicitly, preserve manual entry, gate consent, and ignore cancelled loads', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  let defaultModel = 'backend-model';
  let resolveAudit;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: defaultModel, provider: 'test-provider', key_configured: true });
    if (path === '/api/models') return new Promise((resolve, reject) => pending.push({ resolve, reject, options }));
    if (path === '/api/examples' || (path === '/api/audits' && options.method === 'GET')) return Response.json([]);
    if (path === '/api/audits' && options.method === 'POST') return new Promise((resolve) => { resolveAudit = resolve; });
    if (path === '/api/audits/model-job') return Response.json({ id: 'model-job', status: 'completed', findings: [] });
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const consent = () => document.getElementById('paid-consent');
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const load = async () => flush(() => button('Load models').click());
  const options = () => [...document.querySelectorAll('#available-models option')].map((option) => option.value);
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(pending.length, 0, 'mount does not load models');
    assert.match(document.querySelector('.config-strip').textContent, /backend-model/);
    assert.match(document.querySelector('.confirmation').textContent, /backend-model/);
    assert.equal(document.getElementById('json-mode').value, 'true');
    assert.equal(document.getElementById('reasoning-effort').value, '');
    await flush(() => consent().click());
    assert.equal(button('Start live audit').disabled, false);
    for (const [id, value] of [['audit-model', '  manual/model:version  '], ['json-mode', 'false'], ['reasoning-effort', 'high']]) {
      await edit(id, value);
      assert.equal(consent().checked, false, `${id} resets consent`);
      await flush(() => consent().click());
    }
    assert.match(document.querySelector('.config-strip').textContent, /manual\/model:version/);
    assert.match(document.querySelector('.confirmation').textContent, /manual\/model:version/);
    await edit('audit-model', 'bad model');
    assert.equal(consent().disabled, true);
    assert.equal(button('Start live audit').disabled, true);
    assert.equal(document.getElementById('audit-model').getAttribute('aria-invalid'), 'true');
    await edit('audit-model', '   ');
    assert.match(document.querySelector('.config-strip').textContent, /backend-model/);
    assert.equal(consent().disabled, false);
    await flush(() => consent().click());
    defaultModel = '';
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    assert.equal(consent().checked, false, 'default configuration changes reset consent');
    assert.equal(consent().disabled, true, 'blank resolved model is blocked');
    await edit('audit-model', 'manual-model');
    await load();
    assert.equal(pending.length, 1);
    assert.equal(button('Loading models').disabled, true);
    assert.equal(document.getElementById('audit-model').disabled, false, 'manual input works while loading');
    await edit('audit-model', '  manual/model:version  ');
    await flush(() => pending[0].resolve(Response.json({ models: [{ id: 'z-model' }, { id: 'a-model' }, { id: 'z-model' }] })));
    assert.deepEqual(options(), ['a-model', 'z-model']);
    assert.equal(document.getElementById('audit-model').value, '  manual/model:version  ', 'loading never changes selected model');
    await flush(() => consent().click());
    await load();
    await flush(() => pending[1].resolve(Response.json({ detail: 'Provider secret sk-never-display https://credentials.example' }, { status: 401 })));
    assert.match(document.body.textContent, /API 401.*details are hidden/);
    assert.doesNotMatch(document.body.textContent, /sk-never-display|credentials\.example/);
    assert.equal(document.getElementById('audit-model').disabled, false);
    assert.equal(consent().checked, true, 'loading suggestions does not change paid settings');
    await load();
    await flush(() => pending[2].resolve(Response.json({ models: [{ id: null }] })));
    assert.match(document.body.textContent, /invalid model list/);
    assert.deepEqual(options(), ['a-model', 'z-model']);
    await load();
    await flush(() => pending[3].reject(new Error('network secret sk-never-display')));
    assert.match(document.body.textContent, /Cannot reach the local API/);
    assert.doesNotMatch(document.body.textContent, /sk-never-display/);
    assert.equal(button('Start live audit').disabled, false, 'failed load does not prevent manual model audit');
    await flush(() => button('Start live audit').click());
    const post = calls.find((call) => call.path === '/api/audits' && call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), { mode: 'context', rounds: 1, model: 'manual/model:version', json_mode: false, reasoning_effort: 'high', confirmed_paid: true, target: 'bundled', files: [] });
    for (const id of ['audit-model', 'json-mode', 'reasoning-effort', 'paid-consent']) assert.equal(document.getElementById(id).matches(':disabled'), true, `${id} locked while submitting`);
    assert.equal(button('Load models').matches(':disabled'), true);
    assert.equal(button('New audit').disabled, true);
    await flush(() => resolveAudit(Response.json({ id: 'model-job', status: 'completed', findings: [] })));
    assert.equal(calls.filter((call) => call.path === '/api/audits' && call.options.method === 'POST').length, 1, 'no paid compatibility retry');
    await flush(() => button('New audit').click());
    assert.equal(document.getElementById('audit-model').value, '');
    assert.equal(document.getElementById('json-mode').value, 'true');
    assert.equal(document.getElementById('reasoning-effort').value, '');
    assert.equal(consent().checked, false);
    assert.equal(pending.length, 4, 'reset and config refresh do not load models');
    await load();
    const stale = pending.at(-1);
    await flush(() => button('New audit').click());
    assert.equal(stale.options.signal.aborted, true);
    await load();
    const fresh = pending.at(-1);
    await flush(() => fresh.resolve(Response.json({ models: [{ id: 'fresh-model' }] })));
    await flush(() => stale.resolve(Response.json({ models: [{ id: 'stale-model' }] })));
    assert.deepEqual(options(), ['fresh-model']);
    await load();
    const staleFailure = pending.at(-1);
    await flush(() => button('New audit').click());
    await flush(() => staleFailure.reject(new Error('stale failure')));
    assert.equal(document.querySelector('.model-panel [role="alert"]')?.textContent.includes('Cannot reach'), false);
    await load();
    const unmounted = pending.at(-1);
    await act(async () => root.unmount());
    assert.equal(unmounted.options.signal.aborted, true);
    await flush(() => unmounted.resolve(Response.json({ models: [{ id: 'unmounted-model' }] })));
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

// A finite DOM integration test: no servers, provider calls, or browser binaries.
test('rendered workflow gates paid scans, loads examples, uploads, submits, and preserves history after reset', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  let keyConfigured = false;
  let job = null;
  let failNextPoll = false;
  const finding = { title: 'Unchecked callback', severity: 'high', location: 'Vault.sol:12', description: 'External call before accounting.', impact: 'Funds can be drained.', exploit_steps: ['Deploy receiver.', 'Reenter withdraw.'] };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: 'test-model', provider: 'test-provider', key_configured: keyConfigured, default_target: 'target/src', default_file_count: 12, max_chars: 1000 });
    if (path === '/api/examples') return Response.json([{ id: 'saved-1', title: 'Context sample', findings_count: 1 }]);
    if (path === '/api/examples/saved-1') return Response.json({ _meta: { model: 'example-model' }, findings: [finding], context: 'Protocol actors and invariants' });
    if (path === '/api/audits' && options.method === 'POST') {
      job = { id: 'job-1', status: 'running', stage: 'finding_bugs', mode: 'loop', rounds: 3, completed_rounds: 1, findings: [finding], context: 'Live protocol context', error: null, warnings: ['Verify every finding'], created_at: '2026-10-04T10:00:00Z', model: 'test-model', target: 'upload' };
      return Response.json({ ...job, status: 'queued' });
    }
    if (path === '/api/audits') return Response.json(job ? [job] : []);
    if (path === '/api/audits/job-1') {
      if (failNextPoll) { failNextPoll = false; return Response.json({ detail: 'Provider temporarily unavailable' }, { status: 503 }); }
      return Response.json(job);
    }
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => { await act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); }); };
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const navigate = async (hash) => flush(() => { window.location.hash = hash; window.dispatchEvent(new dom.window.HashChangeEvent('hashchange')); });
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(button('Start live audit').disabled, true);
    assert.match(document.body.textContent, /Live audits are locked/);
    await navigate('#/example/saved-1');
    assert.match(document.body.textContent, /Saved example · Not a live scan/);
    assert.match(document.body.textContent, /Unchecked callback/);
    assert.match(document.body.textContent, /Protocol context/);
    assert.equal(calls.some((call) => call.options.method === 'POST'), false);

    keyConfigured = true;
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    await flush(() => button('New audit').click());
    await flush(() => document.querySelector('input[value="upload"]').click());
    const input = document.querySelector('input[type="file"][accept=".sol"]');
    Object.defineProperty(input, 'files', { configurable: true, value: [{ name: 'Vault.sol', size: 17, text: async () => 'contract Vault {}' }] });
    await flush(() => input.dispatchEvent(new dom.window.Event('change', { bubbles: true })));
    assert.match(document.body.textContent, /1 files selected/);
    const checkbox = document.getElementById('paid-consent');
    await flush(() => checkbox.click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => document.querySelector('input[value="loop"]').click());
    assert.equal(checkbox.checked, false, 'changing strategy resets paid consent');
    assert.match(document.body.textContent, /4 paid model calls planned/);
    await flush(() => checkbox.click());
    await flush(() => button('Start live audit').click());
    const post = calls.find((call) => call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), { mode: 'loop', rounds: 3, model: 'test-model', json_mode: true, reasoning_effort: null, confirmed_paid: true, target: 'upload', files: [{ path: 'Vault.sol', content: 'contract Vault {}' }] });
    assert.equal(post.options.headers['X-Lucid-Request'], 'local-ui');
    assert.equal(window.location.hash, '#/audit/job-1');
    assert.match(document.body.textContent, /1 \/ 3 rounds/);
    assert.match(document.body.textContent, /Backend warnings/);
    assert.match(document.body.textContent, /Unchecked callback/);
    assert.equal(document.querySelector('progress').value, 1);
    await flush(() => document.querySelector('.skip-link').click());
    assert.equal(window.location.hash, '#/audit/job-1', 'skip link does not change the current route');
    assert.equal(document.activeElement.id, 'main-content');

    await flush(() => button('New audit').click());
    assert.equal(window.location.hash, '#/new');
    assert.equal(document.querySelector('input[type="checkbox"]').checked, false);
    assert.ok(document.querySelector('a[href="#/audit/job-1"]'), 'active job remains reachable after reset');
    failNextPoll = true;
    await navigate('#/audit/job-1');
    assert.match(document.body.textContent, /API 503.*backend logs/);
    job = { ...job, status: 'completed', completed_rounds: 3 };
    await flush(() => button('Retry').click());
    assert.match(document.body.textContent, /Ready for review/);
    assert.equal(document.querySelector('progress'), null);
    assert.equal(document.querySelector('[role="alert"]'), null);
    assert.ok(document.querySelector('.finding-card > summary'));
    assert.ok(document.querySelector('.context-panel > summary'));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
