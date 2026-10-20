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
const waiting = new Map(), errors = [], away = [], dialogs = [];
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
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

// Flows named on the command line run alone, as in: python app/test_page.py looks
const only = process.argv.slice(6);
for (const flow of [holds, firstEngine, settingsPanel, results, undoLimits, saveWord, focusAndTitles, copiedNote, longTexts, looks, edges, welcome, sizes]) {
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
