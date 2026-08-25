/* Isochrone map for Västtrafik — everything runs in the browser.
 *
 * Three parts, in order: load the prepared arrays, run a connection scan on
 * click, then paint one disc per reached stop whose radius is the walking
 * budget left over at the current minute. The search mirrors prep/verify.py;
 * change one and change the other.
 */
'use strict';

const DATA = 'data/';
const WALK_MPS = 5000 / 3600;      // 5 km/h, as the crow flies
const MIN_CHANGE = 60;             // seconds needed to board a different trip
const ACCESS_DEFAULT_MIN = 10;     // how far people are assumed to walk to a stop
// The offered horizons, longest last. The data window must extend past the
// latest selectable departure by HORIZONS[last], or a late start runs into the
// edge of the data and the result gets silently truncated.
const HORIZONS = [15, 30, 45, 60, 90, 120];
const LEG_ACCESS = -2;             // reached on foot from the click point
const LEG_WALK = -1;               // reached on foot from another stop
const INF = 0x7fffffff;
const EARTH_CIRC = 40075016.686;
const EARTH_R = 6371008.8;
const TAU = 6.283185307179586;
const SHORE_SLACK = 35;            // see the barrier section for what this is
const REACH_RAYS = 128;            // directions the drawn walk outline samples

const el = (id) => document.getElementById(id);

function say(text, isError) {
  const box = el('status');
  box.textContent = text;
  box.classList.toggle('error', !!isError);
}

/* ---------------------------------------------------------------- loading */

const DTYPES = { uint8: Uint8Array, uint16: Uint16Array, uint32: Uint32Array,
                 int32: Int32Array, float32: Float32Array };

/* Prefer the pre-gzipped copy: it makes the transfer size independent of
 * whether the host bothers to compress application/octet-stream. Some hosts
 * set Content-Encoding on .gz and the browser unwraps it for us, so sniff the
 * magic bytes rather than assuming. */
async function fetchBinary(name) {
  let res = await fetch(DATA + name + '.gz');
  if (!res.ok) res = await fetch(DATA + name);
  if (!res.ok) throw new Error(name + ': HTTP ' + res.status);
  const buf = await res.arrayBuffer();
  const head = new Uint8Array(buf, 0, Math.min(2, buf.byteLength));
  if (head[0] === 0x1f && head[1] === 0x8b) {
    if (!('DecompressionStream' in self)) {
      const plain = await fetch(DATA + name);
      if (!plain.ok) throw new Error(name + ': HTTP ' + plain.status);
      return plain.arrayBuffer();
    }
    const stream = new Blob([buf]).stream()
      .pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).arrayBuffer();
  }
  return buf;
}

async function fetchJSON(name) {
  const res = await fetch(DATA + name);
  if (!res.ok) throw new Error(name + ': HTTP ' + res.status);
  return res.json();
}

/* Slice one blob into the typed arrays its header describes. */
function unpack(buffer, header) {
  const out = {};
  for (const part of header.arrays) {
    const Ctor = DTYPES[part.dtype];
    if (!Ctor) throw new Error('unknown dtype ' + part.dtype);
    out[part.name] = part.offset % Ctor.BYTES_PER_ELEMENT === 0
      ? new Ctor(buffer, part.offset, part.length)
      // misaligned offsets cannot be viewed in place; copy those few bytes
      : new Ctor(buffer.slice(part.offset,
          part.offset + part.length * Ctor.BYTES_PER_ELEMENT));
  }
  return out;
}

const D = {};   // everything loaded, filled in by boot()

/* The barrier files are the one part of the data that may simply not be
 * there: build without them and walking is as the crow flies, which is what
 * it was before they existed. Absence is not an error, so this cannot join
 * the Promise.all above. */
async function loadOptional(jsonName, binName) {
  try {
    const header = await fetchJSON(jsonName);
    return { header, buf: await fetchBinary(binName) };
  } catch (err) {
    return null;
  }
}

async function loadData() {
  const [meta, stops, trips, connHeader, fpHeader] = await Promise.all(
    ['meta.json', 'stops.json', 'trips.json', 'connections.json',
     'footpaths.json'].map(fetchJSON));
  const [connBuf, fpBuf, barriers] = await Promise.all(
    [fetchBinary('connections.bin'), fetchBinary('footpaths.bin'),
     loadOptional('barriers.json', 'barriers.bin')]);
  ingest({ meta, stops, trips, connHeader, connBuf, fpHeader, fpBuf,
           barrierHeader: barriers && barriers.header,
           barrierBuf: barriers && barriers.buf });
}

/* Turn the loaded files into the flat arrays the search wants. Split out from
 * loadData so tests/client.test.mjs can feed it straight off disk. */
function ingest({ meta, stops, trips, connHeader, connBuf, fpHeader, fpBuf,
                  barrierHeader, barrierBuf }) {
  D.meta = meta;
  D.stops = stops;
  D.trips = trips;
  D.header = connHeader;
  D.conn = unpack(connBuf, connHeader);
  D.fp = unpack(fpBuf, fpHeader);
  D.barriers = (barrierHeader && barrierBuf)
    ? buildBarriers(barrierHeader, unpack(barrierBuf, barrierHeader)) : null;
  D.nStops = stops.count;
  D.nTrips = trips.trip_route.length;
  D.nCats = trips.categories.length;
  D.tripRoute = Uint32Array.from(trips.trip_route);
  D.routeCat = Uint8Array.from(trips.routes.category);

  // Web Mercator position and a metres-to-mercator factor per stop, so a
  // walking radius in metres becomes pixels without a per-frame trig call.
  D.mercX = new Float64Array(D.nStops);
  D.mercY = new Float64Array(D.nStops);
  D.mPerMerc = new Float64Array(D.nStops);
  for (let i = 0; i < D.nStops; i++) {
    const lat = stops.lat[i], lon = stops.lon[i];
    const phi = lat * Math.PI / 180;
    D.mercX[i] = (lon + 180) / 360;
    D.mercY[i] = 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI);
    D.mPerMerc[i] = 1 / (EARTH_CIRC * Math.cos(phi));
  }

  // Stop areas: the search runs on stop points, but a passenger sees one
  // stop where the feed sees a boarding point per direction. Everything
  // drawn is drawn per area, or the map doubles every dot and every line.
  const G = stops.groups;
  D.group = Int32Array.from(stops.group);
  D.nGroups = G.count;
  D.gMercX = new Float64Array(D.nGroups);
  D.gMercY = new Float64Array(D.nGroups);
  D.gMPerMerc = new Float64Array(D.nGroups);
  for (let g = 0; g < D.nGroups; g++) {
    const phi = G.lat[g] * Math.PI / 180;
    D.gMercX[g] = (G.lon[g] + 180) / 360;
    D.gMercY[g] = 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI);
    D.gMPerMerc[g] = 1 / (EARTH_CIRC * Math.cos(phi));
  }
  D.gArr = new Int32Array(D.nGroups);
  D.gMode = new Int8Array(D.nGroups);
  D.gOrder = new Uint32Array(D.nGroups);

  // scratch, reused across searches
  D.arr = new Int32Array(D.nStops);
  D.mode = new Int8Array(D.nStops);       // dominant mode of the whole journey
  D.modeTime = new Uint16Array(D.nStops * D.nCats);
  D.prev = new Int32Array(D.nStops);      // stop the last leg started from
  D.prevTime = new Int32Array(D.nStops);  // when that leg left
  D.legMode = new Int8Array(D.nStops);    // mode of that leg alone
  D.boarded = new Uint8Array(D.nTrips);
  D.order = new Uint32Array(D.nStops);
}

/* --------------------------------------------------------------- barriers */

/* Water you cannot walk across, and the bridges where you can. The rules are
 * prep/barriers.py written a second time; change one and change the other,
 * exactly as with the search itself. Two of them:
 *
 *   1. A walk is blocked if the straight line between its ends crosses a
 *      barrier. Blocked, it may instead reach a bridge landing, pay the
 *      bridge, and go on from the far landing — both legs judged the same way.
 *   2. A crossing within SHORE_SLACK of an end does not count. Feed
 *      coordinates put a quayside stop a few metres into the water often
 *      enough that without this, stops along Stenpiren would be unreachable
 *      from every direction at once — a silent, total failure. Slack at one
 *      end never rescues a real crossing: the far bank is hundreds of metres
 *      from both ends.
 *
 * Crossing tests run in degrees. Segment intersection survives any affine map
 * and lon/lat → metres is a diagonal scaling, so the answer is the same and no
 * projection has to be agreed on with the search, which measures its distances
 * about the click latitude and must go on doing exactly that.
 */

function buildBarriers(header, a) {
  // one entry per segment, holding the index of its first point
  let count = 0;
  for (let l = 0; l + 1 < a.offsets.length; l++) {
    count += Math.max(0, a.offsets[l + 1] - a.offsets[l] - 1);
  }
  const seg = new Uint32Array(count);
  let s = 0;
  for (let l = 0; l + 1 < a.offsets.length; l++) {
    for (let p = a.offsets[l]; p + 1 < a.offsets[l + 1]; p++) seg[s++] = p;
  }
  return {
    lon: a.lon, lat: a.lat, seg,
    gateALon: a.gate_a_lon, gateALat: a.gate_a_lat,
    gateBLon: a.gate_b_lon, gateBLat: a.gate_b_lat, gateLen: a.gate_len,
    nGates: a.gate_len.length,
    meta: header,
    // scratch, so a search allocates nothing: which segments the walking disc
    // can reach at all, filled once per click
    cand: new Uint32Array(count),
    reach: new Float32Array(REACH_RAYS),
  };
}

/* Segments the disc around a click can possibly touch. Everywhere in Västra
 * Götaland that is not beside water this returns zero, and the whole feature
 * costs one pass over a few thousand bounding boxes and nothing more. */
function collectSegments(B, lon, lat, radius, kx, ky) {
  const dLon = radius / kx, dLat = radius / ky;
  const minLon = lon - dLon, maxLon = lon + dLon;
  const minLat = lat - dLat, maxLat = lat + dLat;
  const bl = B.lon, ba = B.lat, seg = B.seg, cand = B.cand;
  let n = 0;
  for (let k = 0; k < seg.length; k++) {
    const p = seg[k];
    const x0 = bl[p], x1 = bl[p + 1], y0 = ba[p], y1 = ba[p + 1];
    if ((x0 < minLon && x1 < minLon) || (x0 > maxLon && x1 > maxLon)) continue;
    if ((y0 < minLat && y1 < minLat) || (y0 > maxLat && y1 > maxLat)) continue;
    cand[n++] = p;
  }
  return n;
}

/* Where a-b crosses c-d, as a fraction along a-b, or -1 for no crossing.
 * Proper crossings only: segments that merely touch at an endpoint do not
 * count, and neither does collinear overlap — that is walking along a bank,
 * not across it. */
function segHit(ax, ay, bx, by, cx, cy, dx, dy) {
  const rx = bx - ax, ry = by - ay;
  const sx = dx - cx, sy = dy - cy;
  const denom = rx * sy - ry * sx;
  if (denom === 0) return -1;
  const qx = cx - ax, qy = cy - ay;
  const t = (qx * sy - qy * sx) / denom;
  if (t <= 0 || t >= 1) return -1;
  const u = (qx * ry - qy * rx) / denom;
  if (u <= 0 || u >= 1) return -1;
  return t;
}

function crosses(B, nCand, alon, alat, blon, blat, kx, ky, slack) {
  const bl = B.lon, ba = B.lat, cand = B.cand;
  const slack2 = slack * slack;
  for (let k = 0; k < nCand; k++) {
    const p = cand[k];
    const t = segHit(alon, alat, blon, blat,
                     bl[p], ba[p], bl[p + 1], ba[p + 1]);
    if (t < 0) continue;
    if (slack2 > 0) {
      const hx = alon + (blon - alon) * t;
      const hy = alat + (blat - alat) * t;
      const dax = (alon - hx) * kx, day = (alat - hy) * ky;
      if (dax * dax + day * day <= slack2) continue;
      const dbx = (blon - hx) * kx, dby = (blat - hy) * ky;
      if (dbx * dbx + dby * dby <= slack2) continue;
    }
    return true;
  }
  return false;
}

/* Where a blocked walk can pick up again, having paid for a bridge: the far
 * landing, and the metres already spent reaching the near one and crossing.
 * Sorted by cost, then by index — prep/barriers.py sorts the same way, and a
 * tie has to break identically or the two answers drift apart. */
function gateExits(B, nCand, lon, lat, budget, kx, ky, slack) {
  const out = [];
  for (let gi = 0; gi < B.nGates; gi++) {
    for (let near = 0; near < 2; near++) {
      const nLon = near === 0 ? B.gateALon[gi] : B.gateBLon[gi];
      const nLat = near === 0 ? B.gateALat[gi] : B.gateBLat[gi];
      const fLon = near === 0 ? B.gateBLon[gi] : B.gateALon[gi];
      const fLat = near === 0 ? B.gateBLat[gi] : B.gateALat[gi];
      const dx = (nLon - lon) * kx, dy = (nLat - lat) * ky;
      const cost = Math.sqrt(dx * dx + dy * dy) + B.gateLen[gi];
      if (cost > budget) continue;
      if (crosses(B, nCand, lon, lat, nLon, nLat, kx, ky, slack)) continue;
      out.push({ cost, rank: gi * 2 + near, lon: fLon, lat: fLat });
    }
  }
  out.sort((a, b) => (a.cost - b.cost) || (a.rank - b.rank));
  return out;
}

/* Metres on foot from a to b, or -1 if there is no way inside budget.
 * Straight-line unless water is in the way, and then over one bridge. Two
 * bridges in one walk is not searched for: inside a twenty minute budget that
 * does not happen. */
function walkDistance(B, nCand, exits, alon, alat, blon, blat, direct, budget,
                      kx, ky, slack) {
  if (!crosses(B, nCand, alon, alat, blon, blat, kx, ky, slack)) return direct;
  let best = -1;
  for (let k = 0; k < exits.length; k++) {
    const e = exits[k];
    if (e.cost > budget || (best >= 0 && e.cost >= best)) break;
    const dx = (blon - e.lon) * kx, dy = (blat - e.lat) * ky;
    const total = e.cost + Math.sqrt(dx * dx + dy * dy);
    if (total > budget || (best >= 0 && total >= best)) continue;
    if (crosses(B, nCand, e.lon, e.lat, blon, blat, kx, ky, slack)) continue;
    best = total;
  }
  return best;
}

/* How far the walk gets in each of REACH_RAYS directions before it hits
 * water. Sampled once per search, not per frame; the animation only clips it
 * shorter as the clock runs. No slack here: this is a drawing, and a ray that
 * shrugged off the bank it starts on would draw a circle straight across the
 * river. */
function reachProfile(B, nCand, out, lon, lat, budget, kx, ky) {
  const bl = B.lon, ba = B.lat, cand = B.cand;
  for (let r = 0; r < REACH_RAYS; r++) {
    const angle = r * TAU / REACH_RAYS;
    // a unit vector in metres, expressed in degrees
    const ux = Math.cos(angle) / kx, uy = Math.sin(angle) / ky;
    let best = budget;
    for (let k = 0; k < nCand; k++) {
      const p = cand[k];
      const t = segHit(lon, lat, lon + ux * budget, lat + uy * budget,
                       bl[p], ba[p], bl[p + 1], ba[p + 1]);
      if (t >= 0 && t * budget < best) best = t * budget;
    }
    out[r] = best;
  }
  return out;
}

/* ----------------------------------------------------------------- search */

/* Connection scan. `t0` and the result are seconds from the data window's
 * start. Deviates from plain CSA in one way: a trip already boarded may be
 * stayed on for free, while boarding a fresh one costs MIN_CHANGE. Without
 * that, the scan happily changes trains in zero seconds. */
function search(lon, lat, t0, horizon, accessSec) {
  const n = D.nStops;
  const arr = D.arr, mode = D.mode, modeTime = D.modeTime;
  const prev = D.prev, prevTime = D.prevTime, legMode = D.legMode;
  const nCats = D.nCats;
  arr.fill(INF);
  mode.fill(-1);
  modeTime.fill(0);
  prev.fill(-1);
  legMode.fill(LEG_ACCESS);
  D.boarded.fill(0);

  // How far you will walk to a first stop is a preference, not a fact, and it
  // changes the answer more than anything else on the panel — so it is the
  // caller's to set, and the map says which value it used.
  const seedRadius = Math.min(horizon, accessSec) * WALK_MPS;
  const phi = lat * Math.PI / 180;
  const kx = EARTH_R * Math.cos(phi) * Math.PI / 180;
  const ky = EARTH_R * Math.PI / 180;

  // Water between the click point and a stop makes the crow's distance a
  // lie. Narrow the barriers down to the ones this disc can reach first: a
  // click anywhere away from water leaves nCand at zero and the rest of the
  // walk stays exactly the arithmetic it always was.
  const B = D.barriers;
  let nCand = 0, exits = null, reach = null;
  if (B) {
    nCand = collectSegments(B, lon, lat, seedRadius, kx, ky);
    if (nCand) {
      exits = gateExits(B, nCand, lon, lat, seedRadius, kx, ky, SHORE_SLACK);
      reach = reachProfile(B, nCand, B.reach, lon, lat, seedRadius, kx, ky);
    }
  }

  let seeds = 0, blocked = 0;
  let nearest = Infinity;
  for (let i = 0; i < n; i++) {
    const dx = (D.stops.lon[i] - lon) * kx;
    const dy = (D.stops.lat[i] - lat) * ky;
    const d = Math.sqrt(dx * dx + dy * dy);
    if (d < nearest) nearest = d;
    if (d > seedRadius) continue;
    let w = d;
    if (nCand) {
      w = walkDistance(B, nCand, exits, lon, lat,
                       D.stops.lon[i], D.stops.lat[i], d, seedRadius,
                       kx, ky, SHORE_SLACK);
      // Nothing to fall back on when every nearby stop turns out to be
      // across the water. That is not a failure to answer, it is the
      // answer, and runFrom says so rather than reverting to the crow.
      if (w < 0) { blocked++; continue; }
    }
    arr[i] = t0 + Math.ceil(w / WALK_MPS);
    prevTime[i] = t0;
    seeds++;
  }

  const limit = t0 + horizon;
  const cFrom = D.conn.from, cTo = D.conn.to, cDep = D.conn.dep;
  const cArr = D.conn.arr, cTrip = D.conn.trip;
  const fpOff = D.fp.offsets, fpTgt = D.fp.targets, fpSec = D.fp.seconds;
  const boarded = D.boarded, tripRoute = D.tripRoute, routeCat = D.routeCat;
  const total = cDep.length;

  // first connection that could still be caught
  let lo = 0, hi = total;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cDep[mid] < t0) lo = mid + 1; else hi = mid;
  }

  let scanned = 0;
  for (let c = lo; c < total; c++) {
    const dep = cDep[c];
    if (dep > limit) break;
    scanned++;
    const trip = cTrip[c];
    if (!boarded[trip]) {
      if (arr[cFrom[c]] + MIN_CHANGE > dep) continue;
      boarded[trip] = 1;
    }
    const dst = cTo[c];
    const a = cArr[c];
    if (a >= arr[dst]) continue;

    arr[dst] = a;
    // remember the leg that got us here, so the animation can draw it
    prev[dst] = cFrom[c];
    prevTime[dst] = dep;
    // carry the journey's per-mode time along and add this hop to it
    const src = cFrom[c] * nCats, base = dst * nCats;
    for (let k = 0; k < nCats; k++) modeTime[base + k] = modeTime[src + k];
    const cat = routeCat[tripRoute[trip]];
    legMode[dst] = cat;
    modeTime[base + cat] = Math.min(65535, modeTime[base + cat] + (a - dep));
    const best = dominant(modeTime, base, nCats);
    mode[dst] = best < 0 ? cat : best;

    for (let e = fpOff[dst], end = fpOff[dst + 1]; e < end; e++) {
      const t = a + fpSec[e];
      const tgt = fpTgt[e];
      if (t < arr[tgt]) {
        arr[tgt] = t;
        prev[tgt] = dst;
        prevTime[tgt] = a;
        legMode[tgt] = LEG_WALK;
        const tb = tgt * nCats;
        for (let k = 0; k < nCats; k++) modeTime[tb + k] = modeTime[base + k];
        mode[tgt] = mode[dst];
      }
    }
  }

  // stops actually reached, earliest first — the paint loop relies on it
  const order = D.order;
  let count = 0, longest = 0, farthest = 0;
  for (let i = 0; i < n; i++) {
    if (arr[i] <= limit) {
      order[count++] = i;
      if (arr[i] - t0 > longest) longest = arr[i] - t0;
      const dx = (D.stops.lon[i] - lon) * kx;
      const dy = (D.stops.lat[i] - lat) * ky;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d > farthest) farthest = d;
    }
  }
  const reached = order.subarray(0, count);
  reached.sort((a, b) => arr[a] - arr[b]);

  // collapse to stop areas for drawing and for the counter
  const gArr = D.gArr, gMode = D.gMode, group = D.group;
  gArr.fill(INF);
  for (let k = 0; k < reached.length; k++) {
    const i = reached[k], g = group[i];
    if (arr[i] < gArr[g]) { gArr[g] = arr[i]; gMode[g] = mode[i]; }
  }
  let gCount = 0;
  for (let g = 0; g < D.nGroups; g++) {
    if (gArr[g] <= limit) D.gOrder[gCount++] = g;
  }
  const groups = D.gOrder.subarray(0, gCount);
  groups.sort((a, b) => gArr[a] - gArr[b]);

  // The far side of every bridge the walk can pay for, with the metres it
  // cost to get there, so the paint loop can draw the reach beyond it.
  const lobes = [];
  if (nCand) {
    for (let k = 0; k < exits.length; k++) {
      const e = exits[k];
      if (seedRadius - e.cost < 60) continue;      // too little to be visible
      const lPhi = e.lat * Math.PI / 180;
      lobes.push({
        cost: e.cost,
        reach: reachProfile(B, nCand, new Float32Array(REACH_RAYS),
                            e.lon, e.lat, seedRadius - e.cost, kx, ky),
        mercX: (e.lon + 180) / 360,
        mercY: 0.5 - Math.log(Math.tan(Math.PI / 4 + lPhi / 2)) / (2 * Math.PI),
        mPerMerc: 1 / (EARTH_CIRC * Math.cos(lPhi)),
      });
    }
  }

  return {
    reached, count, groups, gCount, longest, farthest, seeds, scanned, nearest,
    blocked, reach, lobes,
    t0, horizon, accessSec, lon, lat,
    mercX: (lon + 180) / 360,
    mercY: 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI),
    mPerMerc: 1 / (EARTH_CIRC * Math.cos(phi)),
  };
}

function dominant(modeTime, base, nCats) {
  let best = -1, bestVal = 0;
  for (let k = 0; k < nCats; k++) {
    if (modeTime[base + k] > bestVal) { bestVal = modeTime[base + k]; best = k; }
  }
  return best;
}

/* --------------------------------------------------------------- painting */

let view, vctx, buf, bctx;   // set up in sizeCanvas, once the map exists
let colors = [];
let walkColor = '#e8912a';
let holeColor = '#ffffff';
let ringColor = '#16181d';
let areaAlpha = 0.30;
let dpr = 1;

function readColors() {
  const css = getComputedStyle(document.documentElement);
  const pick = (n) => css.getPropertyValue('--' + n).trim() || '#888';
  colors = (D.trips.categories || []).map(pick);
  walkColor = pick('walk');
  holeColor = pick('hole');
  ringColor = pick('ring');
  areaAlpha = Number(css.getPropertyValue('--area-alpha')) || 0.30;
}

function sizeCanvas() {
  if (!view) {
    view = el('bloom');
    vctx = view.getContext('2d');
    buf = document.createElement('canvas');
    bctx = buf.getContext('2d', { willReadFrequently: true });
  }
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = map.getContainer().clientWidth;
  const h = map.getContainer().clientHeight;
  for (const c of [view, buf]) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    c.style.width = w + 'px';
    c.style.height = h + 'px';
  }
}

/* Screen position of a mercator coordinate, without touching MapLibre
 * internals: project two known points and solve the affine transform. Valid
 * only while bearing and pitch stay at zero, which is why both are disabled. */
function calibrate() {
  const toLngLat = (mx, my) => [
    mx * 360 - 180,
    (Math.atan(Math.sinh(Math.PI * (1 - 2 * my))) * 180) / Math.PI,
  ];
  const a = map.project(toLngLat(0.50, 0.30));
  const b = map.project(toLngLat(0.60, 0.40));
  const kx = (b.x - a.x) / 0.10;
  const ky = (b.y - a.y) / 0.10;
  return { kx, ky, ox: a.x - 0.50 * kx, oy: a.y - 0.30 * ky };
}

let current = null;   // last search result
let frameTime = 0;    // seconds from window start, the minute being drawn
let hoverGroup = -1;  // stop area under the pointer, -1 for none

const MODE_LABEL = {
  tram: 'spårvagn', bus: 'buss', rail: 'tåg', ferry: 'färja', other: 'övrigt',
};

/* Nearest lit stop area to a point on screen, in CSS pixels. Only the stops
 * this search reached are candidates, which keeps the scan short. */
function pickGroup(px, py) {
  if (!current) return -1;
  const { kx, ky, ox, oy } = calibrate();
  const groups = current.groups, gArr = D.gArr;
  let best = -1, bestDist = 12 * 12;
  for (let k = 0; k < groups.length; k++) {
    const g = groups[k];
    if (gArr[g] > frameTime) break;      // sorted, so nothing later is lit
    const dx = D.gMercX[g] * kx + ox - px;
    const dy = D.gMercY[g] * ky + oy - py;
    const d = dx * dx + dy * dy;
    if (d < bestDist) { bestDist = d; best = g; }
  }
  return best;
}

function showTip(g, px, py) {
  const tip = el('tip');
  if (g < 0) { tip.hidden = true; return; }
  const minutes = Math.round((D.gArr[g] - current.t0) / 60);
  const cat = D.gMode[g] < 0 ? null : D.trips.categories[D.gMode[g]];
  el('tipName').textContent = D.stops.groups.name[g];
  el('tipWhen').textContent = minutes + ' min' +
    (cat ? ' · ' + (MODE_LABEL[cat] || cat) : ' · till fots');
  tip.hidden = false;
  // flip to the other side of the cursor rather than run off the map
  const box = tip.getBoundingClientRect();
  const w = map.getContainer().clientWidth;
  const left = px + 14 + box.width > w ? px - 14 - box.width : px + 14;
  tip.style.left = Math.max(4, left) + 'px';
  tip.style.top = Math.max(4, py - box.height - 12) + 'px';
}

function paint() {
  vctx.setTransform(1, 0, 0, 1, 0, 0);
  vctx.clearRect(0, 0, view.width, view.height);
  if (!current) return;

  const frame = calibrate();
  if (showWalkArea) paintWalkArea(frame);
  paintNetwork(frame);
}

/* The journey tree: every leg grows along its own segment between the minute
 * it departs and the minute it arrives, and the stop lights up when it lands.
 * Segments are batched into one path per colour — three thousand separate
 * strokes a frame is what makes this kind of thing stutter. */
function paintNetwork({ kx, ky, ox, oy }) {
  const arr = D.arr, prev = D.prev, group = D.group;
  const prevTime = D.prevTime, legMode = D.legMode;
  const gArr = D.gArr, gMode = D.gMode;
  const reached = current.reached;
  const W = view.width, H = view.height;
  const gx = (g) => (D.gMercX[g] * kx + ox) * dpr;
  const gy = (g) => (D.gMercY[g] * ky + oy) * dpr;

  const legs = colors.map(() => new Path2D());
  const dots = colors.map(() => new Path2D());
  const walkLegs = new Path2D();
  const walkDots = new Path2D();
  const dotR = 2.5 * dpr;
  const walkR = 1.9 * dpr;
  // Both directions of a segment, and every platform pair along it, collapse
  // to the same line between the same two areas. Draw it once.
  const seen = new Set();
  let live = 0;

  for (let k = 0; k < reached.length; k++) {
    const i = reached[k];
    const start = prevTime[i];
    if (start > frameTime) continue;          // this leg has not left yet
    const j = prev[i];
    if (j < 0) continue;
    const ga = group[j], gb = group[i];
    if (ga === gb) continue;                  // a walk inside one stop area
    const key = ga < gb ? ga * D.nGroups + gb : gb * D.nGroups + ga;
    if (seen.has(key)) continue;
    seen.add(key);

    const x0 = gx(ga), y0 = gy(ga), x1 = gx(gb), y1 = gy(gb);
    if ((x0 < 0 && x1 < 0) || (y0 < 0 && y1 < 0) ||
        (x0 > W && x1 > W) || (y0 > H && y1 > H)) continue;
    const span = arr[i] - start;
    const f = span > 0 ? Math.min(1, (frameTime - start) / span) : 1;
    const path = legMode[i] === LEG_WALK ? walkLegs
      : (legs[legMode[i]] || walkLegs);
    path.moveTo(x0, y0);
    path.lineTo(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f);
    if (f < 1) live++;
  }

  const groups = current.groups;
  for (let k = 0; k < groups.length; k++) {
    const g = groups[k];
    if (gArr[g] > frameTime) break;           // sorted, so none later has landed
    const x = gx(g), y = gy(g);
    if (x < -dotR || y < -dotR || x > W + dotR || y > H + dotR) continue;
    if (gMode[g] < 0) {
      walkDots.moveTo(x + walkR, y);
      walkDots.arc(x, y, walkR, 0, 6.283185307179586);
    } else {
      const path = dots[gMode[g]] || walkDots;
      path.moveTo(x + dotR, y);
      path.arc(x, y, dotR, 0, 6.283185307179586);
    }
  }

  // Where the walk from the click point runs out. An outline, not a fill:
  // the point of the map is the network, and a filled disc over the middle
  // of it competes for exactly the attention the lines need.
  const walked = Math.min(frameTime - current.t0, current.accessSec) * WALK_MPS;
  if (walked > 0) {
    vctx.globalAlpha = 0.85;
    vctx.strokeStyle = walkColor;
    vctx.lineWidth = 1.4 * dpr;
    vctx.setLineDash([5 * dpr, 5 * dpr]);
    traceReach(current, current.reach, walked, kx, ky, ox, oy);
    // and once more beyond each bridge the walk can afford, which is the
    // part of the answer a single circle can never show
    for (let k = 0; k < current.lobes.length; k++) {
      const lobe = current.lobes[k];
      if (walked > lobe.cost) {
        traceReach(lobe, lobe.reach, walked - lobe.cost, kx, ky, ox, oy);
      }
    }
    vctx.setLineDash([]);
  }

  vctx.lineCap = 'round';
  vctx.lineJoin = 'round';

  vctx.globalAlpha = 0.5;
  vctx.strokeStyle = walkColor;
  vctx.lineWidth = 1.3 * dpr;
  vctx.stroke(walkLegs);

  vctx.globalAlpha = 0.85;
  vctx.lineWidth = 2.3 * dpr;
  for (let c = 0; c < legs.length; c++) {
    vctx.strokeStyle = colors[c];
    vctx.stroke(legs[c]);
  }

  vctx.globalAlpha = 1;
  for (let c = 0; c < dots.length; c++) {
    vctx.fillStyle = colors[c];
    vctx.fill(dots[c]);
  }
  // stops you only walked to: hollow, so they read as "not yet a journey"
  vctx.fillStyle = holeColor;
  vctx.fill(walkDots);
  vctx.strokeStyle = walkColor;
  vctx.lineWidth = 1.2 * dpr;
  vctx.stroke(walkDots);

  if (hoverGroup >= 0 && gArr[hoverGroup] <= frameTime) {
    vctx.beginPath();
    vctx.arc(gx(hoverGroup), gy(hoverGroup), 6.5 * dpr, 0, 6.283185307179586);
    vctx.strokeStyle = ringColor;
    vctx.lineWidth = 1.7 * dpr;
    vctx.stroke();
  }
  return live;
}

/* One dashed outline of how far the walk has got. With no barrier data in
 * range this is the circle it has always been; with barriers it is the same
 * circle sampled in REACH_RAYS directions and cut short wherever the bank
 * comes first, so the map stops claiming a shore it cannot cross. Mercator y
 * grows southward and so does the canvas, hence the negated sine. */
function traceReach(centre, reach, walked, kx, ky, ox, oy) {
  const cx = (centre.mercX * kx + ox) * dpr;
  const cy = (centre.mercY * ky + oy) * dpr;
  vctx.beginPath();
  if (!reach) {
    vctx.arc(cx, cy, walked * centre.mPerMerc * kx * dpr, 0, TAU);
    vctx.stroke();
    return;
  }
  const unit = centre.mPerMerc * dpr;
  for (let r = 0; r < REACH_RAYS; r++) {
    const angle = r * TAU / REACH_RAYS;
    const d = Math.min(walked, reach[r]);
    const x = cx + Math.cos(angle) * d * unit * kx;
    const y = cy - Math.sin(angle) * d * unit * ky;
    if (r === 0) vctx.moveTo(x, y); else vctx.lineTo(x, y);
  }
  vctx.closePath();
  vctx.stroke();
}

/* The old rendering, kept behind a checkbox: how far you could walk from
 * every stop with the time left over. Honest about area, hopeless about
 * showing which way you actually went. */
function paintWalkArea({ kx, ky, ox, oy }) {
  const gArr = D.gArr, gMode = D.gMode;
  const groups = current.groups;
  const W = buf.width, H = buf.height;
  bctx.setTransform(1, 0, 0, 1, 0, 0);

  // One pass per mode. Within a pass the discs are opaque, so overlapping
  // circles of one colour leave no seam; the passes are then composited on
  // top of each other, so circles of different colours do blend. Drawing
  // every disc into a single opaque buffer instead — which is what this used
  // to do — means a later circle punches a hard edge through an earlier one.
  for (let layer = -1; layer < colors.length; layer++) {
    const fill = layer < 0 ? walkColor : colors[layer];
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

    bctx.clearRect(0, 0, W, H);
    bctx.fillStyle = fill;
    bctx.beginPath();
    for (let k = 0; k < groups.length; k++) {
      const g = groups[k];
      const budget = frameTime - gArr[g];
      if (budget <= 0) break;       // sorted by arrival, so nothing later fits
      if ((gMode[g] < 0 ? -1 : gMode[g]) !== layer) continue;
      const r = Math.max(budget * WALK_MPS * D.gMPerMerc[g] * kx * dpr,
                         1.1 * dpr);
      const x = (D.gMercX[g] * kx + ox) * dpr;
      const y = (D.gMercY[g] * ky + oy) * dpr;
      if (x + r < 0 || y + r < 0 || x - r > W || y - r > H) continue;
      bctx.moveTo(x + r, y);
      bctx.arc(x, y, r, 0, 6.283185307179586);
      if (x - r < minX) minX = x - r;
      if (y - r < minY) minY = y - r;
      if (x + r > maxX) maxX = x + r;
      if (y + r > maxY) maxY = y + r;
    }
    if (minX === Infinity) continue;             // nothing in this mode yet
    bctx.fill();

    // blit only what was touched; the buffer is a few megapixels
    const sx = Math.max(0, Math.floor(minX));
    const sy = Math.max(0, Math.floor(minY));
    const sw = Math.min(W, Math.ceil(maxX)) - sx;
    const sh = Math.min(H, Math.ceil(maxY)) - sy;
    if (sw <= 0 || sh <= 0) continue;
    vctx.globalAlpha = areaAlpha;
    vctx.drawImage(buf, sx, sy, sw, sh, sx, sy, sw, sh);
  }
  vctx.globalAlpha = 1;
}

/* -------------------------------------------------------------- animation */

const ICON_PLAY = 'M6 4l10 6-10 6z';
const ICON_PAUSE = 'M5.5 4h3.2v12H5.5zm5.8 0h3.2v12h-3.2z';

let animHandle = 0;
let lastTs = 0;
let playing = false;

/* The clock is a piece of state you can set, not a one-shot animation: play
 * advances it, the scrubber sets it outright, and both end up in the same
 * place. Nothing else may write frameTime. */
function setTime(t) {
  const end = current.t0 + current.horizon;
  frameTime = Math.max(current.t0, Math.min(end, t));
  const elapsed = frameTime - current.t0;
  el('clockValue').textContent = Math.floor(elapsed / 60);
  el('scrub').value = String(Math.round(elapsed));
  el('scrubOut').textContent = Math.floor(elapsed / 60) + ' min';
  paint();
  return frameTime >= end;
}

function step(ts) {
  if (!playing) return;
  const perMs = current.horizon / animDuration();
  const atEnd = setTime(frameTime + (ts - lastTs) * perMs);
  lastTs = ts;
  if (atEnd) pause();
  else animHandle = requestAnimationFrame(step);
}

function animDuration() {
  return Math.min(5500, Math.max(2400, current.horizon / 60 * 110));
}

function play() {
  if (!current) return;
  // starting from the end means "again", not "nothing happens"
  if (frameTime >= current.t0 + current.horizon) setTime(current.t0);
  playing = true;
  lastTs = performance.now();
  el('playIcon').setAttribute('d', ICON_PAUSE);
  el('play').setAttribute('aria-label', 'Pausa');
  cancelAnimationFrame(animHandle);
  animHandle = requestAnimationFrame(step);
}

function pause() {
  playing = false;
  cancelAnimationFrame(animHandle);
  animHandle = 0;
  el('playIcon').setAttribute('d', ICON_PLAY);
  el('play').setAttribute('aria-label', 'Spela upp');
}

function showStats() {
  const stats = el('stats');
  stats.hidden = false;
  el('statStops').textContent = current.gCount.toLocaleString('sv-SE');
  el('statLongest').textContent = Math.round(current.longest / 60) + ' min';
  const km = current.farthest / 1000;
  el('statFar').textContent =
    (km >= 10 ? Math.round(km) : km.toFixed(1).replace('.', ',')) + ' km';
}

/* --------------------------------------------------------------------- UI */

let map;
let marker = null;
let horizonMin = 30;
let departMin = 480;
let accessMin = ACCESS_DEFAULT_MIN;
let showWalkArea = false;
let pendingNote = '';   // explanation the next search should carry

function departSeconds() { return departMin * 60; }

function runFrom(lon, lat) {
  if (!D.conn) return;
  const t0 = departSeconds() - D.header.window_start;
  const horizon = horizonMin * 60;
  if (t0 < 0 || t0 + horizon > D.header.duration) {
    say('Vald tid ligger utanför det förberedda tidsfönstret.', true);
    return;
  }
  const began = performance.now();
  current = search(lon, lat, t0, horizon, accessMin * 60);
  const ms = performance.now() - began;
  hoverGroup = -1;
  el('tip').hidden = true;

  if (!marker) {
    const dot = document.createElement('div');
    dot.className = 'origin-dot';
    marker = new maplibregl.Marker({ element: dot }).setLngLat([lon, lat])
      .addTo(map);
  } else {
    marker.setLngLat([lon, lat]);
  }

  const walkMin = Math.round(current.nearest / WALK_MPS / 60);
  const note = pendingNote ? ' ' + pendingNote : '';
  pendingNote = '';
  // Stops the water check threw out are worth naming. Silence would leave
  // the map looking simply empty, which is a different claim entirely.
  const water = current.blocked
    ? ' ' + current.blocked + (current.blocked === 1
        ? ' hållplatsläge ligger' : ' hållplatslägen ligger') +
      ' på andra sidan vattnet.'
    : '';
  if (current.count === 0 && current.blocked) {
    say('Ingen hållplats inom ' + accessMin + ' minuters gång — de närmaste ' +
        'ligger på andra sidan vattnet, och närmaste bro är för långt bort. ' +
        'Öka gångviljan, eller flytta klickpunkten till rätt sida.' + note);
  } else if (current.count === 0) {
    say('Ingen hållplats inom ' + accessMin + ' minuters gång — närmaste ' +
        'ligger ' + walkMin + ' minuter bort. Öka gångviljan, eller läs det ' +
        'som svaret: härifrån reser man inte kollektivt.' + note);
  } else {
    say('Sökning: ' + ms.toFixed(0) + ' ms, ' +
        current.scanned.toLocaleString('sv-SE') + ' avgångar granskade. ' +
        'Närmaste hållplats: ' + walkMin + ' min gång.' + water + note);
  }
  writeURL(lon, lat);
  el('clock').hidden = false;
  el('scrub').max = String(horizon);
  showStats();
  setTime(t0);
  play();
}

/* The latest departure that still fits inside the prepared data. It depends
 * on the horizon, so a two hour search stops two hours before the window ends
 * while a fifteen minute one runs almost to the edge. Locking the slider at
 * the worst case would throw away most of an evening. */
function latestDeparture() {
  if (!D.header) return 600;
  return Math.floor((D.header.window_end - horizonMin * 60) / 60);
}

function setHorizon(h) {
  horizonMin = h;
  for (const b of el('horizon').children) {
    b.classList.toggle('on', Number(b.dataset.h) === h);
  }
  const slider = el('depart');
  const max = latestDeparture();
  slider.max = String(max);
  if (departMin > max) {
    setDepart(max);
    // the search that follows will overwrite the status line, so hand the
    // explanation to it rather than saying it here and losing it
    pendingNote = 'Avgången flyttades till ' + el('departOut').textContent +
      ' — tidtabellen räcker inte längre än så för ' + h + ' minuter.';
  }
}

function setAccess(minutes) {
  accessMin = minutes;
  for (const b of el('access').children) {
    b.classList.toggle('on', Number(b.dataset.w) === minutes);
  }
}

function setDepart(minutes) {
  departMin = minutes;
  el('depart').value = String(minutes);
  el('departOut').textContent =
    String(Math.floor(minutes / 60)).padStart(2, '0') + ':' +
    String(minutes % 60).padStart(2, '0');
}

function writeURL(lon, lat) {
  const p = new URLSearchParams();
  p.set('at', lon.toFixed(5) + ',' + lat.toFixed(5));
  p.set('t', String(Math.floor(departMin / 60)).padStart(2, '0') +
              String(departMin % 60).padStart(2, '0'));
  p.set('h', String(horizonMin));
  p.set('w', String(accessMin));
  history.replaceState(null, '', '?' + p.toString());
}

function readURL() {
  const p = new URLSearchParams(location.search);
  const out = {};
  const at = p.get('at');
  if (at && /^-?[\d.]+,-?[\d.]+$/.test(at)) {
    const [lon, lat] = at.split(',').map(Number);
    out.lon = lon;
    out.lat = lat;
  }
  const t = p.get('t');
  if (t && /^\d{4}$/.test(t)) out.depart = +t.slice(0, 2) * 60 + +t.slice(2);
  const h = p.get('h');
  if (h && HORIZONS.includes(Number(h))) out.horizon = Number(h);
  const w = p.get('w');
  if (w && ['5', '10', '20'].includes(w)) out.access = Number(w);
  return out;
}

function wireUI() {
  el('horizon').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    setHorizon(Number(b.dataset.h));
    if (current) runFrom(current.lon, current.lat);
  });

  const slider = el('depart');
  slider.addEventListener('input', () => setDepart(Number(slider.value)));
  slider.addEventListener('change', () => {
    if (current) runFrom(current.lon, current.lat);
  });

  el('access').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    setAccess(Number(b.dataset.w));
    if (current) runFrom(current.lon, current.lat);
  });

  el('walkArea').addEventListener('change', (e) => {
    showWalkArea = e.target.checked;
    paint();
  });

  el('play').addEventListener('click', () => (playing ? pause() : play()));

  const scrub = el('scrub');
  scrub.addEventListener('input', () => {
    if (!current) return;
    pause();                       // dragging means you want to look, not watch
    setTime(current.t0 + Number(scrub.value));
  });

  // space is what people press at a video, and the slider would otherwise
  // swallow it as "activate"
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || !current) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' && document.activeElement.type !== 'range') return;
    e.preventDefault();
    if (playing) pause(); else play();
  });

  el('theme').addEventListener('click', () => setTheme(isDark() ? 'light' : 'dark'));

  el('share').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(location.href);
      say('Länken är kopierad.');
    } catch {
      say('Kunde inte kopiera — kopiera adressfältet manuellt.');
    }
  });
}

/* ------------------------------------------------------------------- boot */

// app.js is also require()d by tests/client.test.mjs, where there is no window
const mediaDark = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : { matches: false, addEventListener() {} };
let theme = 'auto';        // 'auto' | 'light' | 'dark'

function isDark() {
  return theme === 'dark' || (theme === 'auto' && mediaDark.matches);
}

function styleFor(dark) {
  const set = dark ? 'dark_all' : 'light_all';
  return {
    version: 8,
    sources: {
      carto: {
        type: 'raster',
        tiles: 'abc'.split('').map((h) =>
          `https://${h}.basemaps.cartocdn.com/${set}/{z}/{x}/{y}@2x.png`),
        tileSize: 256,
        maxzoom: 18,
        attribution: '© OpenStreetMap, © CARTO',
      },
    },
    layers: [
      { id: 'bg', type: 'background',
        paint: { 'background-color': dark ? '#12151a' : '#eef1f4' } },
      { id: 'carto', type: 'raster', source: 'carto' },
    ],
  };
}

/* The palette lives in CSS, so switching the attribute is enough for the
 * panel; the canvas and the basemap have to be told separately. */
function setTheme(next, initial) {
  theme = next;
  const root = document.documentElement;
  if (theme === 'auto') root.removeAttribute('data-theme');
  else root.dataset.theme = theme;
  try { localStorage.setItem('theme', theme); } catch { /* private mode */ }

  const dark = isDark();
  el('iconSun').classList.toggle('hide', !dark);
  el('iconMoon').classList.toggle('hide', dark);
  if (!initial) {
    map.setStyle(styleFor(dark));
    if (D.trips) { readColors(); paint(); }
  }
}

async function boot() {
  const wanted = readURL();
  try {
    const saved = localStorage.getItem('theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch { /* private mode */ }
  setTheme(theme, true);
  mediaDark.addEventListener('change', () => {
    if (theme === 'auto') setTheme('auto');
  });

  map = new maplibregl.Map({
    container: 'map',
    style: styleFor(isDark()),
    center: [12.0, 57.95],
    zoom: 8.2,
    minZoom: 6,
    maxZoom: 15,
    dragRotate: false,
    pitchWithRotate: false,
    attributionControl: false,
    // WebGL contents are lost before a screenshot unless the buffer is kept;
    // only worth the cost when someone is actually capturing one.
    preserveDrawingBuffer: new URLSearchParams(location.search).has('snapshot'),
  });
  map.touchZoomRotate.disableRotation();
  if (map.keyboard && map.keyboard.disableRotation) map.keyboard.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }),
                 'bottom-right');
  map.addControl(new maplibregl.AttributionControl({ compact: true }),
                 'bottom-right');

  map.on('move', () => { if (current) paint(); });
  map.on('resize', () => { sizeCanvas(); if (current) paint(); });
  map.on('click', (e) => runFrom(e.lngLat.lng, e.lngLat.lat));
  map.getCanvas().style.cursor = 'crosshair';

  // Repaint only when the pointer moves onto a different stop, not on every
  // mouse event — the highlight ring means a full redraw of the network.
  map.on('mousemove', (e) => {
    const g = pickGroup(e.point.x, e.point.y);
    const changed = g !== hoverGroup;
    hoverGroup = g;
    showTip(g, e.point.x, e.point.y);
    if (changed) paint();
  });
  map.on('mouseout', () => {
    if (hoverGroup < 0) return;
    hoverGroup = -1;
    el('tip').hidden = true;
    paint();
  });

  wireUI();
  await new Promise((r) => map.on('load', r));
  sizeCanvas();

  try {
    await loadData();
  } catch (err) {
    say('Kunde inte ladda tidtabellsdata: ' + err.message, true);
    return;
  }
  readColors();

  el('metaDate').textContent = D.meta.service_date + ' (' +
    D.meta.window_start + '–' + D.meta.window_end + ')';
  el('metaBuilt').textContent = D.meta.generated_at.slice(0, 10);

  // Water geometry is derived from OpenStreetMap and carries ODbL with it,
  // which is a different licence from the CC0 timetable. Say so when it is
  // actually in use, and say nothing when the build ran without it.
  const bar = D.meta.barriers;
  if (bar) {
    const line = el('metaBarriers');
    line.textContent = 'Vattengeometrin är härledd ur OpenStreetMap ('
      + bar.lines.toLocaleString('sv-SE') + ' strandlinjer, '
      + bar.gates.toLocaleString('sv-SE') + ' broar) och står under ODbL.';
    line.hidden = false;
  }

  // The slider may not run to 10:00 if the prepared window is narrower;
  // keep it inside what a 60 minute horizon can actually reach.
  const slider = el('depart');
  slider.min = String(Math.ceil(D.header.window_start / 60));
  setHorizon(wanted.horizon ?? 30);          // also sets the slider maximum
  setDepart(Math.min(Math.max(wanted.depart ?? 480, +slider.min),
                     latestDeparture()));
  setAccess(wanted.access ?? ACCESS_DEFAULT_MIN);

  say('Klart. Klicka på kartan.');
  if (wanted.lon !== undefined) {
    map.jumpTo({ center: [wanted.lon, wanted.lat], zoom: 10 });
    runFrom(wanted.lon, wanted.lat);
  }
}

if (typeof document !== 'undefined') {
  boot();
} else if (typeof module !== 'undefined') {
  module.exports = {
    D, ingest, search, WALK_MPS, MIN_CHANGE, SHORE_SLACK,
    segHit, crosses, collectSegments, gateExits, walkDistance, buildBarriers,
  };
}
