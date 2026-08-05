/* reliefkit web client.
 *
 * Scale arithmetic is mirrored from settings.py so slider changes re-render
 * locally instead of re-fetching a DEM. The server recomputes it authoritatively
 * when the STL is built; this copy only drives the preview.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = (id) => document.getElementById(id);

const state = {
  bbox: null,       // what the user drew
  preview: null,    // last /api/preview payload
  view: 'map',
  pending: null,    // AbortController for an in-flight preview
};

/* ── settings ────────────────────────────────────────────────── */

function readSettings() {
  return {
    scaleMode: document.querySelector('[data-mode].on').dataset.mode,
    targetSize: +$('size').value,
    relief: +$('relief').value,
    denom: +$('denom').value,
    exag: +$('exag').value,
    base: +$('base').value,
    grid: +$('grid').value,
    source: $('source').value,
    square: $('square').checked,
    tool: $('tool').value === '' ? null : +$('tool').value,
    fmt: document.querySelector('[data-fmt].on').dataset.fmt,
  };
}

/** Longest horizontal dimension of the finished model, in mm. */
function longestMm() {
  const s = readSettings();
  if (s.scaleMode === 'fit') return s.targetSize;
  const p = state.preview;
  if (!p) return null;
  return (Math.max(p.ground.width_m, p.ground.height_m) * 1000) / s.denom;
}

/** Mesh detail follows the selected tool: one sample per tool width.
 *  Sampling finer than the nozzle or bit can reproduce only grows the file. */
function syncGridToTool() {
  const s = readSettings();
  const longest = longestMm();
  if (!s.tool || !longest) return;
  const step = +$('grid').step || 50;
  const raw = Math.ceil(longest / s.tool);
  const snapped = Math.round(raw / step) * step;
  const g = Math.max(+$('grid').min, Math.min(+$('grid').max, snapped));
  $('grid').value = g;
  $('grid-out').textContent = `${g} samples`;
}

function updateGridNote() {
  const s = readSettings();
  const longest = longestMm();
  const el = $('grid-note');
  if (!longest) { el.textContent = ''; el.classList.remove('over'); return; }

  const mmPer = longest / s.grid;
  if (!s.tool) {
    el.textContent = `${mmPer.toFixed(2)} mm per sample.`;
    el.classList.remove('over');
    return;
  }
  const over = s.tool / mmPer;
  if (over > 1.15) {
    // At the slider floor there is nothing left to lower, so don't advise it.
    const atFloor = s.grid <= +$('grid').min;
    el.textContent =
      `${mmPer.toFixed(2)} mm/sample is ${over.toFixed(1)}\u00d7 finer than a ${s.tool} mm tool can reproduce` +
      (atFloor
        ? ' — already the coarsest mesh available.'
        : '. Lowering this shrinks the file with no visible loss.');
    el.classList.add('over');
  } else {
    el.textContent = `${mmPer.toFixed(2)} mm/sample, matched to a ${s.tool} mm tool.`;
    el.classList.remove('over');
  }
}

function recomputeEstimate() {
  const p = state.preview;
  if (!p) return;
  const aspect = p.ground.width_m / p.ground.height_m;
  const g = +$('grid').value;
  const cols = aspect >= 1 ? g : Math.max(2, Math.round(g * aspect));
  const rows = aspect >= 1 ? Math.max(2, Math.round(g / aspect)) : g;
  const ring = 2 * rows + 2 * cols - 4;
  const tris = 2 * (rows - 1) * (cols - 1) + 3 * ring;
  const stl = 84 + 50 * tris;
  p.estimated = { triangles: tris, bytes: stl, bytes_3mf: Math.round(stl * 0.21) };
}

/** Mirrors ReliefSettings.horizontal_mm_per_m / vertical_mm_per_m. */
function scaleFactors(s, groundW, groundH, rangeM) {
  if (s.scaleMode === 'fit') {
    return {
      xy: s.targetSize / Math.max(groundW, groundH),
      z: rangeM > 0 ? s.relief / rangeM : 0,
    };
  }
  const xy = 1000 / s.denom;
  return { xy, z: xy * s.exag };
}

/* ── geometry (mirrors mesh.build_solid) ─────────────────────── */

function perimeterRing(rows, cols) {
  const idx = (r, c) => r * cols + c;
  const ring = [];
  for (let c = 0; c < cols; c++) ring.push(idx(rows - 1, c));
  for (let r = rows - 2; r >= 0; r--) ring.push(idx(r, cols - 1));
  for (let c = cols - 2; c >= 0; c--) ring.push(idx(0, c));
  for (let r = 1; r < rows - 1; r++) ring.push(idx(r, 0));
  return ring;
}

function buildGeometry(heightsMm, rows, cols, dx, dy, base) {
  const ring = perimeterRing(rows, cols);
  const nTop = rows * cols;
  const nRing = ring.length;

  const pos = new Float32Array((nTop + nRing + 1) * 3);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const i = (r * cols + c) * 3;
      pos[i] = c * dx;
      pos[i + 1] = (rows - 1 - r) * dy;   // row 0 is north -> max Y
      pos[i + 2] = heightsMm[r * cols + c] + base;
    }
  }
  for (let k = 0; k < nRing; k++) {
    const src = ring[k] * 3;
    const dst = (nTop + k) * 3;
    pos[dst] = pos[src];
    pos[dst + 1] = pos[src + 1];
    pos[dst + 2] = 0;
  }
  const centre = (nTop + nRing) * 3;
  pos[centre] = ((cols - 1) * dx) / 2;
  pos[centre + 1] = ((rows - 1) * dy) / 2;
  pos[centre + 2] = 0;

  const idx = [];
  for (let r = 0; r < rows - 1; r++) {
    for (let c = 0; c < cols - 1; c++) {
      const v00 = r * cols + c, v01 = v00 + 1, v10 = v00 + cols, v11 = v10 + 1;
      idx.push(v00, v10, v11, v00, v11, v01);
    }
  }
  for (let k = 0; k < nRing; k++) {
    const n = (k + 1) % nRing;
    const ti = ring[k], tn = ring[n], bi = nTop + k, bn = nTop + n;
    idx.push(ti, bi, bn, ti, bn, tn);
  }
  const centreIdx = nTop + nRing;
  for (let k = 0; k < nRing; k++) {
    idx.push(centreIdx, nTop + ((k + 1) % nRing), nTop + k);
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  return geo;
}

/* ── three.js ────────────────────────────────────────────────── */

const three = { ready: false };

function initThree() {
  if (three.ready) return;
  const host = $('three');
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 5000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  scene.add(new THREE.HemisphereLight(0xffffff, 0x545048, 2.1));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(-1, 1.4, 2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.5);
  fill.position.set(2, -1, 1);
  scene.add(fill);

  Object.assign(three, { ready: true, renderer, scene, camera, controls, host, mesh: null });

  new ResizeObserver(resizeThree).observe(host);
  resizeThree();

  (function loop() {
    requestAnimationFrame(loop);
    if (state.view === '3d') { controls.update(); renderer.render(scene, camera); }
  })();
}

function resizeThree() {
  if (!three.ready) return;
  const { clientWidth: w, clientHeight: h } = three.host;
  if (!w || !h) return;
  three.renderer.setSize(w, h, false);
  three.camera.aspect = w / h;
  three.camera.updateProjectionMatrix();
}

function renderModel(heightsMm, rows, cols, sizeX, sizeY, base) {
  initThree();
  if (three.mesh) {
    three.mesh.geometry.dispose();
    three.scene.remove(three.mesh);
  }
  const geo = buildGeometry(heightsMm, rows, cols, sizeX / (cols - 1), sizeY / (rows - 1), base);
  const material = new THREE.MeshStandardMaterial({
    color: 0xbcb3a4, roughness: 0.82, metalness: 0.02,
  });
  const mesh = new THREE.Mesh(geo, material);
  // Recentre so orbiting spins around the model, not the origin corner.
  geo.computeBoundingBox();
  const c = new THREE.Vector3();
  geo.boundingBox.getCenter(c);
  mesh.position.set(-c.x, -c.y, -c.z);
  three.scene.add(mesh);
  three.mesh = mesh;

  three.camera.up.set(0, 0, 1);
  resizeThree();        // settle the aspect before fitting to it
  frameModel(geo);
}

const VIEW_DIR = new THREE.Vector3(0.18, -1, 0.6).normalize();
const FRAME_FILL = 0.92;   // fraction of the viewport the model should span

/** Fit the camera to the model's actual projected extent.
 *
 * A bounding-sphere fit is the usual shortcut, but terrain is a wide flat slab
 * whose sphere radius is dominated by the diagonal -- it leaves most of the
 * frame empty. Projecting the eight box corners and solving for the distance
 * that puts the widest one at FRAME_FILL converges in a few passes and fills
 * the viewport properly at any aspect ratio.
 */
function frameModel(geo) {
  const cam = three.camera;
  geo.computeBoundingBox();
  geo.computeBoundingSphere();

  const offset = three.mesh.position;
  const bb = geo.boundingBox;
  const corners = [];
  for (const x of [bb.min.x, bb.max.x])
    for (const y of [bb.min.y, bb.max.y])
      for (const z of [bb.min.z, bb.max.z])
        corners.push(new THREE.Vector3(x, y, z).add(offset));

  let dist = geo.boundingSphere.radius * 2.5;
  for (let pass = 0; pass < 4; pass++) {
    cam.position.copy(VIEW_DIR.clone().multiplyScalar(dist));
    cam.near = Math.max(dist / 500, 0.01);
    cam.far = dist * 20;
    cam.lookAt(0, 0, 0);
    cam.updateProjectionMatrix();
    cam.updateMatrixWorld();

    let extent = 0;
    for (const c of corners) {
      const p = c.clone().project(cam);
      extent = Math.max(extent, Math.abs(p.x), Math.abs(p.y));
    }
    if (!extent) break;
    dist *= extent / FRAME_FILL;
  }

  three.controls.target.set(0, 0, 0);
  three.controls.update();
}

/* ── map ─────────────────────────────────────────────────────── */

const BASEMAPS = {
  topo: {
    tiles: ['https://a.tile.opentopomap.org/{z}/{x}/{y}.png'],
    maxzoom: 16,
    attribution: '© OpenStreetMap contributors, SRTM | © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
  },
  satellite: {
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
    maxzoom: 18,
    attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
  },
};

function styleFor(name) {
  const b = BASEMAPS[name];
  return {
    version: 8,
    sources: { base: { type: 'raster', tiles: b.tiles, tileSize: 256, maxzoom: b.maxzoom, attribution: b.attribution } },
    layers: [{ id: 'base', type: 'raster', source: 'base' }],
  };
}

const map = new maplibregl.Map({
  container: 'map',
  style: styleFor('topo'),
  center: [-121.75, 46.85],
  zoom: 10,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');

function emptyFC() { return { type: 'FeatureCollection', features: [] }; }

function ensureBoxLayers() {
  if (map.getSource('sel')) return;
  map.addSource('sel', { type: 'geojson', data: emptyFC() });
  map.addLayer({ id: 'sel-fill', type: 'fill', source: 'sel', paint: { 'fill-color': '#0f6f66', 'fill-opacity': 0.16 } });
  map.addLayer({ id: 'sel-line', type: 'line', source: 'sel', paint: { 'line-color': '#0f6f66', 'line-width': 2 } });
}
map.on('style.load', () => { ensureBoxLayers(); drawBox(state.bbox); });

function drawBox(b) {
  if (!map.getSource('sel')) return;
  map.getSource('sel').setData(b ? {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature', properties: {},
      geometry: {
        type: 'Polygon',
        coordinates: [[[b.west, b.south], [b.east, b.south], [b.east, b.north], [b.west, b.north], [b.west, b.south]]],
      },
    }],
  } : emptyFC());
}

// cmd/ctrl + drag draws a region.
let dragStart = null;
const canvas = map.getCanvas();

canvas.addEventListener('mousedown', (e) => {
  if (!(e.metaKey || e.ctrlKey) || e.button !== 0) return;
  e.preventDefault();
  map.dragPan.disable();
  dragStart = map.unproject([e.offsetX, e.offsetY]);
  canvas.style.cursor = 'crosshair';
});

canvas.addEventListener('mousemove', (e) => {
  if (!dragStart) return;
  const now = map.unproject([e.offsetX, e.offsetY]);
  drawBox(normalise(dragStart, now));
});

window.addEventListener('mouseup', (e) => {
  if (!dragStart) return;
  const now = map.unproject([e.offsetX, e.offsetY]);
  const box = normalise(dragStart, now);
  dragStart = null;
  map.dragPan.enable();
  canvas.style.cursor = '';

  if (box.east - box.west < 1e-4 || box.north - box.south < 1e-4) {
    drawBox(state.bbox);
    setStatus('Selection too small — drag a larger box.');
    return;
  }
  state.bbox = box;
  drawBox(box);
  $('clear').disabled = false;
  refreshPreview();
});

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && dragStart) {
    dragStart = null;
    map.dragPan.enable();
    canvas.style.cursor = '';
    drawBox(state.bbox);
  }
});

function normalise(a, b) {
  return {
    west: Math.min(a.lng, b.lng), east: Math.max(a.lng, b.lng),
    south: Math.min(a.lat, b.lat), north: Math.max(a.lat, b.lat),
  };
}

/* ── data flow ───────────────────────────────────────────────── */

function setStatus(text) { $('status').textContent = text || ''; }

let busyTimer = null;

function showBusy(on, text = 'Fetching elevation…') {
  $('busy').hidden = !on;
  $('busy-text').textContent = text;
  clearTimeout(busyTimer);
  if (on) {
    $('error').hidden = true;
    // Large regions can take the better part of a minute upstream; silence
    // that long reads as a hang, so say something before the user gives up.
    busyTimer = setTimeout(() => {
      $('busy-text').textContent = 'Still fetching — large regions take longer to assemble.';
    }, 8000);
  }
}

function showError(message) {
  $('error').hidden = false;
  $('error-text').textContent = message;
  showBusy(false);
}

async function refreshPreview() {
  if (!state.bbox) return;
  state.pending?.abort();
  const controller = new AbortController();
  state.pending = controller;

  const s = readSettings();
  showBusy(true);
  setStatus('');

  try {
    const res = await fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...state.bbox, source: s.source, square: s.square, grid: s.grid }),
      signal: controller.signal,
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);

    state.preview = body;
    syncGridToTool();
    recomputeEstimate();
    drawBox(body.bbox);                       // reflect the squared-off region
    $('tab-3d').disabled = false;
    $('tab-3d').title = '';
    $('export').disabled = false;
    $('three-empty').hidden = true;
    showBusy(false);
    applyScale();
    setStatus(`${body.source} · ${body.rows}×${body.cols} preview`);
  } catch (err) {
    if (err.name === 'AbortError') return;
    showError(err.message);
    setStatus('');
  } finally {
    if (state.pending === controller) state.pending = null;
  }
}

/** Re-scale and re-render from the cached elevation grid. No network. */
function applyScale() {
  const p = state.preview;
  if (!p) return;
  const s = readSettings();
  const { width_m: gw, height_m: gh } = p.ground;
  const { xy, z } = scaleFactors(s, gw, gh, p.elevation.range_m);

  const heights = new Float32Array(p.elevations_m.length);
  for (let i = 0; i < heights.length; i++) heights[i] = (p.elevations_m[i] - p.elevation.min_m) * z;

  const sizeX = gw * xy, sizeY = gh * xy;
  let peak = 0;
  for (let i = 0; i < heights.length; i++) if (heights[i] > peak) peak = heights[i];
  const sizeZ = s.base + peak;

  renderIfVisible(heights, p.rows, p.cols, sizeX, sizeY, s.base);

  $('r-ground').textContent = `${(gw / 1000).toFixed(2)} × ${(gh / 1000).toFixed(2)} km`;
  $('r-elev').textContent = `${p.elevation.min_m} – ${p.elevation.max_m} m`;
  $('r-centre').textContent =
    `${((p.bbox.south + p.bbox.north) / 2).toFixed(3)}, ${((p.bbox.west + p.bbox.east) / 2).toFixed(3)}`;
  $('region-info').hidden = false;
  $('region-empty').hidden = true;

  $('e-size').textContent = `${sizeX.toFixed(1)} × ${sizeY.toFixed(1)} × ${sizeZ.toFixed(1)} mm`;
  $('e-scale').textContent = `1:${Math.round(1000 / xy).toLocaleString()} · ${(z / xy).toFixed(2)}× vert`;
  $('e-tris').textContent = p.estimated.triangles.toLocaleString();
  const bytes = s.fmt === '3mf' ? p.estimated.bytes_3mf : p.estimated.bytes;
  $('e-bytes').textContent = `${(bytes / 1e6).toFixed(1)} MB${s.fmt === '3mf' ? ' (approx)' : ''}`;
  $('export').querySelector('.btn-label').textContent = `Export ${s.fmt.toUpperCase()}`;
  updateGridNote();
  $('attribution').textContent = p.attribution;

  renderWarnings(p.warnings);
}

function renderIfVisible(heights, rows, cols, x, y, base) {
  three.cached = { heights, rows, cols, x, y, base };
  if (state.view === '3d') renderModel(heights, rows, cols, x, y, base);
}

function renderWarnings(list) {
  document.querySelector('.warn-list')?.remove();
  if (!list?.length) return;
  const ul = document.createElement('ul');
  ul.className = 'warn-list';
  ul.setAttribute('role', 'status');
  for (const w of list) {
    const li = document.createElement('li');
    li.textContent = w;
    ul.appendChild(li);
  }
  document.querySelector('.stage').appendChild(ul);
}

/* ── export ──────────────────────────────────────────────────── */

async function doExport() {
  const btn = $('export');
  const s = readSettings();
  btn.classList.add('working');
  btn.disabled = true;
  btn.querySelector('.btn-label').textContent = 'Building…';
  setStatus('Building mesh — this can take a while at high detail.');

  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...state.bbox, source: s.source, square: s.square, grid: s.grid, fmt: s.fmt,
        scale_mode: s.scaleMode, target_size_mm: s.targetSize, relief_height_mm: s.relief,
        base_thickness_mm: s.base, scale_denominator: s.denom, z_exaggeration: s.exag,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Export failed (${res.status})`);
    }
    const blob = await res.blob();
    const name = res.headers.get('Content-Disposition')?.match(/filename="(.+?)"/)?.[1] || 'reliefkit.stl';
    const url = URL.createObjectURL(blob);
    Object.assign(document.createElement('a'), { href: url, download: name }).click();
    URL.revokeObjectURL(url);
    setStatus(`Exported ${name} · ${(+res.headers.get('X-Triangle-Count')).toLocaleString()} triangles`);
  } catch (err) {
    showError(err.message);
    setStatus('');
  } finally {
    btn.classList.remove('working');
    btn.disabled = false;
    btn.querySelector('.btn-label').textContent = `Export ${readSettings().fmt.toUpperCase()}`;
  }
}

/* ── wiring ──────────────────────────────────────────────────── */

function switchView(view) {
  state.view = view;
  const is3d = view === '3d';
  $('map').hidden = is3d;
  $('three').hidden = !is3d;
  $('tab-map').classList.toggle('on', !is3d);
  $('tab-3d').classList.toggle('on', is3d);
  $('tab-map').setAttribute('aria-selected', String(!is3d));
  $('tab-3d').setAttribute('aria-selected', String(is3d));
  $('map-hint').hidden = is3d;
  if (is3d && three.cached) {
    const c = three.cached;
    renderModel(c.heights, c.rows, c.cols, c.x, c.y, c.base);
  } else if (!is3d) {
    map.resize();
  }
}

$('tab-map').addEventListener('click', () => switchView('map'));
$('tab-3d').addEventListener('click', () => { if (!$('tab-3d').disabled) switchView('3d'); });

for (const btn of document.querySelectorAll('[data-base]')) {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-base]').forEach((b) => {
      b.classList.toggle('on', b === btn);
      b.setAttribute('aria-checked', String(b === btn));
    });
    map.setStyle(styleFor(btn.dataset.base));
  });
}

for (const btn of document.querySelectorAll('[data-mode]')) {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-mode]').forEach((b) => {
      b.classList.toggle('on', b === btn);
      b.setAttribute('aria-checked', String(b === btn));
    });
    const fit = btn.dataset.mode === 'fit';
    $('mode-fit').hidden = !fit;
    $('mode-true').hidden = fit;
    syncGridToTool();
    recomputeEstimate();
    applyScale();
  });
}

// Scale-only changes re-render locally; source/region changes need a refetch.
for (const id of ['size', 'relief', 'denom', 'exag', 'base']) {
  $(id).addEventListener('input', () => {
    if (id === 'relief') $('relief-out').textContent = `${$('relief').value} mm`;
    if (id === 'exag') $('exag-out').textContent = `${(+$('exag').value).toFixed(1)}×`;
    applyScale();
  });
}
$('grid').addEventListener('input', () => {
  $('grid-out').textContent = `${$('grid').value} samples`;
  $('tool').value = '';            // hand-set detail means the tool no longer drives it
  recomputeEstimate();
  applyScale();
  updateGridNote();
});

$('tool').addEventListener('change', () => {
  syncGridToTool();
  recomputeEstimate();
  applyScale();
});

for (const btn of document.querySelectorAll('[data-fmt]')) {
  btn.addEventListener('click', () => {
    document.querySelectorAll('[data-fmt]').forEach((b) => {
      b.classList.toggle('on', b === btn);
      b.setAttribute('aria-checked', String(b === btn));
    });
    applyScale();
  });
}
for (const id of ['source', 'square']) $(id).addEventListener('change', refreshPreview);

$('clear').addEventListener('click', () => {
  state.bbox = null;
  state.preview = null;
  drawBox(null);
  $('clear').disabled = true;
  $('export').disabled = true;
  $('tab-3d').disabled = true;
  $('tab-3d').title = 'Select a region first';
  $('region-info').hidden = true;
  $('region-empty').hidden = false;
  $('three-empty').hidden = false;
  document.querySelector('.warn-list')?.remove();
  switchView('map');
  setStatus('');
});

$('retry').addEventListener('click', () => { $('error').hidden = true; refreshPreview(); });
$('export').addEventListener('click', doExport);
