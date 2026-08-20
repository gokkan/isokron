/* Cross-check the browser search against the Python reference.
 *
 *   node tests/client.test.mjs public/data tmp/dump.json
 *
 * The same algorithm is written twice on purpose -- once in public/app.js so
 * it can run on a click, once in prep/verify.py so prep output can be checked
 * without a browser. This asserts the two still agree, stop for stop.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const dataDir = process.argv[2] || join(root, 'public', 'data');
const dumpFile = process.argv[3];

const require = createRequire(import.meta.url);
const app = require(join(root, 'public', 'app.js'));

const json = (name) =>
  JSON.parse(readFileSync(join(dataDir, name), 'utf-8'));
const bin = (name) => {
  const b = readFileSync(join(dataDir, name));
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
};

app.ingest({
  meta: json('meta.json'),
  stops: json('stops.json'),
  trips: json('trips.json'),
  connHeader: json('connections.json'),
  connBuf: bin('connections.bin'),
  fpHeader: json('footpaths.json'),
  fpBuf: bin('footpaths.bin'),
});

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
};

const D = app.D;
const conn = D.conn;

console.log('data sanity');
check('connections sorted by departure', (() => {
  for (let i = 1; i < conn.dep.length; i++) {
    if (conn.dep[i] < conn.dep[i - 1]) return false;
  }
  return true;
})());
check('stop indices in range', (() => {
  for (let i = 0; i < conn.from.length; i++) {
    if (conn.from[i] >= D.nStops || conn.to[i] >= D.nStops) return false;
  }
  return true;
})());
check('trip indices in range', (() => {
  for (let i = 0; i < conn.trip.length; i++) {
    if (conn.trip[i] >= D.nTrips) return false;
  }
  return true;
})());
check('arrival never precedes departure', (() => {
  for (let i = 0; i < conn.dep.length; i++) {
    if (conn.arr[i] < conn.dep[i]) return false;
  }
  return true;
})());
check('every stop maps into a stop area', (() => {
  if (D.group.length !== D.nStops) return false;
  for (let i = 0; i < D.nStops; i++) {
    if (D.group[i] < 0 || D.group[i] >= D.nGroups) return false;
  }
  return D.nGroups > 0 && D.nGroups <= D.nStops;
})(), `${D.nStops} stop points → ${D.nGroups} stop areas`);
// A feed where grouping changed nothing would mean parent_station went
// missing, and the map would silently go back to drawing everything twice.
check('stop areas actually collapse the per-direction duplicates',
      D.nGroups < D.nStops,
      `${D.nStops - D.nGroups} duplicate points collapsed`);
check('footpath CSR is well formed', (() => {
  const off = D.fp.offsets;
  if (off.length !== D.nStops + 1) return false;
  for (let i = 1; i < off.length; i++) if (off[i] < off[i - 1]) return false;
  return off[D.nStops] === D.fp.targets.length;
})());

if (dumpFile) {
  const ref = JSON.parse(readFileSync(dumpFile, 'utf-8'));
  console.log(`\ncross-check against ${dumpFile}`);
  const got = app.search(ref.lon, ref.lat, ref.t0, ref.horizon, ref.access);
  const mine = new Map();
  for (const i of got.reached) mine.set(i, D.arr[i]);

  check('same number of stops reached',
        mine.size === ref.stop.length,
        `js ${mine.size} vs py ${ref.stop.length}`);

  let worst = 0, missing = 0;
  for (let k = 0; k < ref.stop.length; k++) {
    const have = mine.get(ref.stop[k]);
    if (have === undefined) { missing++; continue; }
    worst = Math.max(worst, Math.abs(have - ref.arr[k]));
  }
  check('every stop the reference reached is reached here',
        missing === 0, `${missing} missing`);
  check('arrival times identical', worst === 0, `worst delta ${worst}s`);

  let modeMismatch = 0;
  for (let k = 0; k < ref.stop.length; k++) {
    if (mine.has(ref.stop[k]) && D.mode[ref.stop[k]] !== ref.mode[k]) {
      modeMismatch++;
    }
  }
  // verify.py labels a stop by the last leg, app.js by the leg that took the
  // most time, so a difference here is expected on multi-mode journeys.
  console.log(`  note  ${modeMismatch} of ${ref.stop.length} stops differ in `
              + 'dominant mode (last-leg vs longest-leg labelling)');

  const t = process.hrtime.bigint();
  app.search(ref.lon, ref.lat, ref.t0, ref.horizon, ref.access);
  const ms = Number(process.hrtime.bigint() - t) / 1e6;
  check('search under 100 ms', ms < 100, `${ms.toFixed(1)} ms`);
}

console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
process.exit(failures ? 1 : 0);
