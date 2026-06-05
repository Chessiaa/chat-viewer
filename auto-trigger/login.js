/**
 * login.js  — run once to save your claude.ai session.
 *
 * Usage:
 *   node login.js
 *
 * Steps:
 *   1. Browser opens claude.ai
 *   2. Log in manually
 *   3. Open (or start) the conversation you want the trigger to use
 *   4. Copy the URL (e.g. https://claude.ai/chat/abc123) into config.json
 *   5. Press Enter in this terminal
 */

import { chromium }  from 'playwright';
import { mkdirSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const DIR         = dirname(fileURLToPath(import.meta.url));
const BROWSER_DIR = join(DIR, 'browser-data');

mkdirSync(BROWSER_DIR, { recursive: true });

console.log('Opening browser...');
const ctx = await chromium.launchPersistentContext(BROWSER_DIR, {
  headless: false,
  viewport: null,
  args: ['--start-maximized'],
});

const page = await ctx.newPage();
await page.goto('https://claude.ai');

console.log('\n── Steps ─────────────────────────────────────────────');
console.log('1. Log in to Claude in the browser window');
console.log('2. Open (or create) the conversation to use for triggers');
console.log('3. Copy the conversation URL into config.json → conversation_url');
console.log('4. Come back here and press Enter to save and close');
console.log('──────────────────────────────────────────────────────\n');

await new Promise(resolve => {
  process.stdin.setRawMode?.(false);
  process.stdin.resume();
  process.stdin.once('data', resolve);
});

await ctx.close();
console.log('Session saved to browser-data/. You are ready to run trigger.js');
