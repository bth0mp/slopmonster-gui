// The page checks that test_page.py runs: the real page in a headless Edge or Chrome, driven
// over the browser's debugging port, against the real server with a stand-in model.
//   node test_page.mjs <page url> <debugging port> <temp folder> <app folder> [flow ...]
// Prints one line per failed check and exits 1 when any failed.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const [url, debug, tmp, app] = process.argv.slice(2);
const W = 1180, H = 820;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let target = null;
for (let i = 0; i < 100 && !target; i++) {
  await sleep(150);
  try { target = (await (await fetch(`http://127.0.0.1:${debug}/json/list`)).json()).find((t) => t.type === 'page'); } catch {}
}
if (!target) { console.log(`  · the browser did not answer on port ${debug}`); process.exit(1); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
let id = 0;
const waiting = new Map(), errors = [], away = [], dialogs = [], hooks = new Set();
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  hooks.forEach((hook) => hook(m));
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
  if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === 'Page.frameNavigated' && !m.params.frame.parentId && !m.params.frame.url.startsWith(url)) away.push(m.params.frame.url);
  if (m.method === 'Page.javascriptDialogOpening') { dialogs.push(m.params.message); send('Page.handleJavaScriptDialog', { accept: false }); }
});
const send = (method, params = {}) => new Promise((r) => {
  const n = ++id;
  waiting.set(n, r);
  ws.send(JSON.stringify({ id: n, method, params }));
  setTimeout(() => { if (waiting.delete(n)) r({ timeout: true }); }, 10000);
});
const js = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
  return r.result?.result?.value;
};

let failed = 0, checks = 0;
const check = (name, ok, detail) => {
  checks++;
  if (!ok) { failed++; console.log(`  · ${name}: ${JSON.stringify(detail)}`); }
};
const waitFor = async (expr, ms = 8000) => {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) if (await js(expr)) return true;
  return false;
};

// The stand-ins in test_page.py read control.json on each status and model call.
const control = (c = {}) => writeFileSync(path.join(tmp, 'control.json'), JSON.stringify(Object.assign({ claude: 'ready', chatgpt: 'chatgpt', delay: 1, fail: '' }, c)));
const callsFile = path.join(tmp, 'calls.jsonl');
const calls = () => existsSync(callsFile) ? readFileSync(callsFile, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : [];

// No real clipboard: Copy, Paste and the save dialog answer from inside the page.
await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
  window.__copied = [];
  const board = { writeText: async (t) => { window.__copied.push(String(t)); }, readText: async () => '', write: async () => {}, read: async () => [] };
  Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => board, configurable: true });
  const exec = Document.prototype.execCommand;
  Document.prototype.execCommand = function (name, ...rest) { return /^(copy|cut|paste)$/i.test(name) ? false : exec.call(this, name, ...rest); };
  window.showSaveFilePicker = undefined;
})()` });
await send('Runtime.enable');
await send('Page.enable');
await send('Page.setDownloadBehavior', { behavior: 'deny' });
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

const SLOP = "Are you ready to unlock seamless scheduling? Our game-changing tool empowers clinic managers — whether you're a solo practice or a hospital group — to streamline every room, every clinician, and every shift. It's not just a calendar, it's a journey.\n\nMore than 4,000 customers use it today. Setup takes about a day, and your existing bookings come across in the import.";
const FIXED = 'Our scheduling tool gives clinic managers one calendar for every room and every clinician.';
const PLAIN = 'The tool reads a file and reports what it finds. '.repeat(20).trim();
const EDIT = '\nCUSTOM-PROMPT-MARK: keep it short.';

// A fresh profile: no settings kept, `items` put in localStorage, the stand-ins set by `c`.
const fresh = async (c = {}, items = {}) => {
  control(c);
  await send('Page.navigate', { url });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.clear(); Object.entries(${JSON.stringify(items)}).forEach(([k, v]) => localStorage.setItem(k, v))`);
  await send('Page.navigate', { url });
  await waitFor(`document.readyState === 'complete' && document.getElementById('engine').dataset.state !== 'checking'`);
};
const facts = async () => JSON.parse(await js(`JSON.stringify({ view: document.documentElement.dataset.run,
  open: document.getElementById('settings').open, focus: document.activeElement && (document.activeElement.id || document.activeElement.tagName),
  main: document.getElementById('input').value, prompt: document.getElementById('prompt').value,
  state: document.getElementById('prompt-state').textContent, engine: (document.querySelector('input[name=engine]:checked') || {}).value,
  pill: document.getElementById('engine-text').textContent, stored: localStorage.getItem('slop-engine'),
  hint: document.getElementById('in-hint').textContent, idle: document.getElementById('idle-title').textContent,
  title: document.title })`));
const paste = (t) => js(`(() => { document.activeElement && document.activeElement.blur(); const dt = new DataTransfer(); dt.setData('text/plain', ${JSON.stringify(t)});
  document.body.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); return true })()`);
const drop = (sel, name, body, outside) => js(`(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  const bin = atob(${JSON.stringify(Buffer.from(body).toString('base64'))}), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const dt = new DataTransfer(); dt.items.add(new File([bytes], ${JSON.stringify(name)}));
  const r = el.getBoundingClientRect();
  const at = ${outside ? '{ clientX: 5, clientY: 5 }' : '{ clientX: r.left + 20, clientY: r.top + 20 }'};
  const fire = (type) => { const ev = new DragEvent(type, Object.assign({ bubbles: true, cancelable: true, composed: true, dataTransfer: dt }, at)); el.dispatchEvent(ev); return ev.defaultPrevented; };
  const res = { enter: fire('dragenter'), over: fire('dragover') };
  res.overlay = document.documentElement.dataset.drop || '';
  res.drop = fire('drop');
  return JSON.stringify(res);
})()`).then(JSON.parse);
const click = (sel) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e || e.disabled || e.hidden) return false; e.click(); return true })()`);
const focusOn = (sel) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); e.focus(); return document.activeElement === e })()`);
const openSettings = async () => { await click('#settings-open'); await waitFor(`document.getElementById('settings').open`); await sleep(300); };
const editPrompt = (t) => js(`(() => { const p = document.getElementById('prompt'); p.focus(); p.value = p.value + ${JSON.stringify(t)}; p.dispatchEvent(new Event('input', { bubbles: true })); p.setSelectionRange(p.value.length, p.value.length); return true })()`);
const typeMain = (t) => js(`(() => { const t = document.getElementById('input'); t.focus(); t.value = ${JSON.stringify(t)}; t.dispatchEvent(new Event('input', { bubbles: true })); return true })()`);
const key = async (k, code, vk, text, modifiers = 0) => {
  await send('Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', key: k, code, windowsVirtualKeyCode: vk, text, modifiers });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk, modifiers });
  await sleep(300);
};
const esc = () => key('Escape', 'Escape', 27);
const tab = () => key('Tab', 'Tab', 9);
const enter = () => key('Enter', 'Enter', 13, '\r');
const undo = async () => { await js(`document.activeElement && document.activeElement.blur()`); await key('z', 'KeyZ', 90, undefined, 2); };
const mouse = (type, x, y, buttons) => send('Input.dispatchMouseEvent', { type, x, y, button: type === 'mouseMoved' ? (buttons ? 'left' : 'none') : 'left', buttons, clickCount: type === 'mouseMoved' ? 0 : 1 });
const mdrag = async (from, to, steps = 10) => {
  await mouse('mouseMoved', from[0], from[1], 0);
  await mouse('mousePressed', from[0], from[1], 1);
  for (let i = 1; i <= steps; i++) await mouse('mouseMoved', from[0] + (to[0] - from[0]) * i / steps, from[1] + (to[1] - from[1]) * i / steps, 1);
  await mouse('mouseReleased', to[0], to[1], 0);
  await sleep(300);
};
const rect = (sel) => js(`(() => { const r = document.querySelector(${JSON.stringify(sel)}).getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom } })()`);
const done = () => waitFor(`document.documentElement.dataset.run === 'done'`, 20000);

// ── runs that start by themselves are held in three cases (README: "held")
async function holds() {
  for (const [what, text, said] of [
    ['a four-word paste', 'Fix this now please', /only 4 words, so it was not sent/],
    ['a text of more than 9,000 characters', Array(10).fill(PLAIN).join('\n\n'), /^This is a long text\. It will be rewritten in about 2 parts/],
    ['a paste with a password in it', 'Here is the config for the staging box: password = hunter2hunter2 and the rest of the notes follow below.', /looks like it holds a key or a password, so it was not sent/],
    ['a password written in words', 'Hi Sam, the staging login is admin and the password is Tr0ub4dor&3xyz, please rotate it after the demo tomorrow.', /looks like it holds a key or a password, so it was not sent/],
  ]) {
    await fresh();
    const before = calls().length;
    await paste(text);
    await sleep(1500);
    const f = await facts();
    check(`hold: ${what} is not sent, and the hint says why`, calls().length === before && f.view === 'idle' && said.test(f.hint), f.hint);
  }
  await fresh();
  const before = calls().length;
  await paste(SLOP);
  check('hold control: a sloppy paste runs by itself', await done() && calls().length > before, await facts());
}

// ── a fresh profile picks ChatGPT only when it is signed in to a ChatGPT plan
async function firstEngine() {
  for (const [how, want] of [['chatgpt', 'chatgpt'], ['api_key', 'claude'], ['unknown', 'claude']]) {
    await fresh({ chatgpt: how });
    const f = await facts();
    check(`with Codex signed in by ${how}, a fresh profile picks ${want}`,
      f.engine === want && f.pill.startsWith(want === 'chatgpt' ? 'ChatGPT' : 'Claude') && f.stored === null, f);
    const before = calls().length;
    await paste(SLOP);
    await done();
    const made = calls().slice(before);
    check(`... and the automatic run goes to ${want}`, made.length > 0 && made.every((c) => c.engine === want), made);
  }
  await openSettings();
  await click('input[name=engine][value=chatgpt]');
  await sleep(300);
  const g = await facts();
  check('picking ChatGPT in Settings is kept', g.engine === 'chatgpt' && g.stored === 'chatgpt', g);
}

// ── Settings: drops and Ctrl+Alt+U while it is open, drags, Save and Reset from the
// keyboard, and an unsaved edit
async function settingsPanel() {
  await fresh();
  await openSettings(); await editPrompt(EDIT);
  let c0 = calls().length;
  let before = await facts();
  let d = await drop('#prompt', 'my-instructions.txt', SLOP);
  await sleep(1500);
  let f = await facts();
  check('a file dropped on the instructions is not opened, and the window stays', d.over && d.drop && d.overlay === '' && !away.length, d);
  check('... the box and the instructions are unchanged, and nothing runs',
    f.main === '' && f.prompt === before.prompt && f.view === before.view && calls().length === c0, f);
  await fresh(); await typeMain('The tool reads a file and reports what it finds. It leaves the file unchanged.'); await sleep(800);
  await openSettings();
  c0 = calls().length;
  d = await drop('#settings', 'notes.txt', SLOP, true);
  await sleep(1500);
  f = await facts();
  check('a file dropped on the backdrop leaves the draft, and nothing runs', f.main.startsWith('The tool reads') && calls().length === c0 && d.drop, f);
  await fresh();
  c0 = calls().length;
  await drop('#input', 'draft.txt', SLOP);
  check('control: with Settings closed a dropped file is opened and run', await done() && (await facts()).main.startsWith('Are you ready') && calls().length > c0);

  await fresh();
  await openSettings(); await editPrompt(EDIT);
  before = await facts();
  writeFileSync(path.join(tmp, 'hotkey.txt'), SLOP);
  await waitFor(`document.getElementById('input').value.startsWith('Are you ready')`);
  f = await facts();
  check('hotkey with Settings open: the text goes to the main box, not into the instructions', f.main.startsWith('Are you ready') && f.prompt === before.prompt && !f.open, f);
  await done();
  await openSettings();
  check('hotkey with Settings open: the unsaved edit is still there after', (await facts()).prompt.endsWith(EDIT));
  await click('#settings-close'); await click('#settings-close');

  await fresh();
  await openSettings();
  const pr = await rect('#prompt');
  await mdrag([pr.left + 40, pr.top + 40], [5, pr.top + 40]);
  check('a drag-select from the instructions that ends on the backdrop keeps Settings open', (await facts()).open);
  await mdrag([5, 300], [5, 300], 1);
  check('control: a click on the backdrop closes it', !(await facts()).open);

  await fresh();
  await openSettings(); await editPrompt(EDIT);
  await focusOn('#prompt-save'); await enter();
  f = await facts();
  check('Enter on Save saves, and the focus stays on the instructions', f.state.startsWith('Saved') && f.focus === 'prompt', f);
  await tab();
  check('... and the next Tab goes to Reset', (await facts()).focus === 'prompt-reset');
  c0 = calls().length;
  await esc(); await paste(SLOP); await done();
  check('a run after the save uses the saved instructions', calls().slice(c0).some((c) => c.custom), calls().slice(c0));
  await fresh();
  await openSettings();
  await focusOn('#prompt-reset'); await enter();
  f = await facts();
  check('Enter on Reset goes back to the default, focus on the instructions', f.state.startsWith('Back to the default') && f.focus === 'prompt', f);

  await fresh();
  await openSettings(); await editPrompt(EDIT);
  await click('#settings-close'); await sleep(200);
  f = await facts();
  check('Close with an unsaved edit keeps the panel open and says why', f.open && /not saved/.test(f.state) && f.focus === 'prompt-save', f);
  await click('#settings-close'); await sleep(200);
  f = await facts();
  check('... a second Close closes it, and the edit stays in the box', !f.open && f.prompt.endsWith(EDIT), f);
  await openSettings();
  await focusOn('#prompt'); await key('x', 'KeyX', 88, 'x');   // Chrome lets a page stop Esc only after a key
  await esc();
  f = await facts();
  check('Esc with an unsaved edit keeps it open once', f.open && /not saved/.test(f.state), f);
  await esc();
  check('... and closes it on the second Esc', !(await facts()).open);
  await fresh();
  await openSettings(); await click('#settings-close'); await sleep(200);
  check('control: with no edit Close closes it at once', !(await facts()).open);
}

// ── a run to the end: title verdicts, focus on Copy, Keep original, the history list
async function results() {
  await fresh();
  await paste(SLOP);
  await done();
  let f = await facts();
  // 4/5: the rewrite keeps "4,000 customers", which only the author can confirm.
  check('done: the window title says it passes, with the score', f.title === 'Passes 4/5 - SlopMonster', f.title);
  check('done: the focus is on Copy', f.focus === 'copy', f.focus);
  await click('#copy');
  let copied = await js(`window.__copied.at(-1)`);
  check('Copy copies the rewrite', copied && copied.startsWith(FIXED), copied);

  // Two runs of one text in the same minute must still be told apart (PAGE-8).
  await paste(SLOP);
  await done();
  const labels = await js(`Array.from(document.querySelectorAll('#history option')).filter((o) => !o.hidden).map((o) => o.textContent)`);
  check('history: two runs of one text have different labels', labels.length === 2 && new Set(labels).size === 2, labels);

  await click('input[name=view][value=changes]');
  await waitFor(`document.querySelector('.chg-btn')`);
  const label = () => js(`document.getElementById('view-changes').textContent`);
  const changed = await label();
  const unkept = await js(`document.querySelector('#history option:checked').textContent`);
  await click('.chg-btn');
  await waitFor(`!document.title.startsWith('Passes')`);
  await sleep(800);
  // The count on the switch is of the paragraphs that still differ (DAILY-9).
  const count = (s) => Number((/(\d+)$/.exec(s) || [])[1]);
  const now = await label();
  check('Keep original takes one off the Changes count', count(changed) >= 1 && count(now) === count(changed) - 1, [changed, now]);
  await click('input[name=view][value=result]');
  await click('input[name=view][value=changes]');
  check('... and the count stays so after a switch of view', await label() === now, [now, await label()]);
  await click('#copy');
  copied = await js(`window.__copied.at(-1)`);
  check('Keep original puts the paragraph back into what Copy copies', copied && copied.startsWith('Are you ready') && copied.includes('4,000 customers'), copied);
  // The list gives the score of the text Copy copies, once it is checked (FINAL-F1).
  await waitFor(`!document.getElementById('outcome').classList.contains('is-stale')`);
  const scoreIn = (s) => (/ ([0-5])\/5 /.exec(s || '') || [])[1];
  const listed = await js(`document.querySelector('#history option:checked').textContent`);
  const rechecked = await js(`fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json',
    'X-Slop-Token': document.querySelector('meta[name=slop-token]').content },
    body: JSON.stringify({ text: ${JSON.stringify(copied)} }) }).then((r) => r.json()).then((j) => j.score)`);
  check('... and the list gives the score of that text once it is checked', scoreIn(listed) === String(rechecked), [listed, rechecked]);
  await click('.chg-btn');
  await sleep(100);
  const back = await js(`document.querySelector('#history option:checked').textContent`);
  check('... and the score of the rewrite again when it is taken back', scoreIn(back) === scoreIn(unkept) && scoreIn(back) !== undefined, [back, unkept]);
  await click('.chg-btn');
  await sleep(100);
  const pending = await js(`document.querySelector('#history option:checked').textContent`);
  check('... and no score while the text with the paragraph kept is being checked', scoreIn(pending) === undefined, pending);
  await waitFor(`!document.getElementById('outcome').classList.contains('is-stale')`);
  await click('.chg-btn');
  await sleep(100);
  await click('input[name=view][value=result]');

  await fresh();
  await paste('Northwind serves 4,000 clinics. Setup takes 2 days. Details at https://northwind.example/setup and our seamless onboarding unlocks value.');
  await done();
  f = await facts();
  check('a rewrite that drops a number is titled Check before copying, and Copy does not take the focus',
    f.title === 'Check before copying - SlopMonster' && f.focus !== 'copy', f);
}

// ── Ctrl+Z brings back what a paste replaced, within its limits (README: "Ctrl+Z")
async function undoLimits() {
  await fresh({}, { 'slop-auto': 'off' });
  const first = 'The first text, pasted here, has a few plain words in it.';
  await paste(first); await sleep(200);
  await paste('The second text replaces it, and is also a few plain words.'); await sleep(200);
  await undo();
  check('Ctrl+Z outside the box brings back the text a paste replaced', (await facts()).main === first, (await facts()).main);
  const long = Array.from({ length: 450 }, (_, i) => 'Line ' + (i + 1) + ' of the long text.').join('\n');
  await paste(long); await sleep(200);
  await undo();
  const f = await facts();
  check('Ctrl+Z after a paste of over 400 lines keeps it, and says why', f.main === long && /cannot be brought back/.test(f.hint), f.hint);
}

// ── A window left open for days: the undo history keeps only the last swaps (SOAK2-1).
// Each text kept for Ctrl+Z holds DOM nodes that no garbage collection frees, so the count
// of nodes after a forced collection shows how many swaps are still kept.
async function undoMemory() {
  await send('Performance.enable');
  const nodes = async () => {
    await send('HeapProfiler.collectGarbage');
    await sleep(100);
    const r = await send('Performance.getMetrics');
    return (r.result?.metrics || []).find((m) => m.name === 'Nodes')?.value;
  };
  // 150 lines of about 60 characters: under the 20,000 that make a text large.
  const text = (n) => Array.from({ length: 150 }, (_, i) => `Swap ${n}, line ${i + 1}, says a few plain words here.`).join('\n');
  await fresh({}, { 'slop-auto': 'off' });
  await paste(text(0)); await sleep(100);
  const base = await nodes();
  await paste(text(1)); await sleep(100);
  const one = (await nodes()) - base;   // what one kept swap costs
  for (let n = 2; n <= 60; n++) await paste(text(n));
  await sleep(300);
  const many = (await nodes()) - base;
  // Kept without a bound, 60 swaps cost 60 times one. The bound is the last 20.
  check('60 swaps keep no more than about 20 in the undo history', one > 100 && many < one * 30, { base, one, many });
  await undo();
  check('... and Ctrl+Z still brings back the text the last one replaced', (await facts()).main === text(59), (await facts()).main.slice(0, 20));

  // Large texts: each swap of one empties the history first, so only the last is kept.
  const big = (n) => Array.from({ length: 300 }, (_, i) => `Large ${n}, line ${i + 1}, says a good many more plain words than that, to fill it out well.`).join('\n');
  await fresh({}, { 'slop-auto': 'off' });
  await paste(big(0)); await sleep(100);
  const start = await nodes();
  await paste(big(1)); await sleep(100);
  const each = (await nodes()) - start;
  for (let n = 2; n <= 12; n++) await paste(big(n));
  await sleep(300);
  const after = (await nodes()) - start;
  check('12 swaps of texts over 20,000 characters keep about one', each > 200 && after < each * 3, { start, each, after });
  await undo();
  check('... and Ctrl+Z still brings back the large text the last one replaced', (await facts()).main === big(11), (await facts()).main.slice(0, 20));
}

// ── Save as Word writes the rewrite into a copy of the opened file
async function saveWord() {
  await fresh({}, { 'slop-auto': 'off' });
  await drop('body', 'a_prose.docx', readFileSync(path.join(app, 'testdata', 'a_prose.docx')));
  await waitFor(`document.getElementById('input').value.includes('Harbour Renewal Programme')`);
  await click('#unslop');
  check('Word file: the run finishes', await done(), await facts());
  await js(`(() => { window.__written = null; window.showSaveFilePicker = async () => ({
    kind: 'file', name: 'saved.docx', isSameEntry: async () => false, getFile: async () => new File([], 'saved.docx'),
    createWritable: async () => { const parts = []; return { write: async (b) => { parts.push(b); }, close: async () => {
      const bytes = new Uint8Array(await new Blob(parts).arrayBuffer()); let s = '';
      for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      window.__written = btoa(s); } }; } }); return true })()`);
  await click('#save');
  const wrote = await waitFor(`window.__written !== null`);
  check('Save as Word writes a file', wrote, await facts());
  if (wrote) writeFileSync(path.join(tmp, 'saved.docx'), Buffer.from(await js(`window.__written`), 'base64'));
  // Pasted text has no table rows: its "a | b" lines are sentences, and the page does not ask for a table.
  await fresh({}, { 'slop-auto': 'off' });
  await paste('Pick one size: small | medium | large.\n\nPick one colour: red | green | blue.');
  await click('#unslop');
  check('pasted text: the run finishes', await done(), await facts());
  await js(`(() => { window.__written = null; window.__docx = null; const real = window.fetch;
    window.fetch = (url, init) => { if (String(url).includes('/api/docx')) window.__docx = init && init.body; return real(url, init); };
    window.showSaveFilePicker = async () => ({
    kind: 'file', name: 'pasted.docx', isSameEntry: async () => false, getFile: async () => new File([], 'pasted.docx'),
    createWritable: async () => ({ write: async () => {}, close: async () => { window.__written = true; } }) }); return true })()`);
  await click('#save');
  const sent = await waitFor(`window.__written !== null`) && await js(`window.__docx`);
  check('Save as Word from pasted text does not ask for table rows',
    typeof sent === 'string' && JSON.parse(sent).rows === false, sent);
}

// ── the focus after an error and after a sign-in (PAGE-3, PAGE-4), and the idle title (PAGE-6)
async function focusAndTitles() {
  await fresh({ delay: 2.5, fail: 'timeout' });
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'working'`);
  await focusOn('#cancel');
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  await sleep(300);
  check('a run that fails while Cancel has the focus leaves it on the error\'s button', (await facts()).focus === 'err-action', await facts());

  // Codex signed in with an API key, so a fresh profile picks Claude, which is signed out.
  await fresh({ claude: 'login', chatgpt: 'api_key' });
  check('signed out: the error says so', (await facts()).view === 'error');
  await focusOn('#err-action');
  await click('#err-action');
  await waitFor(`document.getElementById('err-title').textContent === 'Finish signing in'`);
  control({ chatgpt: 'api_key' });
  await waitFor(`document.documentElement.dataset.run === 'idle'`, 10000);
  let f = await facts();
  check('after the sign-in, the idle view says Claude is ready and the focus is on the box', f.idle === 'Claude is ready' && f.focus === 'input', f);
  await openSettings();
  await click('input[name=engine][value=chatgpt]');
  await sleep(300);
  await click('#settings-close');
  await sleep(300);
  f = await facts();
  check('after a switch to ChatGPT, the idle title names ChatGPT', f.idle === 'ChatGPT is ready' && f.pill.startsWith('ChatGPT'), f);

  await fresh({ claude: 'login', chatgpt: 'api_key' });
  await focusOn('#err-settings');
  await click('#err-settings');
  await waitFor(`document.getElementById('settings').open`);
  // From the keyboard, as a person does it: a scripted click() never lets the focus settle on
  // the radio button, and the check would pass on a page without the fix.
  await focusOn('input[name=engine][value=claude]');
  await key('ArrowRight', 'ArrowRight', 39);
  await focusOn('#settings-close');
  await enter();
  await sleep(300);
  f = await facts();
  check('closing Settings after a switch from the error puts the focus on the Settings button', !f.open && f.focus === 'settings-open', f);
}

// ── Thorough sends every part of a long text, and the progress note says so (Q4)
async function longTexts() {
  const long = ["It's not just a tool, it's a game-changing journey.", ...Array(10).fill(PLAIN)].join('\n\n');
  for (const [strength, note, sent] of [['light', 'Parts with nothing flagged are skipped.', (n) => n === 1],
    ['thorough', 'Thorough rewrites every part.', (n) => n === 2]]) {
    await fresh({ delay: 1.5 }, { 'slop-strength': strength });
    const before = calls().length;
    await paste(long);
    await sleep(500);
    await click('#unslop');
    await waitFor(`document.getElementById('work-title').textContent.startsWith('Part ')`);
    const said = await js(`document.getElementById('work-note').textContent`);
    await done();
    check(`a long text with ${strength}: the progress note says what is sent`, said === 'A long text takes a few minutes. ' + note, said);
    check(`a long text with ${strength}: the calls made`, sent(calls().length - before), calls().length - before);
  }
}

// ── how it looks: High Contrast (PAGE-1) and a long file name at a narrow width (PAGE-5)
async function looks() {
  await fresh();
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'forced-colors', value: 'active' }] });
  await sleep(300);
  const seen = JSON.parse(await js(`JSON.stringify((() => {
    const knob = (id) => getComputedStyle(document.getElementById(id).nextElementSibling, '::after').backgroundColor;
    const seg = (v) => getComputedStyle(document.querySelector('input[name=view][value=' + v + '] + span')).backgroundColor;
    return { on: document.getElementById('auto').checked, off: document.getElementById('autocopy').checked,
      knobOn: knob('auto'), knobOff: knob('autocopy'), segOn: seg('result'), segOff: seg('changes') };
  })())`));
  check('High Contrast: a switch that is on looks different from one that is off', seen.on && !seen.off && seen.knobOn !== seen.knobOff, seen);
  // Forced colours keep a fill's alpha: the light tint a chosen option had is all but invisible.
  check('High Contrast: the chosen option of a choice is filled', /^rgb\(/.test(seen.segOn) && seen.segOn !== seen.segOff, seen);
  await send('Emulation.setEmulatedMedia', { features: [] });

  await send('Emulation.setDeviceMetricsOverride', { width: 420, height: 800, deviceScaleFactor: 1, mobile: false });
  await fresh({}, { 'slop-auto': 'off' });
  await drop('body', 'quarterly_board_update_final_comments_resolved_v12_with_appendix_tables.txt', PLAIN);
  await waitFor(`document.getElementById('in-hint').textContent.includes('opened')`);
  const wide = JSON.parse(await js(`JSON.stringify((() => { const h = document.getElementById('in-hint'), d = document.documentElement;
    return { hint: h.scrollWidth - h.clientWidth, page: d.scrollWidth - d.clientWidth } })())`));
  check('a long file name in the hint wraps, with no sideways scroll', wide.hint <= 0 && wide.page <= 0, wide);
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
}

// ── Ctrl+Alt+U with the window in the background: the title and the notification name the
// placeholders still to fill in (DAILY-2), and text that passes is never made worse (DAILY-3)
async function copiedNote() {
  for (const [what, draft, title, body] of [
    ['with a placeholder', SLOP + '\n\nBest,\n[Your Name]', 'Copied - fill in 1 - SlopMonster', /Fill in \[Your Name\] before you send it\.$/],
    ['with none', SLOP, 'Copied - SlopMonster', /Paste it where you need it\.$/],
  ]) {
    await fresh({ clipboard: draft });
    await js(`(() => { document.hasFocus = () => false; window.__notes = [];
      window.Notification = function (t, o) { window.__notes.push({ t, body: o.body }); this.close = () => {}; };
      window.Notification.permission = 'granted'; return true })()`);
    writeFileSync(path.join(tmp, 'hotkey.txt'), draft);
    await done();
    await sleep(300);
    const notes = await js('window.__notes');
    check(`hotkey ${what}: the title says so`, (await facts()).title === title, await facts());
    check(`hotkey ${what}: and so does the notification`, notes.length === 1 && body.test(notes[0].body), notes);
  }
  await fresh();
  await paste('The tool reads one file. It prints what it finds and stops.');
  await done();
  await click('#anyway');
  await waitFor(`document.documentElement.dataset.run === 'working'`);
  await done();
  const shown = await js(`[document.getElementById('outcome-title').textContent, document.getElementById('result').textContent]`);
  check('a rewrite that only adds a pattern to text that passes gives the text back as it was',
    shown[0] === 'Your text is shown as it was.' && shown[1].startsWith('The tool reads one file.'), shown);
}

// ── a list of three costs a point but never stops a pass (README: "Lists of three")
async function listsOfThree() {
  await fresh();
  await paste('We leverage notes, calls, and emails. The rest of this text is plain and fine.');
  await done();
  const shown = JSON.parse(await js(`JSON.stringify({
    title: document.getElementById('outcome-title').textContent,
    lists: !document.getElementById('lists').hidden, verify: !document.getElementById('verify').hidden,
    chips: Array.from(document.querySelectorAll('#lists-hits .hit')).map((h) => h.textContent),
    row: Array.from(document.querySelectorAll('#cats .cat')).map((r) => r.querySelector('.cat-title').textContent + ': ' + r.querySelector('.cat-state').textContent)
      .find((s) => s.startsWith('Three-item lists')) || '' })`));
  check('a list of three left in the rewrite: it passes after 1 round', shown.title.startsWith('Passes the checker after 1 round'), shown);
  check('... the list is shown under Lists of three, not Verify these claims',
    shown.lists && !shown.verify && shown.chips.length === 1 && shown.chips[0].includes('notes, calls, and emails'), shown);
  check('... and the left pane calls it optional', / optional$/.test(shown.row), shown.row);

  // A chat answer's bold labels left in the rewrite would paste with their asterisks.
  await fresh();
  await paste('Here is the plan for a seamless handover.\n\n- **Cost:** It costs little this year.\n- **Speed:** It takes one week.\n- **Support:** Our own team answers calls.');
  await done();
  const meta = await js(`document.getElementById('outcome-meta').textContent`);
  check('bold labels left in the rewrite are named under the title', meta.includes('3 bold labels are still in the text.'), meta);

  // Labels the rewrite folded, put back by Keep original, are named too: Copy pastes them with asterisks.
  await fresh();
  await paste('Here is the plan for a robust launch.\n\n- **Budget:** We spend little on it this year.\n- **Timing:** We start in May.\n- **Owner:** Our team runs it.');
  await done();
  const metaNow = () => js(`document.getElementById('outcome-meta').textContent`);
  check('bold labels the rewrite folded are not named', !(await metaNow()).includes('bold label'), await metaNow());
  await click('input[name=view][value=changes]');
  await waitFor(`document.querySelectorAll('.chg-btn').length === 2`);
  await js(`(() => { const b = document.querySelectorAll('.chg-btn'); b[b.length - 1].click(); return true })()`);
  await waitFor(`document.getElementById('outcome-meta').textContent.includes('bold label')`, 5000);
  check('... and are named once Keep original puts them back', (await metaNow()).includes('3 bold labels are still in the text.'), await metaNow());
  await js(`(() => { const b = document.querySelectorAll('.chg-btn'); b[b.length - 1].click(); return true })()`);
  await sleep(500);
  check('... and no longer once Use the rewrite folds them again', !(await metaNow()).includes('bold label'), await metaNow());
  await click('input[name=view][value=result]');
}

// ── a caveat the rewrite added is named under Less sure than your text, and stops nothing
async function lessSure() {
  await fresh();
  await paste('Our robust backups run every night at two.');
  await done();
  await sleep(300);
  const f = await facts();
  const shown = JSON.parse(await js(`JSON.stringify({ hedged: !document.getElementById('hedged').hidden,
    carried: !document.getElementById('carried').hidden,
    chips: Array.from(document.querySelectorAll('#hedged-hits .hit')).map((h) => h.textContent) })`));
  check('an added caveat is listed under Less sure than your text', shown.hedged && shown.chips.length === 1
    && shown.chips[0].includes('generally') && !shown.carried, shown);
  check('... and it stops nothing: the title passes and the focus is on Copy', f.title.startsWith('Passes') && f.focus === 'copy', f);

  const draft = 'Our robust backups run every night at two.';
  await fresh({ clipboard: draft });
  await js(`(() => { document.hasFocus = () => false; window.__notes = [];
    window.Notification = function (t, o) { window.__notes.push({ t, body: o.body }); this.close = () => {}; };
    window.Notification.permission = 'granted'; return true })()`);
  writeFileSync(path.join(tmp, 'hotkey.txt'), draft);
  await done();
  await sleep(300);
  const notes = await js('window.__notes');
  check('... a copied result\'s notification says so', notes.length === 1 && notes[0].body.endsWith('1 sentence is less sure than yours.'), notes);
}

// ── Where it will appear goes with every run, so the window names it while it applies
async function contextShown() {
  const seen = () => js(`JSON.stringify({ idle: document.getElementById('idle-note').textContent, meta: document.getElementById('outcome-meta').textContent,
    value: document.getElementById('context').value, clear: !document.getElementById('context-clear').hidden,
    focus: document.activeElement && document.activeElement.id })`).then(JSON.parse);
  await fresh();
  let s = await seen();
  check('no context: no Clear button and no mention of one', !s.clear && !/written for/i.test(s.idle), s);
  await paste(SLOP);
  await done();
  s = await seen();
  check('no context: the result does not say what it was written for', !/Written for/.test(s.meta), s.meta);

  await fresh();
  await js(`(() => { const c = document.getElementById('context'); c.focus(); c.value = 'homepage for a roofing company';
    c.dispatchEvent(new Event('input', { bubbles: true })); return true })()`);
  s = await seen();
  check('a context: the idle note names it, Ctrl+Alt+U runs included, and Clear shows',
    s.idle.endsWith('Runs are written for "homepage for a roofing company", Ctrl+Alt+U runs included.') && s.clear, s);
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'working'`);
  const working = await js(`document.getElementById('work-note').textContent`);
  check('... the working note names it', working.endsWith('Written for: "homepage for a roofing company".'), working);
  await done();
  s = await seen();
  check('... and so does the result', s.meta.endsWith('Written for: "homepage for a roofing company".'), s.meta);
  await click('#context-clear');
  await sleep(200);
  s = await seen();
  check('Clear empties the field, hides itself, and puts the focus in the field', s.value === '' && !s.clear && s.focus === 'context', s);
  check('... and the idle note no longer names it', !/written for/i.test(s.idle), s.idle);

  // A long one is cut under the result title, so that it leaves the result its room.
  const long = 'the spring newsletter for a roofing company in a small town, read by homeowners and builders. '.repeat(3).trim();
  await js(`(() => { const c = document.getElementById('context'); c.value = ${JSON.stringify(long)};
    c.dispatchEvent(new Event('input', { bubbles: true })); return true })()`);
  await paste(SLOP + ' Once more.');
  await done();
  s = await seen();
  const cut = Array.from(long).slice(0, 80).join('').trimEnd() + '…';
  const tip = await js(`document.getElementById('outcome-meta').title`);
  check('a long context is cut to 80 characters under the result title', s.meta.endsWith('Written for: "' + cut + '".'), s.meta);
  check('... with the whole of it in the tooltip', tip === 'Written for: ' + long, tip);
}

// ── Try with the other AI: one run with it, the choice in Settings untouched (README: "Try with Claude")
async function otherEngine() {
  const copiesFile = path.join(tmp, 'copies.txt');
  const copies = () => existsSync(copiesFile) ? readFileSync(copiesFile, 'utf8').split('\n').filter(Boolean).length : 0;
  const shown = () => js(`JSON.stringify({ other: !document.getElementById('other').hidden, otherText: document.getElementById('other').textContent,
    errOther: !document.getElementById('err-other').hidden, errOtherText: document.getElementById('err-other').textContent,
    history: Array.from(document.querySelectorAll('#history option')).filter((o) => !o.hidden).map((o) => o.textContent) })`).then(JSON.parse);
  const gpt = { 'slop-engine': 'chatgpt' };

  await fresh({}, gpt);
  await paste(SLOP);
  await done();
  let s = await shown();
  check('a ChatGPT result offers Try with Claude', s.other && s.otherText === 'Try with Claude', s);
  let c0 = calls().length;
  await click('#other');
  await waitFor(`document.documentElement.dataset.run === 'working'`);
  await done();
  let f = await facts();
  s = await shown();
  check('Try with Claude: the run goes to Claude', calls().length === c0 + 1 && calls().at(-1).engine === 'claude', calls().slice(c0));
  check('... the choice in Settings, its storage and the pill stay ChatGPT', f.stored === 'chatgpt' && f.engine === 'chatgpt' && f.pill.startsWith('ChatGPT'), f);
  check('... both results are listed, one named Claude', s.history.length === 2 && s.history.filter((l) => / · Claude( [0-5]\/5)? · /.test(l)).length === 1, s.history);
  check('... and the box holds the draft, not the rewrite', f.main === SLOP, f.main);
  check('... and the Claude result offers ChatGPT back', s.other && s.otherText === 'Try with ChatGPT', s);

  await typeMain('Some other words I typed into the box after the run.');
  await sleep(500);
  c0 = calls().length;
  await click('#other');
  await done();
  f = await facts();
  check('Try with the other AI after an edit sends the result\'s own draft', f.main === SLOP && calls().length === c0 + 1 && calls().at(-1).engine === 'chatgpt', [f.main, calls().slice(c0)]);

  // Ctrl+Alt+U copies its own result, but never one from Try with the other AI.
  await fresh({ clipboard: SLOP }, gpt);
  await js(`(() => { document.hasFocus = () => false; window.__notes = [];
    window.Notification = function (t, o) { window.__notes.push({ t, body: o.body }); this.close = () => {}; };
    window.Notification.permission = 'granted'; return true })()`);
  const earlier = copies();
  writeFileSync(path.join(tmp, 'hotkey.txt'), SLOP);
  await done();
  await sleep(300);
  const copied = copies() - earlier, noted = (await js('window.__notes')).length;
  c0 = calls().length;
  await click('#other');
  await waitFor(`document.documentElement.dataset.run === 'working'`);
  await done();
  await sleep(300);
  check('after a hotkey run, Try with Claude makes a call', calls().length === c0 + 1 && calls().at(-1).engine === 'claude', calls().slice(c0));
  check('... and its result is neither copied nor notified', copied === 1 && copies() === earlier + copied && (await js('window.__notes')).length === noted, [earlier, copied, copies(), noted]);

  await fresh({ fail: 'usage_limit', fail_engine: 'chatgpt', delay: 0.2 }, gpt);
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  s = await shown();
  check('a ChatGPT usage limit offers Try with Claude', s.errOther && s.errOtherText === 'Try with Claude', s);
  // Try again runs what the box and Where it will appear hold now, so Try with Claude goes
  // while they no longer hold what failed: the two buttons never run different texts.
  const setContext = (v) => js(`(() => { const c = document.getElementById('context'); c.value = ${JSON.stringify(v)};
    c.dispatchEvent(new Event('input', { bubbles: true })); return true })()`);
  await typeMain(SLOP + ' Edited after the error.');
  s = await shown();
  check('... an edit in the box hides it', !s.errOther, s);
  await typeMain(SLOP);
  await setContext('a clinic newsletter');
  s = await shown();
  check('... and so does a new Where it will appear', !s.errOther, s);
  await setContext('');
  s = await shown();
  check('... and it is back with the text that failed', s.errOther, s);
  c0 = calls().length;
  await key('Enter', 'Enter', 13, '\r', 2 | 8);
  check('... and Ctrl+Shift+Enter runs it with Claude', await done() && calls().length === c0 + 1 && calls().at(-1).engine === 'claude', calls().slice(c0));

  // Try with Claude fails: the error names Claude, and Try again goes to Claude, not to ChatGPT.
  await fresh({}, gpt);
  await paste(SLOP);
  await done();
  control({ fail: 'usage_limit', fail_engine: 'claude', delay: 0.2 });
  await click('#other');
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  s = await shown();
  const errTitle = await js(`document.getElementById('err-title').textContent`);
  check('a failed Try with Claude names Claude and offers ChatGPT back', errTitle.startsWith('Claude') && s.errOther && s.errOtherText === 'Try with ChatGPT', [errTitle, s]);
  c0 = calls().length;
  await click('#err-action');
  for (const end = Date.now() + 5000; Date.now() < end && calls().length === c0; ) await sleep(100);
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  f = await facts();
  check('... and Try again runs it with Claude once more', calls().length === c0 + 1 && calls().at(-1).engine === 'claude', calls().slice(c0));
  check('... the choice in Settings still ChatGPT', f.stored === 'chatgpt' && f.engine === 'chatgpt', f);

  await fresh({ claude: 'login' }, gpt);
  await paste(SLOP);
  await done();
  s = await shown();
  check('Claude signed out: no Try with Claude under the result', !s.other, s);
  control({ claude: 'login', fail: 'usage_limit', fail_engine: 'chatgpt', delay: 0.2 });
  await paste(SLOP + ' Again.');
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  s = await shown();
  check('... nor on the error', !s.errOther && (await js(`document.getElementById('err-title').textContent`)).includes('usage limit'), s);

  await fresh({ fail: 'cancelled', delay: 0.2 }, gpt);
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  s = await shown();
  check('a run stopped from another window offers no other AI', !s.errOther && (await js(`document.getElementById('err-title').textContent`)) === 'This run was stopped', s);
}

// ── engine failures, text with nothing to rewrite, right-to-left text (FR-1, FR-2, FR-4, FR-5)
async function edges() {
  const claude = { 'slop-engine': 'claude' };
  await fresh({ fail: 'usage_limit', delay: 0.2 }, claude);
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  let got = await js(`[document.getElementById('err-title').textContent, document.getElementById('err-settings').hidden]`);
  check('a Claude usage limit is named as Claude\'s, with Settings beside it', got[0] === 'Claude has reached its usage limit' && got[1] === false, got);

  // The sign-in lapses after the first part of a long text: the rest is left, and the pill,
  // the line under the title and Settings all say Claude is signed out.
  await fresh({ delay: 0.2 }, Object.assign({ 'slop-strength': 'thorough' }, claude));
  control({ delay: 0.2, fail: 'not_logged_in', fail_from: calls().length + 1 });
  await paste(["It's not just a tool, it's a game-changing journey.", ...Array(10).fill(PLAIN)].join('\n\n'));
  await sleep(500);
  if ((await facts()).view !== 'working') await click('#unslop');
  await done();
  got = await js(`[document.getElementById('engine-text').textContent, document.getElementById('outcome-meta').textContent, document.getElementById('signin-claude').hidden]`);
  check('a sign-in that lapses part-way: the pill says Claude is not signed in', got[0] === 'Claude not signed in', got);
  check('... the line under the title says why the rest was left', /Claude was not signed in, so the rest of the text was left as it was\./.test(got[1]), got[1]);
  check('... and Settings offers the sign-in', got[2] === false, got);

  await fresh({ delay: 0.2 }, claude);
  await paste('```python\ndef add(a, b):\n    return a + b\n```');
  await waitFor(`document.documentElement.dataset.run === 'error'`);
  // The check of the paste is its own request, and on a slow machine it lands after the run's error.
  await waitFor(`document.getElementById('check-empty-text').textContent.startsWith('The checker leaves')`);
  got = await js(`[document.getElementById('err-title').textContent, document.getElementById('err-body').textContent, document.getElementById('check-empty-text').textContent]`);
  check('code blocks alone: the error says they are left as they are', got[0] === 'There is nothing to unslop'
    && got[1] === 'SlopMonster leaves code blocks as they are, and this text has nothing outside them to rewrite.', got);
  check('... and so does the checker', got[2] === 'The checker leaves code blocks out, so there is nothing here to score.', got[2]);

  // Arabic reads from the right: the full stop ends the line on the left.
  await fresh({ delay: 0.2 }, claude);
  await paste('هذا نص قصير عن التطبيق وما يفعله كل يوم.');
  await done();
  got = await js(`(() => {
    const box = document.getElementById('result'), walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const node = walk.nextNode(), text = node.data.trimEnd(), at = (i) => { const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1); return r.getBoundingClientRect().left; };
    return { first: at(0), stop: at(text.length - 1), input: document.getElementById('input').dir, context: document.getElementById('context').dir };
  })()`);
  check('right-to-left text: the result ends its sentence on the left', got.stop < got.first, got);
  check('right-to-left text: the boxes take their direction from the text', got.input === 'auto' && got.context === 'auto', got);
}

// ── the first run: neither AI can rewrite, so the result pane offers both (G1)
async function welcome() {
  const seen = () => js(`JSON.stringify({ welcome: !document.getElementById('s-welcome').hidden, idle: !document.getElementById('s-idle').hidden,
    error: !document.getElementById('s-error').hidden, flag: document.documentElement.dataset.welcome, run: document.documentElement.dataset.run,
    signClaude: !document.getElementById('w-signin-claude').hidden, signGpt: !document.getElementById('w-signin-chatgpt').hidden,
    howClaude: !document.getElementById('w-how-claude').hidden, howGpt: !document.getElementById('w-how-chatgpt').hidden,
    stateClaude: document.getElementById('w-state-claude').dataset.state, stateGpt: document.getElementById('w-state-chatgpt').dataset.state,
    note: document.getElementById('welcome-note').textContent, check: document.getElementById('check').dataset.state,
    stored: localStorage.getItem('slop-engine'), focus: document.activeElement && (document.activeElement.id || document.activeElement.tagName),
    title: document.title })`).then(JSON.parse);

  // The switch is off so the typed text below waits for Unslop; the paste further down runs by itself.
  await fresh({ claude: 'login', chatgpt: 'login' }, { 'slop-auto': 'off' });
  let s = await seen();
  check('first run, both signed out: the cards show in place of the idle and error views',
    s.welcome && !s.idle && !s.error && s.flag === 'on' && s.run === 'error', s);
  check('... each card offers its sign-in', s.signClaude && s.signGpt && !s.howClaude && !s.howGpt
    && s.stateClaude === 'not_logged_in' && s.stateGpt === 'not_logged_in', s);
  check('... and the window is titled by the cards', s.title.startsWith('Connect Claude or ChatGPT'), s.title);
  await typeMain(SLOP);
  await waitFor(`document.getElementById('check').dataset.state !== 'empty'`);
  s = await seen();
  check('... the checker scores typed text beside the cards', s.check !== 'empty' && s.welcome && s.focus === 'input', s);

  await focusOn('#welcome-check');
  await click('#welcome-check');
  await waitFor(`!document.getElementById('welcome-check').disabled`);
  await sleep(300);
  s = await seen();
  check('... Check again keeps the focus on itself', s.focus === 'welcome-check' && s.welcome, s);

  await focusOn('#w-signin-chatgpt');
  await click('#w-signin-chatgpt');
  await waitFor(`document.getElementById('welcome-note').textContent !== ''`);
  s = await seen();
  check('... a sign-in from the ChatGPT card makes ChatGPT the AI that rewrites', s.stored === 'chatgpt' && s.welcome && /sign-in window/.test(s.note), s);
  control({ claude: 'login', chatgpt: 'chatgpt' });
  await waitFor(`document.getElementById('s-welcome').hidden`, 10000);
  await sleep(300);
  s = await seen();
  check('... once ChatGPT is signed in the cards give way to the idle view', !s.welcome && s.idle && !s.error && s.flag === 'off' && s.run === 'idle', s);
  check('... and the focus goes on to Unslop', s.focus === 'unslop', s);
  const live = await js(`document.getElementById('live').textContent`);
  check('... and the page says ChatGPT is ready', live === 'ChatGPT is ready.', live);

  await fresh({ claude: 'login', chatgpt: 'login' });
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error' && document.documentElement.dataset.welcome === 'on'`);
  await click('#w-signin-claude');
  await waitFor(`document.getElementById('welcome-note').textContent !== ''`);
  control({ claude: 'ready', chatgpt: 'login' });
  check('a paste held for a sign-in from the cards runs once Claude is signed in', await done(), await facts());

  await fresh({ claude: 'login', chatgpt: 'missing' });
  s = await seen();
  check('first run, Claude signed out and Codex missing: Claude\'s card offers its sign-in', s.welcome && s.signClaude && !s.howClaude, s);
  check('... and the Codex card says how to get it', s.howGpt && !s.signGpt && s.stateGpt === 'missing', s);

  await fresh({ claude: 'missing', chatgpt: 'missing' });
  s = await seen();
  check('first run, neither installed: both cards say how to get them', s.welcome && s.howClaude && s.howGpt && !s.signClaude && !s.signGpt, s);
  await click('#welcome-settings');
  await waitFor(`document.getElementById('settings').open`);
  check('... and Open settings opens Settings', await js(`document.getElementById('settings').open`), null);
  const cards = JSON.parse(await js(`JSON.stringify(['claude', 'chatgpt'].map((e) => document.getElementById('state-' + e).lastElementChild.textContent))`));
  check('... where each card says how to get its CLI', cards.every((t) => /not found\. Install /.test(t)), cards);
  await esc();
  const note = await js(`document.querySelector('#s-welcome .view-note').textContent`);
  check('... and the note names no side, since narrow windows stack the panes', !/\b(left|right)\b/.test(note), note);

  // Ctrl+V with a card's button focused: the run cannot start, and focus stays on the cards.
  await fresh({ claude: 'login', chatgpt: 'login' });
  await focusOn('#w-signin-claude');
  await js(`(() => { const dt = new DataTransfer(); dt.setData('text/plain', ${JSON.stringify(SLOP)});
    document.activeElement.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); return true })()`);
  await waitFor(`document.documentElement.dataset.run === 'error' && document.getElementById('input').value !== ''`);
  await sleep(500);
  s = await seen();
  check('a paste with a card\'s button focused leaves the focus on a sign-in button', s.welcome && s.focus === 'w-signin-claude', s);

  await fresh({ claude: 'login', chatgpt: 'api_key' });
  s = await seen();
  check('one AI ready: no cards, the chosen AI\'s sign-in instead', !s.welcome && s.error && s.flag === 'off', s);
  await fresh({ claude: 'ready', chatgpt: 'missing' });
  s = await seen();
  check('one AI ready and the other missing: no cards', !s.welcome && s.idle && s.flag === 'off', s);
}

// ── sizes: the box after a run at the app's own window, the outcome band, the instructions box
async function sizes() {
  // The app opens at --window-size=1180,820, which leaves a page of about 1164 by 781.
  await send('Emulation.setDeviceMetricsOverride', { width: 1164, height: 781, deviceScaleFactor: 1, mobile: false });
  await fresh();
  await paste(SLOP);
  await done();
  await sleep(500);
  const box = await js(`document.getElementById('input').clientHeight`);
  check('after a run at the app\'s own window size the box keeps room for its text', box >= 150, box);
  for (const width of [900, 960, 1024, 1180, 1440]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 760, deviceScaleFactor: 1, mobile: false });
    await sleep(400);
    const band = JSON.parse(await js(`JSON.stringify((() => {
      const d = document.querySelector('.delta').getBoundingClientRect(), t = document.querySelector('.outcome-text');
      return { below: t.getBoundingClientRect().top >= d.bottom - 1, rule: getComputedStyle(t).borderLeftWidth } })())`));
    check(`outcome band at ${width}px: no rule left over when the text wraps under the scores`, !band.below || band.rule === '0px', band);
  }
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await fresh();
  await openSettings();
  const fit = JSON.parse(await js(`JSON.stringify((() => {
    const p = document.getElementById('prompt'), cs = getComputedStyle(p), c = document.createElement('canvas').getContext('2d');
    c.font = cs.font;
    const longest = Math.max(...p.value.split('\\n').map((l) => c.measureText(l).width));
    return { longest: Math.ceil(longest), room: p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) } })())`));
  check('Settings: each line of the default instructions fits the box unwrapped', fit.longest > 0 && fit.longest <= fit.room, fit);
  await esc();
}

// ── a window nobody is looking at draws nothing, and a dropped connection costs no work
async function recovery() {
  // The background drifts only while someone is at the window (SOAK-1): an idle window left
  // open all day kept the GPU drawing every frame.
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await fresh();
  const drift = () => js(`JSON.stringify({ still: document.documentElement.hasAttribute('data-still'),
    play: getComputedStyle(document.querySelector('.b1')).animationPlayState })`).then(JSON.parse);
  await js(`window.dispatchEvent(new Event('focus')); true`);
  let d = await drift();
  check('a window in use: the background drifts', !d.still && d.play === 'running', d);
  await js(`window.dispatchEvent(new Event('blur')); true`);
  d = await drift();
  check('a window that lost the focus: the background holds still', d.still && d.play === 'paused', d);
  await js(`window.dispatchEvent(new Event('focus')); true`);
  d = await drift();
  check('... and drifts again once the focus is back', !d.still && d.play === 'running', d);
  await send('Emulation.setFocusEmulationEnabled', { enabled: false });

  // Requests to the API fail while `cut` is 2, as when the network changes on waking from
  // sleep. cut === 1 fails the next check only.
  let cut = 0, navigations = 0;
  const hook = (m) => {
    if (m.method === 'Page.frameNavigated' && !m.params.frame.parentId) navigations++;
    if (m.method !== 'Fetch.requestPaused') return;
    const { requestId } = m.params;
    if (cut === 2 || cut === 1 && m.params.request.url.includes('/api/check')) {
      if (cut === 1) cut = 0;
      send('Fetch.failRequest', { requestId, errorReason: 'ConnectionReset' });
    } else {
      send('Fetch.continueRequest', { requestId });
    }
  };
  hooks.add(hook);
  try {
    await fresh();
    await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/*' }] });
    await paste(SLOP);
    await done();
    await typeMain('');
    await paste('It is a tool. ' + SLOP);
    await done();
    const history = () => js(`document.getElementById('history').options.length`);
    const had = await history();
    const DRAFT = 'A draft the user is still writing, with a few more words to score.';

    // One check fails while the server is fine: no "not running", and nothing is lost.
    cut = 1;
    await typeMain(DRAFT);
    await waitFor(`/connection dropped/.test(document.getElementById('check-empty-text').textContent)`, 5000);
    await sleep(800);
    let s = JSON.parse(await js(`JSON.stringify({ power: document.documentElement.dataset.power, block: !document.getElementById('block').hidden,
      note: document.getElementById('check-empty-text').textContent, main: document.getElementById('input').value })`));
    check('one failed check with the server fine: the window is not blocked', s.power === 'on' && !s.block, s);
    check('... the check says the connection dropped, and the draft stays', /connection dropped/.test(s.note) && s.main === DRAFT, s);

    // Every request fails: the server looks stopped. Once it answers again, Check again
    // reconnects this same window: the draft and the results stay (REC-2).
    cut = 2;
    await typeMain(DRAFT + ' More.');
    check('with every request failing, the window says SlopMonster is not running',
      await waitFor(`document.documentElement.dataset.power === 'blocked'`, 6000));
    cut = 0;
    navigations = 0;
    await click('#block-action');
    await waitFor(`document.documentElement.dataset.power === 'on'`, 6000);
    s = JSON.parse(await js(`JSON.stringify({ power: document.documentElement.dataset.power, main: document.getElementById('input').value,
      history: document.getElementById('history').options.length })`));
    check('Check again with the server back: the same window carries on, draft and results kept',
      s.power === 'on' && s.main === DRAFT + ' More.' && s.history === had && navigations === 0, { s, had, navigations });
    writeFileSync(path.join(tmp, 'hotkey.txt'), SLOP);
    check('... and Ctrl+Alt+U reaches it again', await waitFor(`document.getElementById('input').value.startsWith('Are you ready')`) && await done());

    // The same, with nobody pressing the button: it comes back by itself.
    cut = 2;
    await typeMain(DRAFT);
    await waitFor(`document.documentElement.dataset.power === 'blocked'`, 6000);
    cut = 0;
    navigations = 0;
    check('after a longer drop the window reconnects by itself', await waitFor(`document.documentElement.dataset.power === 'on'`, 9000)
      && navigations === 0 && await js(`document.getElementById('input').value`) === DRAFT, navigations);
  } finally {
    cut = 0;
    await send('Fetch.disable');
    hooks.delete(hook);
  }
}

// ── all-day use: Enter after a hold, the reload guard, the window in front, what the page names
async function nonstop() {
  const pressed = (text) => writeFileSync(path.join(tmp, 'hotkey.txt'), text);
  const callsReach = async (n, ms = 8000) => {
    for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) if (calls().length >= n) return true;
    return false;
  };

  // A short text held from Ctrl+Alt+U: the focus is on Unslop, so Enter runs it.
  await fresh();
  let before = calls().length;
  pressed('Fix this now please');
  await waitFor(`/only 4 words/.test(document.getElementById('in-hint').textContent)`);
  let f = await facts();
  check('a short text held from Ctrl+Alt+U: the focus is on Unslop', f.focus === 'unslop' && f.view === 'idle', f);
  await enter();
  check('... and Enter runs it', await callsReach(before + 1), calls().length - before);
  await waitFor(`['done', 'error'].includes(document.documentElement.dataset.run)`, 20000);
  // A key or a password is held with the focus off Unslop, so Enter from habit sends nothing.
  await fresh();
  before = calls().length;
  pressed('Here is the config for the staging box: password = hunter2hunter2 and the rest of the notes follow below.');
  await waitFor(`/key or a password/.test(document.getElementById('in-hint').textContent)`);
  f = await facts();
  check('a password held from Ctrl+Alt+U: the focus is not on Unslop', f.focus === 'BODY', f);
  await enter();
  await sleep(1200);
  f = await facts();
  check('... and Enter sends nothing', calls().length === before && f.view === 'idle', { f, calls: calls().length - before });

  // F5 and Ctrl+R ask first while there is something to lose (ERGO-5).
  let navs = 0;
  const hook = (m) => { if (m.method === 'Page.frameNavigated' && !m.params.frame.parentId) navs++; };
  // With the native key code too: the browser's own F5 and Ctrl+R are seen only with it.
  const browserKey = async (k, code, vk, modifiers = 0) => {
    await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    await sleep(300);
  };
  hooks.add(hook);
  try {
    await fresh();
    navs = 0;
    await browserKey('F5', 'F5', 116);
    check('F5 on an empty window reloads at once', await (async () => {
      for (const end = Date.now() + 5000; Date.now() < end; await sleep(100)) if (navs) return true;
      return false;
    })(), navs);
    await waitFor(`document.readyState === 'complete' && document.getElementById('engine').dataset.state !== 'checking'`);
    await paste(SLOP);
    await done();
    navs = 0;
    await browserKey('F5', 'F5', 116);
    await sleep(500);
    f = await facts();
    check('F5 with a result on show: no reload, and the hint says why', navs === 0 && /Press it again to reload anyway/.test(f.hint)
      && f.view === 'done', { navs, f });
    await browserKey('r', 'KeyR', 82, 2);
    check('... and Ctrl+R straight after reloads', await (async () => {
      for (const end = Date.now() + 5000; Date.now() < end; await sleep(100)) if (navs) return true;
      return false;
    })(), navs);
    await waitFor(`document.readyState === 'complete' && document.getElementById('engine').dataset.state !== 'checking'`);
  } finally {
    hooks.delete(hook);
  }

  // Two windows: a press goes to the one in front, not to whichever asked first (SOAK-2).
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  const ctrl = new AbortController();
  const other = [];
  try {
    await fresh();
    await js(`window.dispatchEvent(new Event('focus')); true`);
    await sleep(300);
    const html = await (await fetch(url)).text();
    const token = (/<meta name="slop-token" content="([^"<>]+)">/.exec(html) || [])[1];
    (async () => {
      while (!ctrl.signal.aborted) {
        try {
          const r = await fetch(url + 'api/events?page=elsewhere', { headers: { 'X-Slop-Token': token }, signal: ctrl.signal });
          const got = await r.json();
          if (got.events && got.events.length) other.push(...got.events);
        } catch { return; }
      }
    })();
    await sleep(300);
    let reached = 0;
    for (const text of ['Fix this now please', 'Fix that now please', 'Fix them now please']) {
      pressed(text);
      if (await waitFor(`document.getElementById('input').value === ${JSON.stringify(text)}`, 5000)) reached++;
    }
    check('two windows: every press goes to the window in front', reached === 3 && !other.length, { reached, other });
  } finally {
    ctrl.abort();
    await send('Emulation.setFocusEmulationEnabled', { enabled: false });
  }

  // Thorough is named in the pill, as it changes what every run does.
  await fresh({}, { 'slop-engine': 'claude', 'slop-strength': 'thorough' });
  f = await facts();
  check('the pill names Thorough when it is on', f.pill === 'Claude is ready \u00b7 Thorough', f.pill);
  await fresh({}, { 'slop-engine': 'claude' });
  f = await facts();
  check('... and says nothing of it when it is off', f.pill === 'Claude is ready', f.pill);

  // A sentence pattern's chip shows the words it matched, with the rule on hover.
  await typeMain(SLOP);
  check('a chip shows the matched words, and the rule on hover', await waitFor(`[...document.querySelectorAll('#cats .hit[title]')]
    .some((c) => /^"[^"]*not just a calendar[^"]*"/i.test(c.querySelector('.hit-label').textContent) && c.title.length > 0)`),
  await js(`[...document.querySelectorAll('#cats .hit')].map((c) => [c.textContent, c.title]).slice(0, 6)`));

  // The list of results names the AI and the score of each.
  await typeMain('');
  await paste(SLOP);
  await done();
  await typeMain('');
  await paste('It is a tool. ' + SLOP);
  await done();
  const labels = await js(`[...document.getElementById('history').options].map((o) => o.textContent)`);
  check('the list of results names the AI and the score', labels.length === 2 && labels.every((l) => / · Claude [0-5]\/5 · /.test(l)), labels);

  // A plan that has run out: one click runs the same text with the other AI, this once.
  await fresh({ fail: 'usage_limit' }, { 'slop-engine': 'claude' });
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error'`, 15000);
  const button = await js(`(() => { const b = document.getElementById('err-other'); return { hidden: b.hidden, text: b.textContent } })()`);
  check('a usage limit offers the other AI', !button.hidden && button.text === 'Try with ChatGPT', button);
  control();
  before = calls().length;
  await click('#err-other');
  check('... which runs the text with it', await done() && calls().slice(before).every((c) => c.engine === 'chatgpt')
    && calls().length > before, calls().slice(before));
  f = await facts();
  check('... and leaves Claude chosen in Settings', f.engine === 'claude' && f.stored === 'claude', f);
  await fresh({ fail: 'cancelled' }, { 'slop-engine': 'claude' });
  await paste(SLOP);
  await waitFor(`document.documentElement.dataset.run === 'error'`, 15000);
  check('a stopped run does not offer it', await js(`document.getElementById('err-other').hidden`));
  control();
}

// Flows named on the command line run alone, as in: python app/test_page.py looks
const only = process.argv.slice(6);
for (const flow of [holds, firstEngine, settingsPanel, results, undoLimits, undoMemory, saveWord, focusAndTitles, copiedNote, listsOfThree, lessSure, contextShown, otherEngine, longTexts, looks, edges, welcome, sizes, recovery, nonstop]) {
  if (only.length && !only.includes(flow.name)) continue;
  try {
    await flow();
  } catch (err) {
    check(`${flow.name} crashed`, false, String(err && err.stack || err));
  }
}
check('no page errors, navigations away or dialogs', !errors.length && !away.length && !dialogs.length, { errors, away, dialogs });
console.log(`${checks} page checks, ${failed} failed`);
try { await send('Browser.close'); } catch {}
ws.close();
process.exit(failed ? 1 : 0);
