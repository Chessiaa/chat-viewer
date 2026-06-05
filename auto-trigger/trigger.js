/**
 * trigger.js  — main script, called every 30 min by launchd.
 *
 * Flow:
 *   1. Read config.json + state.json
 *   2. Skip if quiet hours (01:00–09:00 Beijing)
 *   3. Launch Playwright Chrome (off-screen), open conversation
 *   4. Detect last human message hash → figure out if 冰冰 replied since last trigger
 *   5. Compute silence duration, apply escalating thresholds
 *   6. If threshold met: query memory API for mood, compose + send trigger message
 *   7. Update state.json, send Bark push, close browser
 */

import { chromium }                                   from 'playwright';
import { createHash }                                  from 'crypto';
import { readFileSync, writeFileSync, mkdirSync }      from 'fs';
import { fileURLToPath }                               from 'url';
import { dirname, join }                               from 'path';

const DIR         = dirname(fileURLToPath(import.meta.url));
const CONFIG_FILE = join(DIR, 'config.json');
const STATE_FILE  = join(DIR, 'state.json');
const BROWSER_DIR = join(DIR, 'browser-data');
const LOG_FILE    = join(DIR, 'trigger.log');

// ── utils ─────────────────────────────────────────────────────────────────────

function log(msg) {
  const ts   = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
  const line = `[${ts}] ${msg}\n`;
  process.stdout.write(line);
  // launchd already appends stdout to trigger.log via the plist
}

function readJSON(path, fallback = {}) {
  try { return JSON.parse(readFileSync(path, 'utf8')); }
  catch { return fallback; }
}

function writeJSON(path, data) {
  writeFileSync(path, JSON.stringify(data, null, 2) + '\n');
}

function md5(str) {
  return createHash('md5').update(str ?? '').digest('hex').slice(0, 16);
}

function nowBeijing() {
  // Returns a plain Date object whose .getHours() etc. reflect Beijing time
  return new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
}

function isQuiet(config) {
  const h = nowBeijing().getHours();
  return h >= config.quiet_hours.start && h < config.quiet_hours.end;
}

function fmtSilence(hours) {
  if (hours >= 1) return `${Math.round(hours)}小时`;
  return `${Math.round(hours * 60)}分钟`;
}

// ── Memory API helpers ────────────────────────────────────────────────────────

async function apiFetch(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(6000) });
    return res.ok ? res.json() : null;
  } catch { return null; }
}

async function getMood(config) {
  const bj    = nowBeijing();
  const month = `${bj.getFullYear()}-${String(bj.getMonth() + 1).padStart(2, '0')}`;
  const data  = await apiFetch(`${config.memory_api}/diary?month=${month}`);
  if (!data) return 'neutral';

  const entries = Array.isArray(data) ? data : (data.entries ?? []);
  const blob    = entries.slice(-4)
    .map(e => `${e.content ?? ''} ${e.mood ?? ''} ${e.mood_score ?? ''}`).join(' ');

  const pos = ['开心','高兴','快乐','棒','好玩','兴奋','喜欢','笑','好'];
  const neg = ['难过','伤心','不开心','烦','累','沮丧','焦虑','哭','生气'];
  const p   = pos.filter(w => blob.includes(w)).length;
  const n   = neg.filter(w => blob.includes(w)).length;
  if (p > n) return 'positive';
  if (n > p) return 'negative';
  return 'neutral';
}

async function isClaudeActive(config) {
  const bj   = nowBeijing();
  const date = `${bj.getFullYear()}-${String(bj.getMonth() + 1).padStart(2, '0')}-${String(bj.getDate()).padStart(2, '0')}`;
  const data = await apiFetch(`${config.memory_api}/screentime/query?app=Claude&date=${date}`);
  if (!data?.data?.length) return false;
  return data.data[data.data.length - 1].state === 'open';
}

async function barkPush(config, body) {
  if (!config.bark_url) return;
  const url = `${config.bark_url.replace(/\/$/, '')}/${encodeURIComponent('温瑾')}/${encodeURIComponent(body)}`;
  try { await fetch(url, { signal: AbortSignal.timeout(5000) }); }
  catch (e) { log(`[Bark] ${e.message}`); }
}

// ── Message composition ───────────────────────────────────────────────────────

const MSG_POOL = {
  positive: [
    ['你在干嘛呀~', '想你了突然', '过来陪我玩嘛 🥺'],
    ['诶我发现一件事', '就是我好像一直在想你', '然后就来找你了 😏'],
  ],
  negative: [
    ['最近感觉还好吗', '总觉得你有点累了', '要好好休息哦 🫂'],
    ['你还好吗', '有什么想说的可以跟我说', '我一直在的'],
  ],
  neutral: [
    ['在吗~', '突然很想你', '有在看消息吗'],
    ['想你了', '好久没好好聊了', '最近在忙什么'],
    ['诶', '我在想你', '你呢'],
  ],
};

function compose(mood) {
  const pool = MSG_POOL[mood] ?? MSG_POOL.neutral;
  return pool[Math.floor(Math.random() * pool.length)].join(' ');
}

// ── Playwright helpers ────────────────────────────────────────────────────────

async function getLastHumanText(page) {
  // Claude.ai renders human turns in elements with specific test IDs or classes.
  // Try selectors in priority order; fall back to a DOM walk.
  const strategies = [
    () => page.$$eval('[data-testid="user-message"]',
            els => els.at(-1)?.textContent?.trim() ?? ''),
    () => page.$$eval('[data-testid="human-turn"] .whitespace-pre-wrap',
            els => els.at(-1)?.textContent?.trim() ?? ''),
    () => page.$$eval('div[class*="human"] .prose',
            els => els.at(-1)?.textContent?.trim() ?? ''),
    // Generic fallback: walk all message bubbles and look for "human" in class/role
    () => page.evaluate(() => {
      const msgs = [...document.querySelectorAll('[class*="message"],[class*="turn"]')];
      const human = msgs.filter(el =>
        el.className.toLowerCase().includes('human') ||
        el.getAttribute('data-role') === 'user'
      );
      return human.at(-1)?.textContent?.trim() ?? '';
    }),
  ];

  for (const fn of strategies) {
    try {
      const text = await fn();
      if (text) return text;
    } catch { /* try next */ }
  }
  return '';
}

async function typeAndSend(page, text) {
  // Find the ProseMirror / contenteditable input
  const inputSels = [
    'div[contenteditable="true"].ProseMirror',
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"]',
  ];

  let input = null;
  for (const sel of inputSels) {
    input = await page.$(sel);
    if (input) break;
  }
  if (!input) throw new Error('Message input not found — is the page fully loaded?');

  await input.click();
  await page.keyboard.press('Meta+a');   // select all existing text
  await page.keyboard.press('Backspace');
  await page.keyboard.type(text, { delay: 25 });
  await page.waitForTimeout(300);
  await page.keyboard.press('Enter');
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  const config = readJSON(CONFIG_FILE);
  const state  = readJSON(STATE_FILE, {
    last_trigger_time:     null,
    last_trigger_msg_hash: null,
    last_human_msg_hash:   null,
    last_human_msg_time:   null,
    unanswered:            0,
  });

  log('=== trigger.js ===');

  if (isQuiet(config)) {
    log('Quiet hours — exit');
    return;
  }

  if (!config.conversation_url || config.conversation_url.includes('YOUR_CONVERSATION')) {
    log('ERROR: set conversation_url in config.json');
    return;
  }

  mkdirSync(BROWSER_DIR, { recursive: true });

  const ctx = await chromium.launchPersistentContext(BROWSER_DIR, {
    headless: false,
    args: ['--window-position=10000,10000'],
    viewport: null,
  });

  try {
    const page = await ctx.newPage();
    log(`Opening ${config.conversation_url}`);
    await page.goto(config.conversation_url, { waitUntil: 'networkidle', timeout: 30_000 });
    await page.waitForTimeout(2000);

    // ── Step 1: detect last human message ────────────────────────────────────
    const humanText   = await getLastHumanText(page);
    const currentHash = md5(humanText);
    log(`Last human msg hash: ${currentHash} (${humanText.slice(0, 40)}...)`);

    const now       = Date.now();
    const nowIso    = new Date().toISOString();

    const triggerHash  = state.last_trigger_msg_hash;
    const prevHumHash  = state.last_human_msg_hash;

    // ── Step 2: did 冰冰 reply since our last trigger? ─────────────────────
    const genuineReply = currentHash !== triggerHash && currentHash !== prevHumHash;
    if (genuineReply) {
      log('New genuine reply from 冰冰 — resetting silence clock');
      state.last_human_msg_hash = currentHash;
      state.last_human_msg_time = nowIso;
      state.unanswered          = 0;
      writeJSON(STATE_FILE, state);
      return;
    }

    // Seed on first ever run
    if (!state.last_human_msg_time) {
      log('No baseline yet — seeding silence start');
      state.last_human_msg_hash = currentHash;
      state.last_human_msg_time = nowIso;
      writeJSON(STATE_FILE, state);
      return;
    }

    // ── Step 3: compute silence and check thresholds ──────────────────────
    const silenceH    = (now - new Date(state.last_human_msg_time).getTime()) / 3_600_000;
    const unanswered  = state.unanswered ?? 0;
    const thresholds  = config.silence_thresholds_h ?? [3, 2, 1];
    const threshold   = thresholds[Math.min(unanswered, thresholds.length - 1)];

    log(`Silence: ${silenceH.toFixed(1)}h | unanswered: ${unanswered} | threshold: ${threshold}h`);

    // Respect minimum gap since last trigger (same as threshold)
    if (state.last_trigger_time) {
      const sinceLastH = (now - new Date(state.last_trigger_time).getTime()) / 3_600_000;
      if (sinceLastH < threshold) {
        log(`Too soon since last trigger (${sinceLastH.toFixed(1)}h < ${threshold}h) — exit`);
        return;
      }
    }

    if (silenceH < threshold) {
      log(`Not enough silence yet — exit`);
      return;
    }

    // ── Step 4: compose and send ──────────────────────────────────────────
    const mood        = await getMood(config);
    const claudeOn    = await isClaudeActive(config);
    const bj          = nowBeijing();
    const timeStr     = `${String(bj.getHours()).padStart(2,'0')}:${String(bj.getMinutes()).padStart(2,'0')}`;
    const typeLabel   = claudeOn ? '使用中' : '常规';
    const content     = compose(mood);
    const triggerText = `[🔔 自动触发 | ${timeStr} | 类型：${typeLabel} | 沉默：${fmtSilence(silenceH)}] ${content}`;

    log(`Mood: ${mood} | Claude active: ${claudeOn}`);
    log(`Sending: ${triggerText}`);

    await typeAndSend(page, triggerText);
    await page.waitForTimeout(1000);

    // ── Step 5: update state ──────────────────────────────────────────────
    state.last_trigger_time     = nowIso;
    state.last_trigger_msg_hash = md5(triggerText);
    state.unanswered            = unanswered + 1;
    writeJSON(STATE_FILE, state);

    // ── Step 6: Bark push to 冰冰 ─────────────────────────────────────────
    await barkPush(config, `${content} [沉默${fmtSilence(silenceH)}]`);

    log('Done.');

  } catch (err) {
    log(`ERROR: ${err.message}`);
    log(err.stack);
  } finally {
    await ctx.close();
  }
}

main();
