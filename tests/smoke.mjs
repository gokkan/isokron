/* Load the real page in a real browser, click the map, and fail on any
 * console error. Needs a local server:
 *
 *   python -m http.server 8731 --directory public
 *   node tests/smoke.mjs http://127.0.0.1:8731/ [screenshot.png]
 *
 * Optional: needs playwright-core and a Chromium build on the machine, so it
 * is a developer convenience rather than part of the build.
 */
import { chromium } from 'playwright-core';

const url = process.argv[2] || 'http://127.0.0.1:8731/';
const shot = process.argv[3];

const browser = await chromium.launch({
  args: (process.env.SMOKE_ARGS ||
    '--use-angle=swiftshader --enable-unsafe-swiftshader').split(' '),
});
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });

const problems = [];
page.on('console', (m) => {
  if (m.type() === 'error') problems.push('console: ' + m.text());
});
page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));

// Start at Brunnsparken via the shareable URL, which also exercises the
// state round-trip; the map then centres there, so a click in the middle of
// the viewport lands on the same spot.
const origin = process.env.SMOKE_AT || '11.96683,57.70724';
await page.goto(url + '?snapshot&at=' + origin + '&t=0800&h=30',
                { waitUntil: 'load' });
await page.waitForFunction(
  () => document.getElementById('status').textContent.includes('ms,'),
  null, { timeout: 30000 });

console.log('timetable:', (await page.textContent('#metaDate')).trim());

const box = await page.locator('#map').boundingBox();
await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.5);

await page.waitForSelector('#stats:not([hidden])', { timeout: 15000 });
await page.waitForTimeout(7000);

// sweep for a stop under the pointer; the origin is one, so this should hit
let tip = null;
for (let i = 0; i < 60 && !tip; i++) {
  await page.mouse.move(box.x + box.width * 0.5 + (i % 10) * 3 - 15,
                        box.y + box.height * 0.5 + Math.floor(i / 10) * 3 - 9);
  tip = await page.evaluate(() => document.getElementById('tip').hidden
    ? null
    : document.getElementById('tipName').textContent + ' — ' +
      document.getElementById('tipWhen').textContent);
}
console.log('hover:', tip ?? 'no stop found under the sweep');
if (!tip) problems.push('hovering a reached stop showed no name');

const stats = await page.evaluate(() => ({
  status: document.getElementById('status').textContent,
  stops: document.getElementById('statStops').textContent,
  longest: document.getElementById('statLongest').textContent,
  far: document.getElementById('statFar').textContent,
  clock: document.getElementById('clockValue').textContent,
  painted: (() => {
    const c = document.getElementById('bloom');
    const g = c.getContext('2d');
    const d = g.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4 * 37) if (d[i] > 8) n++;
    return n;
  })(),
  url: location.search,
}));
console.log(stats);

if (shot) {
  await page.screenshot({ path: shot });
  console.log('screenshot:', shot);
}

if (stats.painted === 0) problems.push('overlay canvas is empty');
if (stats.clock === '0') problems.push('animation never advanced');

await browser.close();

if (problems.length) {
  console.error('\nFAIL');
  for (const p of problems) console.error('  ' + p);
  process.exit(1);
}
console.log('\nsmoke test passed');
