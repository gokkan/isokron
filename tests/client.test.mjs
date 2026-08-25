/* Cross-check the browser search against the Python reference.
 *
 *   node tests/client.test.mjs public/data tmp/dump.json
 *
 * The same algorithm is written twice on purpose -- once in public/app.js so
 * it can run on a click, once in prep/verify.py so prep output can be checked
 * without a browser. This asserts the two still agree, stop for stop.
 */
import { existsSync, readFileSync } from 'node:fs';
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

const hasBarriers = existsSync(join(dataDir, 'barriers.json'));

app.ingest({
  meta: json('meta.json'),
  stops: json('stops.json'),
  trips: json('trips.json'),
  connHeader: json('connections.json'),
  connBuf: bin('connections.bin'),
  fpHeader: json('footpaths.json'),
  fpBuf: bin('footpaths.bin'),
  barrierHeader: hasBarriers ? json('barriers.json') : null,
  barrierBuf: hasBarriers ? bin('barriers.bin') : null,
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
  // Seeding is where the water check lives, so compare it directly: a
  // divergence then fails here instead of surfacing three transfers later
  // as an arrival-time delta nobody can trace back.
  if (ref.seeds !== undefined) {
    check('same stops seeded on foot', got.seeds === ref.seeds,
          `js ${got.seeds} vs py ${ref.seeds}`);
    check('same stops rejected for water', got.blocked === ref.blocked,
          `js ${got.blocked} vs py ${ref.blocked}`);
  }

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

  // The dashed outline is sampled from the same geometry the search used, so
  // a map that draws a circle straight over the water while refusing to seed
  // anything on the far side is a contradiction worth catching here.
  if (got.blocked > 0) {
    const budget = Math.min(ref.horizon, ref.access) * app.WALK_MPS;
    check('the drawn outline is cut where the walk was blocked',
          !!got.reach && Math.min(...got.reach) < budget - 1,
          got.reach ? `shortest ray ${Math.min(...got.reach).toFixed(0)} m of `
                      + `${budget.toFixed(0)} m` : 'no outline sampled');
  }

  const t = process.hrtime.bigint();
  app.search(ref.lon, ref.lat, ref.t0, ref.horizon, ref.access);
  const ms = Number(process.hrtime.bigint() - t) / 1e6;
  check('search under 100 ms', ms < 100, `${ms.toFixed(1)} ms`);
}

if (hasBarriers) {
  const B = D.barriers;
  const head = json('barriers.json');
  console.log('\nbarrier data');
  const off = new Uint32Array(
    bin('barriers.bin'),
    head.arrays.find((a) => a.name === 'offsets').offset,
    head.arrays.find((a) => a.name === 'offsets').length);
  check('offsets start at zero and rise', (() => {
    if (off[0] !== 0) return false;
    for (let i = 1; i < off.length; i++) if (off[i] <= off[i - 1]) return false;
    return off[off.length - 1] === B.lon.length;
  })(), `${off.length - 1} lines over ${B.lon.length} points`);
  check('every polyline has at least two points', (() => {
    for (let i = 1; i < off.length; i++) if (off[i] - off[i - 1] < 2) return false;
    return true;
  })());
  check('every coordinate is finite', (() => {
    for (let i = 0; i < B.lon.length; i++) {
      if (!Number.isFinite(B.lon[i]) || !Number.isFinite(B.lat[i])) return false;
    }
    return true;
  })());
  check('every bridge has a positive length', (() => {
    for (let i = 0; i < B.nGates; i++) if (!(B.gateLen[i] > 0)) return false;
    return true;
  })(), `${B.nGates} bridges`);
}

// The two deliberate decisions in the geometry, asserted so that a later
// reader cannot quietly "fix" either of them.
console.log('\ncrossing rules');
{
  const line = (ax, ay, bx, by) => ({
    lon: Float32Array.from([ax, bx]), lat: Float32Array.from([ay, by]),
    offsets: Uint32Array.from([0, 2]),
    gate_a_lon: new Float32Array(0), gate_a_lat: new Float32Array(0),
    gate_b_lon: new Float32Array(0), gate_b_lat: new Float32Array(0),
    gate_len: new Float32Array(0),
  });
  const B = app.buildBarriers({}, line(0, 0, 10, 0));
  const kx = 1, ky = 1;
  const hit = (ax, ay, bx, by, slack = 0) => {
    const n = app.collectSegments(B, (ax + bx) / 2, (ay + by) / 2, 1e6, kx, ky);
    return app.crosses(B, n, ax, ay, bx, by, kx, ky, slack);
  };
  check('a line straight across is blocked', hit(5, -1, 5, 1));
  check('a line alongside is not', hit(1, 1, 9, 1) === false);
  check('touching an end is not a crossing', hit(5, -1, 5, 0) === false);
  check('slack forgives a crossing next to an end',
        hit(5, -0.5, 5, 0.4, 0.5) === false);
  check('slack does not forgive one in the middle',
        hit(5, -3, 5, 3, 0.5) === true);
}

console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
process.exit(failures ? 1 : 0);
