/* MeshCanvas frontend.
 *
 * Vanilla JS, no build step, no framework. Leaflet is the only dependency.
 *
 * Contract with the backend:
 *   POST /api/render    {shape spec}          -> {points: [[lat,lon],...], node_count}
 *   POST /api/transmit  {shape spec + mode}   -> starts a run
 *   POST /api/abort                           -> drains the queue
 *   GET  /api/budget?<params>                 -> {toa_ms_per_packet, total_airtime_ms,
 *                                                 duty_cycle_percent, eta_seconds,
 *                                                 region_duty_cycle_limit}
 *   WS   /ws            -> {type: "log"|"progress"|"done"|"error", ...}
 *
 * Nothing here computes a frequency, a duty cycle or a time on air. Those are
 * backend numbers. Any panel with no backend value shows "--" rather than a
 * guess: a plausible wrong number on an RF instrument is worse than a blank.
 */

'use strict';

(function () {

  /* ===================================================================== */
  /* Configuration                                                          */
  /* ===================================================================== */

  var LOG_MAX_LINES = 500;          // hard cap on retained console lines
  var BUDGET_DEBOUNCE_MS = 350;
  var WS_BACKOFF_MAX_MS = 30000;
  var MAX_IMAGE_BYTES = 4 * 1024 * 1024;
  var FREEHAND_MAX_POINTS = 400;    // traces are decimated to this before sending
  var CLICK_VS_DBLCLICK_MS = 220;   // see onMapClick

  var COLOR_PREVIEW = '#ff3ea5';    // synthetic preview points, deliberately not
                                    // a colour used for anything observed
  /* Default map view. */
  var MAP_HOME = [36.133319, -115.158470];
  var MAP_HOME_ZOOM = 14;

  var COLOR_DRAW = '#38bdf8';
  var COLOR_CENTRE = '#e3b341';

  var JSON_HEADERS = { 'Content-Type': 'application/json' };

  /* When the page is opened straight off disk there is no origin to talk to.
   * Fall back to the uvicorn default rather than failing silently, and say so
   * in the log so the target is never a mystery. */
  var FILE_ORIGIN = window.location.protocol === 'file:';
  var API_BASE = FILE_ORIGIN ? 'http://127.0.0.1:8000' : '';

  var MODES = {
    'dry-run': {
      tag: 'DRY RUN',
      text: 'Nothing is transmitted. Frames are built and counted only.',
      hint: 'dry-run: frames are built, encrypted and counted, then discarded.'
    },
    'mqtt': {
      tag: 'MQTT',
      text: 'Packets are published to the configured broker. No RF is generated.',
      hint: 'mqtt: a ServiceEnvelope goes to the broker. Anything bridging that broker to RF will relay it.'
    },
    'rf': {
      tag: 'RF LIVE',
      text: 'The transmitter will be keyed on real spectrum. Confirm every run.',
      hint: 'rf: this keys a real radio. Check the region, the power limit and your licence.'
    }
  };

  /* ===================================================================== */
  /* State - one object, no globals beyond this closure                     */
  /* ===================================================================== */

  var state = {
    mode: 'dry-run',
    region: 'US',
    preset: 'LONG_FAST',
    channelName: 'LongFast',
    psk: '',
    txPowerDbm: 20,
    nodeCount: 50,
    scaleM: 1000,

    tool: null,                     // polygon | freehand | text | image | null
    draw: {
      kind: null,                   // which tool produced vertices
      vertices: [],                 // [[lat,lon],...] current/most recent stroke
      strokes: [],                  // freehand: every completed stroke
      closed: false,
      centre: null,                 // [lat,lon] for text and image placement
      text: '',
      image: null                   // {filename, mime, data_base64}
    },

    airtimeTarget: 50,
    channelNum: null,
    hopLimit: 3,
    precisionBits: 32,
    profile: 'private',

    preview: { count: null },

    budget: {
      toaMs: null,
      totalMs: null,
      duty: null,
      limit: null,
      target: null,
      gapMs: null,
      etaS: null,
      freqHz: null,
      powerLimit: null,
      packets: null
    },
    dutyOverride: false,

    running: false,
    armed: false,
    armedSpec: null,

    ws: { sock: null, attempts: 0, timer: null, status: 'idle' },

    map: null,
    layers: { draw: null, preview: null },
    freehand: { active: false, line: null },
    clickTimer: null,
    budgetTimer: null,
    lastBudgetError: { text: '', at: 0 }
  };

  var dom = {};

  /* ===================================================================== */
  /* Small helpers                                                          */
  /* ===================================================================== */

  function $(id) { return document.getElementById(id); }

  function num(v) {
    var n = typeof v === 'string' ? parseFloat(v) : v;
    return (typeof n === 'number' && isFinite(n)) ? n : null;
  }

  function show(el, visible) {
    if (el) { el.classList.toggle('hidden', !visible); }
  }

  /* Never let a key reach the log pane, whatever the backend sends back. */
  function redact(obj) {
    var out = {};
    Object.keys(obj).forEach(function (k) {
      out[k] = /psk|key|secret|password|token/i.test(k) ? '[redacted]' : obj[k];
    });
    return out;
  }

  /* ---------------------------- formatting ----------------------------- */

  function fmtFreq(hz) {
    if (hz === null || hz === undefined || typeof hz !== 'number' || isNaN(hz)) { return '--'; }
    return (hz / 1e6).toFixed(4) + ' MHz';
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined || typeof ms !== 'number' || isNaN(ms)) { return '--'; }
    if (ms < 1000) { return (ms < 10 ? ms.toFixed(2) : ms.toFixed(0)) + ' ms'; }
    return fmtDuration(ms / 1000);
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || typeof seconds !== 'number' || isNaN(seconds)) { return '--'; }
    if (seconds < 1) { return (seconds * 1000).toFixed(0) + ' ms'; }
    if (seconds < 60) { return seconds.toFixed(1) + ' s'; }
    var s = Math.round(seconds);
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var r = s % 60;
    if (h > 0) { return h + ' h ' + String(m).padStart(2, '0') + ' m ' + String(r).padStart(2, '0') + ' s'; }
    return m + ' m ' + String(r).padStart(2, '0') + ' s';
  }

  function fmtPercent(p) {
    if (p === null || p === undefined || typeof p !== 'number' || isNaN(p)) { return '--'; }
    return (p < 10 ? p.toFixed(2) : p.toFixed(1)) + ' %';
  }

  function fmtBytes(n) {
    if (n < 1024) { return n + ' B'; }
    if (n < 1024 * 1024) { return (n / 1024).toFixed(1) + ' kB'; }
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
  }

  /* ===================================================================== */
  /* Log console                                                            */
  /* ===================================================================== */

  function log(level, message) {
    if (!dom.log) { return; }
    var atBottom = (dom.log.scrollHeight - dom.log.scrollTop - dom.log.clientHeight) < 24;

    var line = document.createElement('div');
    line.className = 'log-line log-' + level;
    line.textContent = new Date().toISOString().slice(11, 23) + '  ' +
                       level.toUpperCase().padEnd(5, ' ') + '  ' + message;
    dom.log.appendChild(line);

    while (dom.log.childElementCount > LOG_MAX_LINES) {
      dom.log.removeChild(dom.log.firstChild);
    }
    if (atBottom) { dom.log.scrollTop = dom.log.scrollHeight; }
  }

  /* Budget polling can fail on every keystroke while the backend is down.
   * Collapse repeats of the same message so the cap is not burned on noise. */
  function logThrottled(level, message, windowMs) {
    var now = Date.now();
    if (state.lastBudgetError.text === message &&
        (now - state.lastBudgetError.at) < windowMs) {
      return;
    }
    state.lastBudgetError = { text: message, at: now };
    log(level, message);
  }

  /* ===================================================================== */
  /* API                                                                    */
  /* ===================================================================== */

  /* Resolves to {ok: true, data} or {ok: false, error}. Never throws, never
   * swallows: every failure comes back as a string for the log pane. */
  function api(path, options, label) {
    var url = API_BASE + path;
    return fetch(url, options).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var detail = '';
          if (data && data.detail !== undefined) {
            detail = (typeof data.detail === 'string') ? data.detail : JSON.stringify(data.detail);
          } else {
            detail = text ? text.slice(0, 300) : res.statusText;
          }
          if (res.status === 404) {
            detail = detail || 'endpoint not implemented on this backend';
          }
          return { ok: false, error: label + ': HTTP ' + res.status + ' ' + detail };
        }
        if (data === null) {
          return { ok: false, error: label + ': response body was not JSON' };
        }
        return { ok: true, data: data };
      });
    }).catch(function (err) {
      var msg = (err && err.message) ? err.message : String(err);
      return { ok: false, error: label + ': request failed (' + msg + ') at ' + url };
    });
  }

  /* ===================================================================== */
  /* Map and drawing                                                        */
  /* ===================================================================== */

  function initMap() {
    state.map = L.map('map', {
      doubleClickZoom: false,   // dblclick closes a polygon instead
      zoomControl: true
    }).setView(MAP_HOME, MAP_HOME_ZOOM);

    /* OpenStreetMap standard tiles.
     *
     * OSM TILE USAGE POLICY: https://operations.osmfoundation.org/policies/tiles/
     * The tiles are donated capacity, not a free CDN. Bulk downloading is
     * prohibited, and so is any form of scripted prefetching or local mirroring
     * of the tile stream. This page therefore loads only the tiles the user is
     * actually looking at, sets no custom cache layer, and never walks a tile
     * range programmatically. A production or unattended deployment must point
     * this URL at its own tile server or a commercial provider before it goes
     * anywhere near heavy use. Attribution below is required and must stay. */
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: 'Map data and tiles (c) OpenStreetMap contributors, ' +
                   'ODbL. Tiles served by the OpenStreetMap Foundation under its ' +
                   'tile usage policy. MeshCanvas: synthetic node overlay, not observed data.'
    }).addTo(state.map);

    state.layers.draw = L.layerGroup().addTo(state.map);
    state.layers.preview = L.layerGroup().addTo(state.map);

    state.map.on('click', onMapClick);
    state.map.on('dblclick', onMapDblClick);
    state.map.on('mousedown', onFreehandStart);
  }

  /* Leaflet fires click, click, dblclick for a double click. Adding a vertex on
   * a timer that a dblclick cancels keeps the closing double click from also
   * dropping two stray vertices. The cost is a ~220 ms delay before a vertex
   * appears, which is the cheaper of the two annoyances. */
  function onMapClick(e) {
    if (state.tool === 'polygon') {
      if (state.clickTimer) { clearTimeout(state.clickTimer); }
      var ll = e.latlng;
      state.clickTimer = setTimeout(function () {
        state.clickTimer = null;
        addVertex(ll);
      }, CLICK_VS_DBLCLICK_MS);
    } else if (state.tool === 'text' || state.tool === 'image') {
      state.draw.centre = [e.latlng.lat, e.latlng.lng];
      redrawDrawing();
      disarm();
    }
  }

  function onMapDblClick() {
    if (state.clickTimer) { clearTimeout(state.clickTimer); state.clickTimer = null; }
    if (state.tool === 'polygon') { closePolygon(); }
  }

  function addVertex(latlng) {
    if (state.draw.kind !== 'polygon') {
      state.draw.kind = 'polygon';
      state.draw.vertices = [];
    }
    state.draw.closed = false;
    state.draw.vertices.push([latlng.lat, latlng.lng]);
    redrawDrawing();
    disarm();
  }

  function closePolygon() {
    if (state.draw.kind !== 'polygon' || state.draw.vertices.length < 3) {
      log('warn', 'a polygon needs at least 3 vertices before it can be closed');
      return;
    }
    state.draw.closed = true;
    redrawDrawing();
    log('info', 'polygon closed with ' + state.draw.vertices.length + ' vertices');
  }

  /* ------------------------------ freehand ------------------------------ */

  function onFreehandStart(e) {
    if (state.tool !== 'freehand') { return; }
    /* Switching tools resets strokes; a new stroke only appends. Clearing
     * here is what used to make each stroke erase the previous one. */
    if (state.draw.kind !== 'freehand') {
      state.draw.kind = 'freehand';
      state.draw.strokes = [];
    }
    state.draw.vertices = [[e.latlng.lat, e.latlng.lng]];
    state.draw.closed = false;
    state.freehand.active = true;

    state.map.dragging.disable();
    state.freehand.line = L.polyline([e.latlng], { color: COLOR_DRAW, weight: 2 })
      .addTo(state.layers.draw);

    state.map.on('mousemove', onFreehandMove);
    document.addEventListener('mouseup', onFreehandEnd, { once: true });
  }

  function onFreehandMove(e) {
    if (!state.freehand.active) { return; }
    var v = state.draw.vertices;
    var last = v[v.length - 1];
    if (last) {
      var a = state.map.latLngToContainerPoint(L.latLng(last[0], last[1]));
      var b = state.map.latLngToContainerPoint(e.latlng);
      if (a.distanceTo(b) < 5) { return; }   // pixel filter, keeps the trace sane
    }
    v.push([e.latlng.lat, e.latlng.lng]);
    if (state.freehand.line) { state.freehand.line.addLatLng(e.latlng); }
  }

  function onFreehandEnd() {
    if (!state.freehand.active) { return; }
    state.freehand.active = false;
    state.map.off('mousemove', onFreehandMove);
    state.map.dragging.enable();

    if (state.draw.vertices.length < 2) {
      log('warn', 'freehand stroke was too short to use; drag further');
      state.draw.vertices = [];
      if (!state.draw.strokes.length) { state.draw.kind = null; }
    } else {
      var stroke = decimate(state.draw.vertices, FREEHAND_MAX_POINTS);
      state.draw.strokes.push(stroke);
      state.draw.vertices = stroke;
      state.draw.closed = false;
      log('info', 'stroke ' + state.draw.strokes.length + ' captured: ' +
                  stroke.length + ' points. Drag again to add another stroke.');
    }
    state.freehand.line = null;
    redrawDrawing();
    updateDrawStatus();
  }

  /* Even sampling, endpoints kept. Trimming here keeps the POST body small
   * without biasing the outline the way "first N points" would. */
  function decimate(points, maxPoints) {
    if (points.length <= maxPoints) { return points; }
    var out = [];
    var step = (points.length - 1) / (maxPoints - 1);
    for (var i = 0; i < maxPoints; i++) {
      out.push(points[Math.round(i * step)]);
    }
    return out;
  }

  /* ------------------------------ rendering ----------------------------- */

  function redrawDrawing() {
    var g = state.layers.draw;
    if (!g) { return; }
    g.clearLayers();

    /* Freehand keeps a list of strokes; draw them all. */
    if (state.draw.kind === 'freehand' && state.draw.strokes.length) {
      state.draw.strokes.forEach(function (stroke) {
        L.polyline(stroke, { color: COLOR_DRAW, weight: 2 }).addTo(g);
      });
    }

    var pts = (state.draw.kind === 'freehand') ? [] : state.draw.vertices;
    if (pts.length > 1) {
      if (state.draw.closed && pts.length >= 3) {
        L.polygon(pts, { color: COLOR_DRAW, weight: 2, fillColor: COLOR_DRAW, fillOpacity: 0.08 }).addTo(g);
      } else {
        L.polyline(pts, { color: COLOR_DRAW, weight: 2 }).addTo(g);
      }
    }
    /* Vertex handles only for polygons: a freehand trace has hundreds of
     * points and drawing a marker for each one costs more than it shows. */
    if (state.draw.kind === 'polygon') {
      pts.forEach(function (p) {
        L.circleMarker(p, {
          radius: 3, color: COLOR_DRAW, weight: 1,
          fillColor: COLOR_DRAW, fillOpacity: 1, interactive: false
        }).addTo(g);
      });
    }
    if (state.draw.centre) {
      L.circleMarker(state.draw.centre, {
        radius: 6, color: COLOR_CENTRE, weight: 2,
        fillColor: COLOR_CENTRE, fillOpacity: 0.35, interactive: false
      }).addTo(g);
    }
    updateDrawStatus();
  }

  function drawPreview(points) {
    state.layers.preview.clearLayers();
    var good = 0;
    var bad = 0;
    var bounds = [];
    points.forEach(function (p) {
      var lat = Array.isArray(p) ? num(p[0]) : num(p && p.lat);
      var lon = Array.isArray(p) ? num(p[1]) : num(p && (p.lon !== undefined ? p.lon : p.lng));
      if (lat === null || lon === null || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        bad++;
        return;
      }
      L.circleMarker([lat, lon], {
        radius: 3,
        color: COLOR_PREVIEW,
        weight: 1,
        fillColor: COLOR_PREVIEW,
        fillOpacity: 0.85,
        interactive: false        // hundreds of markers stay cheap
      }).addTo(state.layers.preview);
      bounds.push([lat, lon]);
      good++;
    });
    if (bad > 0) {
      log('warn', 'render: dropped ' + bad + ' point(s) that were not a valid [lat, lon] pair');
    }
    if (bounds.length > 0) {
      state.map.fitBounds(L.latLngBounds(bounds), { padding: [40, 40], maxZoom: 17 });
    }
    return good;
  }

  function clearPreview() {
    if (state.layers.preview) { state.layers.preview.clearLayers(); }
    state.preview.count = null;
    dom.previewCount.textContent = '--';
  }

  function clearDrawing() {
    state.draw.kind = null;
    state.draw.vertices = [];
    state.draw.strokes = [];
    state.draw.closed = false;
    state.draw.centre = null;
    redrawDrawing();
    disarm();
  }

  function updateDrawStatus() {
    var d = state.draw;
    var parts = [];
    if (d.kind === 'freehand') {
      if (d.strokes.length) {
        var total = d.strokes.reduce(function (n, s2) { return n + s2.length; }, 0);
        parts.push(d.strokes.length + ' stroke' + (d.strokes.length === 1 ? '' : 's') +
                   ', ' + total + ' points');
      }
    } else if (d.vertices.length > 0) {
      parts.push(d.vertices.length + ' ' + 'vertices' + (d.closed ? ', closed' : ', open'));
    }
    if (d.centre) {
      parts.push('centre ' + d.centre[0].toFixed(5) + ', ' + d.centre[1].toFixed(5));
    }
    if (d.image) { parts.push('image ' + d.image.filename); }
    dom.drawStatus.textContent = parts.length ? parts.join(' | ') : 'No shape defined.';
  }

  /* ===================================================================== */
  /* Tools                                                                  */
  /* ===================================================================== */

  var TOOL_HINTS = {
    polygon: 'Click the map to add vertices. Double-click, or press close polygon, to finish.',
    freehand: 'Press and drag to trace a stroke. Drag again to add more strokes; they render together.',
    text: 'Type the string, then click the map to place its centre. Rasterizing happens in the backend.',
    image: 'Choose a PNG or SVG, then click the map to place its centre.'
  };

  function setTool(tool) {
    var next = (state.tool === tool) ? null : tool;

    /* Polygon and freehand both live in draw.vertices. Switching between them
     * would leave the other tool's geometry behind, so drop it. */
    if ((next === 'polygon' || next === 'freehand') && state.draw.kind && state.draw.kind !== next) {
      state.draw.kind = null;
      state.draw.vertices = [];
      state.draw.strokes = [];
      state.draw.closed = false;
    }

    state.tool = next;

    Array.prototype.forEach.call(document.querySelectorAll('.tool-btn'), function (b) {
      b.classList.toggle('active', b.dataset.tool === state.tool);
      b.setAttribute('aria-pressed', b.dataset.tool === state.tool ? 'true' : 'false');
    });

    show(dom.textField, state.tool === 'text');
    show(dom.imageField, state.tool === 'image');
    dom.closePolygon.disabled = state.tool !== 'polygon';

    dom.toolHint.textContent = state.tool
      ? TOOL_HINTS[state.tool]
      : 'No tool active. Pick one to define a shape.';

    if (state.map) {
      state.map.getContainer().style.cursor = state.tool ? 'crosshair' : '';
    }
    redrawDrawing();
    disarm();
  }

  /* ===================================================================== */
  /* Shape spec                                                             */
  /* ===================================================================== */

  function reject(reason) {
    log('error', reason);
    return null;
  }

  function centroidOf(points) {
    var lat = 0, lon = 0;
    points.forEach(function (p) { lat += p[0]; lon += p[1]; });
    return [lat / points.length, lon / points.length];
  }

  /* Assumed request body for /api/render and /api/transmit. Backend is not
   * written yet, so this is the shape the frontend commits to. */
  /* Radio profiles.
   *
   * "private" is the default and the recommended one for testing against a
   * regular Meshtastic node: standard LongFast radio settings so a stock node
   * can demodulate, but a channel name and a freshly generated key that only
   * your own node holds, so no other device decodes the synthetic nodes.
   *
   * "meshtastic_public" is the actual public default channel (LongFast, the
   * well-known default key, channel hash 0x08). It is what every stock device
   * powers up on, so a run on it in a populated area reaches strangers, not just
   * you. It carries a warning and is never the default.
   */
  var SHARED_CHANNEL_WARN =
    ' is a shared channel you do not own. Anything you transmit lands in the ' +
    'node list of every Meshtastic device in radio range and is rebroadcast by ' +
    'their radios. Use it only in RF isolation (a shielded enclosure, or with no ' +
    'other nodes in range) or against a node you own, at the lowest power that ' +
    'works. In a populated area this is injecting into a mesh you do not own.';

  var PROFILES = {
    private: {
      label: 'Private test (LongFast, your own key)',
      region: 'US',
      preset: 'LONG_FAST',
      channelName: 'meshcanvas',
      channelNum: null,
      psk: null,
      generate: true,
      hopLimit: 3,
      precisionBits: 32,
      warn: null
    },
    meshtastic_public: {
      label: 'Meshtastic public default (LongFast)',
      region: 'US',
      preset: 'LONG_FAST',
      channelName: 'LongFast',
      channelNum: null,
      psk: null,
      hopLimit: 3,
      precisionBits: 32,
      warn: 'The Meshtastic public default channel (LongFast)' + SHARED_CHANNEL_WARN
    }
  };

  function applyProfile(key) {
    var p = PROFILES[key];
    if (!p) { return; }

    state.region = p.region;
    state.preset = p.preset;
    state.channelName = p.channelName;
    state.channelNum = p.channelNum;
    state.hopLimit = p.hopLimit;
    state.precisionBits = p.precisionBits;
    /* Setting dom.psk.value does not fire the input event, so state.psk must be
     * assigned here too. The transmit request reads state.psk, not the field:
     * miss this and the profile's key stays visible in the box but the default
     * PSK goes on the air, which is a wrong key AND a wrong channel hash. */
    state.psk = p.psk || '';

    dom.region.value = p.region;
    dom.preset.value = p.preset;
    dom.channelName.value = p.channelName;
    dom.channelNum.value = (p.channelNum === null) ? '' : String(p.channelNum);
    dom.psk.value = p.psk || '';

    /* A private profile with no fixed key gets a fresh random one, so it lands
     * on a channel only this operator holds rather than silently falling back
     * to the public default key. generatePsk sets both dom.psk and state.psk. */
    if (p.generate && !state.psk) {
      generatePsk();
      log('info', 'private channel "' + p.channelName + '": set your receiving ' +
                  'node to this channel name and this key, then transmit.');
    }

    dom.profileWarn.classList.toggle('hidden', !p.warn);
    if (p.warn) {
      dom.profileWarn.textContent = p.warn;
      log('warn', 'profile ' + p.label + ' selected: ' + p.warn);
    }

    log('info', 'profile: ' + p.label + ' (' + p.region + ' / ' + p.preset +
                ', slot ' + (p.channelNum || 'from name') + ')');
    disarm();
    scheduleBudget();
  }

  function buildShapeSpec() {
    var tool = state.tool;
    var d = state.draw;

    if (!tool) {
      return reject('no draw tool is active: pick polygon, freehand, text or image first');
    }

    var shape;
    var centre;

    if (tool === 'freehand') {
      if (d.kind !== tool || !d.strokes.length) {
        return reject('freehand: drag on the map to trace at least one stroke first');
      }
      shape = { type: tool, paths: d.strokes.map(function (s2) { return s2.slice(); }) };
      centre = d.centre || centroidOf([].concat.apply([], d.strokes));
    } else if (tool === 'polygon') {
      if (d.kind !== tool || d.vertices.length < 3) {
        return reject('polygon: needs at least 3 points on the map before it can be rendered');
      }
      shape = { type: tool, vertices: d.vertices.slice() };
      centre = d.centre || centroidOf(d.vertices);
    } else if (tool === 'text') {
      var text = dom.shapeText.value.trim();
      if (!text) { return reject('text tool: the string is empty'); }
      shape = { type: 'text', text: text };
      centre = d.centre || mapCentre();
    } else if (tool === 'image') {
      if (!d.image) { return reject('image tool: no PNG or SVG has been loaded'); }
      shape = { type: 'image', image: d.image };
      centre = d.centre || mapCentre();
    } else {
      return reject('unknown tool: ' + tool);
    }

    return {
      shape: shape,
      center: centre,
      scale_m: state.scaleM,
      node_count: state.nodeCount
    };
  }

  function mapCentre() {
    var c = state.map.getCenter();
    return [c.lat, c.lng];
  }

  function specSummary(spec) {
    return 'shape=' + spec.shape.type +
           ' nodes=' + spec.node_count +
           ' scale=' + spec.scale_m + ' m' +
           ' centre=' + spec.center[0].toFixed(5) + ',' + spec.center[1].toFixed(5);
  }

  /* ===================================================================== */
  /* Preview                                                                */
  /* ===================================================================== */

  function onRender() {
    var spec = buildShapeSpec();
    if (!spec) { return; }

    dom.render.disabled = true;
    log('info', 'POST /api/render ' + specSummary(spec));

    api('/api/render', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(spec)
    }, 'render').then(function (res) {
      dom.render.disabled = false;
      if (!res.ok) {
        log('error', res.error);
        return;
      }
      var pts = res.data.points;
      if (!Array.isArray(pts)) {
        log('error', 'render: response has no points array; keys were ' +
                     Object.keys(res.data).join(', '));
        return;
      }
      var drawn = drawPreview(pts);
      var reported = num(res.data.node_count);
      state.preview.count = drawn;
      dom.previewCount.textContent = String(drawn);
      log('ok', 'render: ' + drawn + ' preview points drawn' +
                (reported !== null && reported !== drawn
                  ? ' (backend reported node_count=' + reported + ')' : ''));
    });
  }

  /* ===================================================================== */
  /* Airtime budget                                                         */
  /* ===================================================================== */

  function scheduleBudget() {
    if (state.budgetTimer) { clearTimeout(state.budgetTimer); }
    state.budgetTimer = setTimeout(refreshBudget, BUDGET_DEBOUNCE_MS);
  }

  function refreshBudget() {
    if (state.budgetTimer) { clearTimeout(state.budgetTimer); state.budgetTimer = null; }

    var q = new URLSearchParams({
      region: state.region,
      modem_preset: state.preset,
      channel_name: state.channelName,
      node_count: String(state.nodeCount),
      tx_power_dbm: String(state.txPowerDbm),
      airtime_target_percent: String(state.airtimeTarget)
    });
    if (state.channelNum !== null) { q.set('channel_num', String(state.channelNum)); }
    if (state.psk) { q.set('psk_base64', state.psk); }

    api('/api/budget?' + q.toString(), { method: 'GET' }, 'budget').then(function (res) {
      if (!res.ok) {
        clearBudget();
        logThrottled('error', res.error + ' (budget panel left blank)', 10000);
        renderBudget();
        return;
      }
      applyBudget(res.data);
      renderBudget();
    });
  }

  function clearBudget() {
    state.budget = {
      toaMs: null, totalMs: null, duty: null, limit: null,
      etaS: null, freqHz: null, powerLimit: null, packets: null,
      target: null, gapMs: null
    };
  }

  function applyBudget(d) {
    var freqHz = num(d.frequency_hz);
    if (freqHz === null && num(d.frequency_mhz) !== null) {
      freqHz = num(d.frequency_mhz) * 1e6;
    }
    state.budget = {
      toaMs: num(d.toa_ms_per_packet),
      totalMs: num(d.total_airtime_ms),
      duty: num(d.duty_cycle_percent),
      limit: num(d.region_duty_cycle_limit),
      etaS: num(d.eta_seconds),
      target: num(d.airtime_target_percent),
      gapMs: num(d.inter_packet_ms),
      /* Optional extras. Absent means "--", never a locally invented value:
       * the slot maths belongs to the backend, and a frequency this page made
       * up would be a frequency nothing on the mesh is listening to. */
      freqHz: freqHz,
      powerLimit: num(d.region_power_limit_dbm),
      packets: num(d.packet_count)
    };
  }

  function renderBudget() {
    var b = state.budget;

    dom.toa.textContent = fmtMs(b.toaMs);
    dom.totalAirtime.textContent = fmtMs(b.totalMs);
    dom.duty.textContent = fmtPercent(b.duty);
    dom.dutyLimit.textContent = fmtPercent(b.limit);
    dom.dutyTarget.textContent = fmtPercent(b.target);
    dom.gap.textContent = fmtMs(b.gapMs);
    dom.eta.textContent = fmtDuration(b.etaS);
    dom.frequency.textContent = fmtFreq(b.freqHz);

    dom.dutyRow.classList.remove('state-ok', 'state-warn', 'state-err');
    if (b.duty !== null && b.limit !== null) {
      if (b.duty > b.limit) {
        dom.dutyRow.classList.add('state-err');
      } else if (b.duty > b.limit * 0.8) {
        dom.dutyRow.classList.add('state-warn');
      } else {
        dom.dutyRow.classList.add('state-ok');
      }
    }

    renderPowerLimit();
    updateTransmitState();
  }

  function renderPowerLimit() {
    var limit = state.budget.powerLimit;
    dom.powerLimit.textContent = (limit === null) ? '--' : limit + ' dBm';
    if (limit !== null && state.txPowerDbm > limit) {
      dom.powerWarn.textContent = 'TX power ' + state.txPowerDbm + ' dBm is above the ' +
        state.region + ' limit of ' + limit + ' dBm. The backend clamps to the limit.';
      show(dom.powerWarn, true);
    } else {
      show(dom.powerWarn, false);
    }
  }

  function overDutyLimit() {
    var b = state.budget;
    return (b.duty !== null && b.limit !== null && b.duty > b.limit);
  }

  /* ===================================================================== */
  /* Transmit, confirm, abort                                               */
  /* ===================================================================== */

  function updateTransmitState() {
    var over = overDutyLimit();

    show(dom.dutyAlert, over);
    show(dom.overrideWrap, over);
    if (!over && state.dutyOverride) {
      /* Do not let an override survive out of sight: if the projection drops
       * back under the limit the deliberate act has to be repeated. */
      state.dutyOverride = false;
      dom.dutyOverride.checked = false;
    }

    var reason = null;
    if (state.running) {
      reason = 'a run is active; abort it before starting another';
    } else if (over && !state.dutyOverride) {
      reason = 'projected duty cycle ' + fmtPercent(state.budget.duty) +
               ' exceeds the ' + state.region + ' limit of ' + fmtPercent(state.budget.limit) +
               '. Tick the override to proceed anyway.';
    }

    dom.transmit.disabled = (reason !== null);
    dom.transmitBlock.textContent = reason || '';
    show(dom.transmitBlock, reason !== null);

    dom.transmit.textContent = (state.mode === 'rf') ? 'transmit (rf)' : 'transmit (' + state.mode + ')';
  }

  function onTransmit() {
    var spec = buildShapeSpec();
    if (!spec) { return; }

    if (state.mode === 'rf') {
      arm(spec);
      return;
    }
    doTransmit(spec);
  }

  /* Two-step confirmation for rf, inline. No window.confirm and no modal:
   * browser dialogs block automation and cannot be read back from the page. */
  function arm(spec) {
    state.armed = true;
    state.armedSpec = spec;

    dom.confirmFreq.textContent = fmtFreq(state.budget.freqHz);
    dom.confirmPower.textContent = state.txPowerDbm + ' dBm';
    dom.confirmAirtime.textContent = fmtMs(state.budget.totalMs);
    dom.confirmPackets.textContent = (state.budget.packets === null)
      ? '-- (' + spec.node_count + ' nodes requested)'
      : String(state.budget.packets);

    show(dom.rfConfirm, true);
    dom.rfConfirmGo.focus();
    log('warn', 'rf transmit armed: ' + fmtFreq(state.budget.freqHz) + ' at ' +
                state.txPowerDbm + ' dBm, ' + specSummary(spec) +
                '. Confirm to key the transmitter.');
    if (state.budget.freqHz === null) {
      log('warn', 'the backend has not reported a frequency; the confirmation cannot name one');
    }
  }

  /* Any parameter change invalidates an armed confirmation. The user must
   * confirm the values that are actually on screen. */
  function disarm(silent) {
    if (!state.armed) { return; }
    state.armed = false;
    state.armedSpec = null;
    show(dom.rfConfirm, false);
    if (!silent) { log('info', 'rf confirmation cancelled: parameters changed'); }
  }

  function onConfirmGo() {
    if (!state.armed || !state.armedSpec) { return; }
    var spec = state.armedSpec;
    disarm(true);
    doTransmit(spec);
  }

  function transmitBody(spec) {
    var body = {
      shape: spec.shape,
      center: spec.center,
      scale_m: spec.scale_m,
      node_count: spec.node_count,
      mode: state.mode,
      region: state.region,
      modem_preset: state.preset,
      channel_name: state.channelName,
      tx_power_dbm: state.txPowerDbm,
      duty_cycle_override: state.dutyOverride,
      airtime_target_percent: state.airtimeTarget,
      channel_num: state.channelNum,
      hop_limit: state.hopLimit,
      precision_bits: state.precisionBits
    };
    /* The PSK goes on the wire to the backend and nowhere else. It is never
     * passed to log(), console, or any error string. The field name must match
     * the RadioSettings model exactly: a mismatch is dropped by the backend and
     * the default key is used silently, which puts a wrong channel hash on air. */
    if (state.psk) { body.psk_base64 = state.psk; }
    return body;
  }

  function doTransmit(spec) {
    log('tx', 'POST /api/transmit mode=' + state.mode +
              ' region=' + state.region +
              ' preset=' + state.preset +
              ' power=' + state.txPowerDbm + ' dBm' +
              ' channel=' + (state.channelName || '(preset default)') +
              ' psk=' + (state.psk ? 'set by user' : 'backend default') +
              ' ' + specSummary(spec));

    if (state.ws.status !== 'open') {
      log('warn', 'websocket is not connected: progress and completion events will not appear here');
    }

    dom.transmit.disabled = true;
    api('/api/transmit', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(transmitBody(spec))
    }, 'transmit').then(function (res) {
      if (!res.ok) {
        state.running = false;
        log('error', res.error);
        updateTransmitState();
        return;
      }
      state.running = true;
      var runId = res.data.run_id || res.data.id || null;
      log('ok', 'run started' + (runId ? ' (run ' + runId + ')' : '') +
                '. Abort is available in the log header.');
      updateTransmitState();
    });
  }

  function onAbort() {
    log('warn', 'POST /api/abort requested by operator');
    api('/api/abort', { method: 'POST', headers: JSON_HEADERS, body: '{}' }, 'abort')
      .then(function (res) {
        if (!res.ok) {
          log('error', res.error);
          log('error', 'ABORT DID NOT REACH THE BACKEND. If a run is live, stop it at the radio.');
          return;
        }
        state.running = false;
        updateTransmitState();
        log('ok', 'abort acknowledged by the backend');
      });
  }

  /* ===================================================================== */
  /* WebSocket                                                              */
  /* ===================================================================== */

  function wsUrl() {
    if (API_BASE) { return API_BASE.replace(/^http/, 'ws') + '/ws'; }
    var scheme = (window.location.protocol === 'https:') ? 'wss:' : 'ws:';
    return scheme + '//' + window.location.host + '/ws';
  }

  function setWsState(status, text) {
    state.ws.status = status;
    dom.wsState.className = 'ws-state ws-' + status;
    dom.wsState.textContent = text;
  }

  function wsConnect() {
    if (state.ws.timer) { clearTimeout(state.ws.timer); state.ws.timer = null; }

    var existing = state.ws.sock;
    if (existing && (existing.readyState === WebSocket.CONNECTING ||
                     existing.readyState === WebSocket.OPEN)) {
      return;
    }

    var url = wsUrl();
    setWsState('connecting', 'socket: connecting');

    var sock;
    try {
      sock = new WebSocket(url);
    } catch (err) {
      log('error', 'websocket could not be created for ' + url + ': ' +
                   ((err && err.message) ? err.message : String(err)));
      setWsState('closed', 'socket: failed');
      scheduleReconnect();
      return;
    }
    state.ws.sock = sock;

    sock.onopen = function () {
      state.ws.attempts = 0;
      setWsState('open', 'socket: connected');
      log('ok', 'websocket connected: ' + url);
    };
    sock.onmessage = onWsMessage;
    /* onerror carries no useful detail in browsers and is always followed by
     * onclose, so the reporting lives there and is not duplicated. */
    sock.onerror = function () { };
    sock.onclose = function (ev) {
      setWsState('closed', 'socket: closed');
      scheduleReconnect(ev);
    };
  }

  function scheduleReconnect(ev) {
    var attempt = state.ws.attempts;
    state.ws.attempts = attempt + 1;

    var delay = Math.min(WS_BACKOFF_MAX_MS, 1000 * Math.pow(2, attempt));
    delay += Math.floor(Math.random() * 400);   // jitter

    /* Log the first few, then thin out: a backend that is simply not running
     * yet must not fill the console. */
    if (attempt < 3 || attempt % 5 === 0) {
      log('warn', 'websocket closed' +
                  (ev && ev.code ? ' (code ' + ev.code + ')' : '') +
                  '; retry ' + (attempt + 1) + ' in ' + (delay / 1000).toFixed(1) + ' s');
    }
    setWsState('closed', 'socket: retry in ' + Math.round(delay / 1000) + ' s');
    state.ws.timer = setTimeout(wsConnect, delay);
  }

  function onWsMessage(ev) {
    var msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (err) {
      log('warn', 'unparsable websocket frame: ' + String(ev.data).slice(0, 200));
      return;
    }
    if (!msg || typeof msg !== 'object') {
      log('warn', 'unexpected websocket payload: ' + String(ev.data).slice(0, 200));
      return;
    }

    switch (msg.type) {
      case 'log':
        log(normaliseLevel(msg.level), String(msg.message !== undefined ? msg.message : ev.data));
        break;

      case 'progress':
        updateProgress(msg);
        break;

      case 'done':
        state.running = false;
        updateTransmitState();
        dom.progress.textContent = 'done';
        log('ok', 'run finished' + (msg.message ? ': ' + msg.message : '') +
                  (num(msg.sent) !== null ? ' (' + msg.sent + ' packets sent)' : ''));
        break;

      case 'error':
        state.running = false;
        updateTransmitState();
        log('error', 'backend error: ' + (msg.message || JSON.stringify(redact(msg))));
        break;

      default:
        log('info', 'event: ' + JSON.stringify(redact(msg)).slice(0, 300));
    }
  }

  function normaliseLevel(level) {
    var l = String(level || 'info').toLowerCase();
    if (l === 'warning') { l = 'warn'; }
    if (['info', 'warn', 'error', 'ok', 'tx'].indexOf(l) === -1) { l = 'info'; }
    return l;
  }

  function updateProgress(msg) {
    var sent = num(msg.sent);
    if (sent === null) { sent = num(msg.current); }
    var total = num(msg.total);
    var pct = num(msg.percent);

    if (sent !== null && total !== null && total > 0) {
      dom.progress.textContent = sent + '/' + total +
                                 ' (' + ((sent / total) * 100).toFixed(0) + ' %)';
    } else if (pct !== null) {
      dom.progress.textContent = pct.toFixed(0) + ' %';
    } else if (sent !== null) {
      dom.progress.textContent = sent + ' sent';
    }

    if (!state.running) {
      state.running = true;      // progress implies a run the page did not start
      updateTransmitState();
    }
    if (msg.message) { log('info', String(msg.message)); }
  }

  /* ===================================================================== */
  /* Inputs                                                                 */
  /* ===================================================================== */

  function setMode(mode) {
    if (!MODES[mode]) { return; }
    state.mode = mode;
    document.body.className = 'mode-' + mode;
    dom.modeTag.textContent = MODES[mode].tag;
    dom.modeText.textContent = MODES[mode].text;
    dom.modeHint.textContent = MODES[mode].hint;
    disarm(true);
    show(dom.rfConfirm, false);
    updateTransmitState();
    log(mode === 'rf' ? 'warn' : 'info', 'mode set to ' + mode);
  }

  function setTxPower(value) {
    var v = Math.round(num(value) === null ? state.txPowerDbm : num(value));
    v = Math.max(0, Math.min(30, v));
    state.txPowerDbm = v;
    dom.txPower.value = String(v);
    dom.txPowerNum.value = String(v);
    renderPowerLimit();
  }

  function generatePsk() {
    if (!window.crypto || !window.crypto.getRandomValues) {
      log('error', 'crypto.getRandomValues is unavailable in this context; PSK not generated');
      return;
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    var bin = '';
    for (var i = 0; i < bytes.length; i++) { bin += String.fromCharCode(bytes[i]); }
    var b64 = window.btoa(bin);

    dom.psk.value = b64;
    state.psk = b64;
    disarm();
    /* The value itself is deliberately not logged. */
    log('info', 'generated a 16-byte PSK with crypto.getRandomValues; it is shown in the field only');
  }

  function onImageChange(ev) {
    var file = ev.target.files && ev.target.files[0];
    if (!file) {
      state.draw.image = null;
      dom.imageStatus.textContent = 'No file selected.';
      updateDrawStatus();
      return;
    }
    var typeOk = (file.type === 'image/png' || file.type === 'image/svg+xml') ||
                 /\.(png|svg)$/i.test(file.name);
    if (!typeOk) {
      log('error', 'unsupported image type: ' + (file.type || 'unknown') + '. PNG or SVG only.');
      ev.target.value = '';
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      log('error', 'image is ' + fmtBytes(file.size) + ', over the ' +
                   fmtBytes(MAX_IMAGE_BYTES) + ' limit for a request body');
      ev.target.value = '';
      return;
    }

    var reader = new FileReader();
    reader.onerror = function () { log('error', 'could not read ' + file.name); };
    reader.onload = function () {
      var url = String(reader.result);
      var comma = url.indexOf(',');
      if (comma === -1) {
        log('error', 'unexpected FileReader output for ' + file.name);
        return;
      }
      state.draw.image = {
        filename: file.name,
        mime: file.type || (/\.svg$/i.test(file.name) ? 'image/svg+xml' : 'image/png'),
        data_base64: url.slice(comma + 1)
      };
      dom.imageStatus.textContent = file.name + ' (' + fmtBytes(file.size) + ') loaded';
      updateDrawStatus();
      disarm();
    };
    reader.readAsDataURL(file);
  }

  /* ===================================================================== */
  /* Wiring                                                                 */
  /* ===================================================================== */

  function cacheDom() {
    dom.modeTag = $('mode-banner-tag');
    dom.modeText = $('mode-banner-text');
    dom.modeHint = $('mode-hint');

    dom.region = $('region');
    dom.preset = $('preset');
    dom.frequency = $('frequency');
    dom.txPower = $('tx-power');
    dom.txPowerNum = $('tx-power-num');
    dom.powerLimit = $('power-limit');
    dom.powerWarn = $('power-warn');

    dom.channelName = $('channel-name');
    dom.psk = $('psk');
    dom.pskToggle = $('psk-toggle');
    dom.pskGenerate = $('psk-generate');

    dom.nodeCount = $('node-count');
    dom.nodeCountOut = $('node-count-out');
    dom.scale = $('scale');
    dom.scaleOut = $('scale-out');

    dom.toolHint = $('tool-hint');
    dom.textField = $('text-field');
    dom.shapeText = $('shape-text');
    dom.imageField = $('image-field');
    dom.shapeImage = $('shape-image');
    dom.imageStatus = $('image-status');
    dom.closePolygon = $('close-polygon');
    dom.clearDrawing = $('clear-drawing');
    dom.drawStatus = $('draw-status');

    dom.render = $('render');
    dom.clearPreview = $('clear-preview');
    dom.previewCount = $('preview-count');

    dom.toa = $('toa');
    dom.totalAirtime = $('total-airtime');
    dom.duty = $('duty');
    dom.dutyRow = $('duty-row');
    dom.dutyLimit = $('duty-limit');
    dom.eta = $('eta');
    dom.refreshBudget = $('refresh-budget');
    dom.airtimeTarget = $('airtime-target');
    dom.profile = $('profile');
    dom.profileWarn = $('profile-warn');
    dom.channelNum = $('channel-num');
    dom.dutyTarget = $('duty-target');
    dom.gap = $('gap');
    dom.dutyAlert = $('duty-alert');
    dom.overrideWrap = $('override-wrap');
    dom.dutyOverride = $('duty-override');

    dom.transmit = $('transmit');
    dom.transmitBlock = $('transmit-block');
    dom.rfConfirm = $('rf-confirm');
    dom.confirmFreq = $('confirm-freq');
    dom.confirmPower = $('confirm-power');
    dom.confirmAirtime = $('confirm-airtime');
    dom.confirmPackets = $('confirm-packets');
    dom.rfConfirmGo = $('rf-confirm-go');
    dom.rfConfirmCancel = $('rf-confirm-cancel');

    dom.wsState = $('ws-state');
    dom.progress = $('progress');
    dom.reconnect = $('reconnect');
    dom.clearLog = $('clear-log');
    dom.abort = $('abort');
    dom.log = $('log');
  }

  function wire() {
    Array.prototype.forEach.call(document.querySelectorAll('input[name="mode"]'), function (r) {
      r.addEventListener('change', function () {
        if (r.checked) { setMode(r.value); }
      });
    });

    dom.region.addEventListener('change', function () {
      state.region = dom.region.value;
      disarm();
      scheduleBudget();
    });

    dom.preset.addEventListener('change', function () {
      state.preset = dom.preset.value;
      disarm();
      scheduleBudget();
    });

    dom.txPower.addEventListener('input', function () {
      setTxPower(dom.txPower.value);
      disarm();
      scheduleBudget();
    });
    dom.txPowerNum.addEventListener('change', function () {
      setTxPower(dom.txPowerNum.value);
      disarm();
      scheduleBudget();
    });

    dom.channelName.addEventListener('input', function () {
      state.channelName = dom.channelName.value;
      disarm();
      scheduleBudget();      // the name selects the frequency slot
    });

    dom.psk.addEventListener('input', function () {
      state.psk = dom.psk.value;
      disarm();
    });

    dom.pskToggle.addEventListener('click', function () {
      var showing = dom.psk.type === 'text';
      dom.psk.type = showing ? 'password' : 'text';
      dom.pskToggle.textContent = showing ? 'show' : 'hide';
      dom.pskToggle.setAttribute('aria-pressed', showing ? 'false' : 'true');
    });

    dom.pskGenerate.addEventListener('click', generatePsk);

    dom.nodeCount.addEventListener('input', function () {
      state.nodeCount = parseInt(dom.nodeCount.value, 10);
      dom.nodeCountOut.textContent = String(state.nodeCount);
      disarm();
      scheduleBudget();
    });

    dom.scale.addEventListener('input', function () {
      state.scaleM = parseInt(dom.scale.value, 10);
      dom.scaleOut.textContent = String(state.scaleM);
      disarm();
    });

    Array.prototype.forEach.call(document.querySelectorAll('.tool-btn'), function (b) {
      b.addEventListener('click', function () { setTool(b.dataset.tool); });
    });

    dom.shapeText.addEventListener('input', function () {
      state.draw.text = dom.shapeText.value;
      disarm();
    });
    dom.shapeImage.addEventListener('change', onImageChange);
    dom.closePolygon.addEventListener('click', closePolygon);
    dom.clearDrawing.addEventListener('click', function () {
      clearDrawing();
      log('info', 'drawing cleared');
    });

    dom.render.addEventListener('click', onRender);
    dom.clearPreview.addEventListener('click', function () {
      clearPreview();
      log('info', 'preview cleared');
    });

    dom.refreshBudget.addEventListener('click', refreshBudget);
    dom.profile.addEventListener('change', function () {
      state.profile = dom.profile.value;
      applyProfile(state.profile);
    });
    dom.channelNum.addEventListener('input', function () {
      var v = dom.channelNum.value.trim();
      state.channelNum = v === '' ? null : Number(v);
      disarm();
      scheduleBudget();
    });
    dom.airtimeTarget.addEventListener('change', function () {
      state.airtimeTarget = Number(dom.airtimeTarget.value);
      log('info', 'airtime target set to ' + state.airtimeTarget + ' %');
      disarm();
      scheduleBudget();
    });
    dom.dutyOverride.addEventListener('change', function () {
      state.dutyOverride = dom.dutyOverride.checked;
      disarm();
      updateTransmitState();
      if (state.dutyOverride) {
        log('warn', 'duty cycle override enabled deliberately by the operator');
      }
    });

    dom.transmit.addEventListener('click', onTransmit);
    dom.rfConfirmGo.addEventListener('click', onConfirmGo);
    dom.rfConfirmCancel.addEventListener('click', function () { disarm(); });

    dom.abort.addEventListener('click', onAbort);
    dom.reconnect.addEventListener('click', function () {
      state.ws.attempts = 0;
      if (state.ws.sock) {
        try { state.ws.sock.close(); } catch (e) { /* already gone */ }
      }
      log('info', 'manual websocket reconnect requested');
      wsConnect();
    });
    dom.clearLog.addEventListener('click', function () {
      dom.log.textContent = '';
    });
  }

  /* ===================================================================== */
  /* Init                                                                   */
  /* ===================================================================== */

  function init() {
    cacheDom();
    wire();

    /* Abort is a safety control. It stays reachable at all times rather than
     * being gated on a running flag this page could have lost track of. */
    dom.abort.disabled = false;

    setMode('dry-run');
    setTxPower(20);
    dom.nodeCountOut.textContent = String(state.nodeCount);
    dom.scaleOut.textContent = String(state.scaleM);
    dom.closePolygon.disabled = true;

    applyProfile(state.profile);   // boot on the private channel with a fresh key
    log('info', 'MeshCanvas frontend loaded. Mode is dry-run. Use only on a mesh you own.');
    if (FILE_ORIGIN) {
      log('warn', 'page opened over file://, so there is no same-origin backend. ' +
                  'Requests are directed at ' + API_BASE + '. Serve the page from the ' +
                  'MeshCanvas backend to use a different origin.');
    }

    if (typeof L === 'undefined') {
      log('error', 'Leaflet did not load (blocked CDN or failed integrity check). ' +
                   'The control panel still works; the map does not.');
    } else {
      initMap();
    }

    renderBudget();     // paints "--" everywhere until the backend answers
    refreshBudget();
    wsConnect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
