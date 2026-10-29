/* strata-kit.js: the page shell for a Strata scene built with strata_kit.h.

   Part of the Strata web kit (docs/WEB.md, "The web kit"). A classic script
   with no dependencies: copy it next to your .wasm and serve it from the same
   origin, with no CDN and no build step. It defines window.StrataKit.

     const kit = StrataKit.create({
       module,                  // the Emscripten module (StrataLoader.load gives one)
       canvas,                  // a <canvas> with an id
       camera: {yaw: 0.9, pitch: 0.6, dist: 30, tx: 0, ty: 0, tz: 0},
       frame(t, now, dt) {},    // write this frame's data into the module
       after(t, now) {},        // after the frame: place labels
       pick(x, y, radius) {},   // CSS pixels relative to the canvas -> a thing or null
       onTap(hit) {}, onHover(hit, x, y) {},
       idOf(hit) {},            // a stable string per thing, for tools and tests
       targets() {},            // [{id, x, y}] in CSS pixels, for the smoke test
     });
     kit.start();               // or, with ?drive=1, tools call kit.drive(t)

   What it does for the page:

   - Sizes the drawing buffer to the canvas's CSS size times the device pixel
     ratio, capped (dprCap), on every frame and on ResizeObserver.
   - Adaptive quality: when frames average over slowMs, first lowers the
     pixel density towards 1, then the quality tier (shadows), with a settle
     window after each step. A hidden tab resets the measurement. ?quality=
     pins a tier; ?fps=1 shows what it settled on and the frame's stats.
   - One orbit/pan/zoom controller: pointer capture, drag to orbit,
     right-drag or shift-drag or two fingers to pan, pinch and wheel to zoom,
     arrow keys and +/-, and a slow idle orbit (none under reduced motion).
     A tap is a pointer that moved under 6 CSS pixels.
   - project() and pick() in CSS pixels. Pointer coordinates are never scaled
     by the drawing buffer's size, so the device pixel ratio (and its cap)
     never enters picking.
   - webglcontextlost / webglcontextrestored: the loop stops, and on restore
     the engine is rebuilt in the same canvas (sk_restore) and the host's
     onRestore runs. A context that is lost twice in a minute, or does not
     come back in 5 seconds, calls onFatal("context-lost").
   - window.__strataKit: drive(t) (draws scene time t synchronously; the loop
     does not run under ?drive=1), pickAt, tapAt, lastTapId, targets, stats,
     loseContext/restoreContext, for tools such as testing/smoke.cjs.
   - StrataKit.placeLabels: label placement with collision and hysteresis.
   - StrataKit.store: localStorage that never throws.

   MIT licensed, as Strata. */
(function () {
  "use strict";
  const KIT_VERSION = "1.1";

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window, blocked storage */ } },
  };

  function params() {
    try { return new URLSearchParams(location.search); } catch (e) { return new URLSearchParams(""); }
  }

  function create(opts) {
    const mod = opts.module, canvas = opts.canvas;
    if (!mod || typeof mod._sk_init !== "function") throw new Error("strata-kit: the module has no sk_init export");
    if (!canvas || !canvas.id) throw new Error("strata-kit: the canvas needs an id");
    const q = params();
    const reducedMotion = opts.reducedMotion != null ? !!opts.reducedMotion
      : matchMedia("(prefers-reduced-motion: reduce)").matches;
    const coarse = matchMedia("(pointer: coarse)").matches;
    const wasmKit = mod._sk_kit_version && mod.UTF8ToString ? mod.UTF8ToString(mod._sk_kit_version()) : null;
    if (wasmKit && wasmKit.split(".")[0] !== KIT_VERSION.split(".")[0]) {
      console.warn(`strata-kit: page kit ${KIT_VERSION}, wasm kit ${wasmKit}; rebuild one of them`);
    }
    // A 1.0 wasm's sk_stats has no mesh count (kit 1.1).
    const statsHaveMeshes = !!wasmKit && (parseInt(wasmKit.split(".")[0], 10) > 1 || parseInt(wasmKit.split(".")[1], 10) >= 1);

    const forced = q.get("quality");
    const forcedQuality = forced != null && forced !== "" ? Math.min(Math.max(parseInt(forced, 10) || 0, 0), 2) : null;
    const driven = q.get("drive") === "1" || !!opts.driven;
    // The page's own camera and limits objects are used (and given defaults
    // for what they leave out), so the page can move the camera by writing
    // to them: kit.cam === opts.camera.
    const withDefaults = (o, d) => { for (const k of Object.keys(d)) if (o[k] == null) o[k] = d[k]; return o; };
    const limits = withDefaults(opts.limits || {}, {minPitch: 0.12, maxPitch: 1.45, minDist: 4, maxDist: 120});
    const cam = withDefaults(opts.camera || {}, {yaw: 0.8, pitch: 0.6, dist: 30, tx: 0, ty: 0, tz: 0});
    const slowMs = opts.slowMs || 28, windowMs = opts.windowMs || 1500, settleMs = opts.settleMs || 2500;
    const idle = opts.idleOrbit === false ? null : Object.assign({afterMs: 20000, rate: 0.0006}, opts.idleOrbit || {});

    const kit = {
      module: mod, canvas, cam, limits, coarse, reducedMotion, driven,
      quality: forcedQuality != null ? forcedQuality : (opts.quality != null ? opts.quality : (coarse ? 1 : 2)),
      forcedQuality,
      dprCap: opts.dprCap || 2,
      dpr: 1,
      lastInput: performance.now(),
      lastTapId: null,
      selected: null,       // the host may set this; the idle orbit pauses while it is set
      running: false,
      lost: false,
      fatal: false,
      version: KIT_VERSION,
    };
    const minDprCap = 1;
    const onFatal = opts.onFatal || ((code, msg) => console.error("strata-kit:", code, msg));

    /* --------------------------------------------------------- sizing */
    kit.resize = function () {
      kit.dpr = Math.min(window.devicePixelRatio || 1, kit.dprCap);
      const w = Math.max(1, Math.round(canvas.clientWidth * kit.dpr));
      const h = Math.max(1, Math.round(canvas.clientHeight * kit.dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w; canvas.height = h;
        if (kit.ready) mod._sk_resize(w, h);
      }
    };
    if (typeof ResizeObserver === "function") new ResizeObserver(() => kit.resize()).observe(canvas);

    kit.resize();
    const sel = mod.stringToNewUTF8("#" + canvas.id);
    const rc = mod._sk_init(sel, canvas.width, canvas.height, kit.quality, reducedMotion ? 1 : 0);
    mod._free(sel);
    if (rc !== 0) {
      const err = new Error(rc < 0 ? "no-webgl2" : "strata-status-" + rc);
      err.code = rc < 0 ? "no-webgl2" : "engine";
      err.status = rc;
      throw err;
    }
    kit.ready = true;
    kit.setQuality = function (n) { kit.quality = Math.min(Math.max(n, 0), 2); mod._sk_set_quality(kit.quality); };
    kit.setSun = function (unixSeconds, lat, lon) { return mod._sk_set_sun(unixSeconds, lat, lon); };

    /* ----------------------------------------------- project and pick */
    kit.viewProj = function () {
      const p = mod._sk_view_proj() >> 2;
      return mod.HEAPF32.subarray(p, p + 16);
    };
    // World -> CSS pixels relative to the canvas's top-left, plus clip w
    // (its depth), or null behind the camera.
    kit.project = function (x, y, z) {
      const m = kit.viewProj();
      const cx = m[0] * x + m[4] * y + m[8] * z + m[12];
      const cy = m[1] * x + m[5] * y + m[9] * z + m[13];
      const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
      if (cw <= 0.01) return null;
      return {x: (cx / cw * 0.5 + 0.5) * canvas.clientWidth,
              y: (1 - (cy / cw * 0.5 + 0.5)) * canvas.clientHeight, w: cw};
    };
    kit.hitRadius = function () { return opts.hitRadius ? opts.hitRadius(coarse) : (coarse ? 30 : 18); };
    // Client (viewport) coordinates -> the host's thing under them, or null.
    kit.pickAt = function (clientX, clientY, radius) {
      if (!opts.pick) return null;
      const r = canvas.getBoundingClientRect();
      return opts.pick(clientX - r.left, clientY - r.top, radius || kit.hitRadius()) || null;
    };
    const idOf = (hit) => (hit == null ? null : (opts.idOf ? opts.idOf(hit) : String(hit.id != null ? hit.id : hit)));
    kit.tapAt = function (clientX, clientY) {
      const hit = kit.pickAt(clientX, clientY);
      kit.lastTapId = idOf(hit);
      if (opts.onTap) opts.onTap(hit, clientX, clientY);
      return hit;
    };

    /* ----------------------------------------------------------- input */
    const pointers = new Map();
    let dragMoved = 0, pinchStart = null;
    const touch = () => { kit.lastInput = performance.now(); };
    kit.touch = touch;
    kit.clampCam = function () {
      cam.pitch = Math.min(Math.max(cam.pitch, limits.minPitch), limits.maxPitch);
      cam.dist = Math.min(Math.max(cam.dist, limits.minDist), limits.maxDist);
    };
    kit.pan = function (dx, dy) {
      const k = cam.dist / Math.max(canvas.clientHeight, 1) * 1.1;
      const sx = -Math.sin(cam.yaw), sz = Math.cos(cam.yaw);   // screen-right in world
      const fx = -Math.cos(cam.yaw), fz = -Math.sin(cam.yaw);  // screen-up (forward) in world
      cam.tx += (-dx * sx + dy * fx) * k;
      cam.tz += (-dx * sz + dy * fz) * k;
    };
    canvas.addEventListener("pointerdown", (e) => {
      try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* synthetic pointer */ }
      pointers.set(e.pointerId, {x: e.clientX, y: e.clientY, button: e.button, shift: e.shiftKey});
      dragMoved = 0;
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinchStart = {d: Math.hypot(a.x - b.x, a.y - b.y), dist: cam.dist, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2};
      }
      canvas.classList.add("dragging");
      touch();
    });
    canvas.addEventListener("pointermove", (e) => {
      const p = pointers.get(e.pointerId);
      if (!p) {
        if (e.pointerType === "mouse" && opts.onHover) opts.onHover(kit.pickAt(e.clientX, e.clientY), e.clientX, e.clientY);
        return;
      }
      const dx = e.clientX - p.x, dy = e.clientY - p.y;
      dragMoved += Math.abs(dx) + Math.abs(dy);
      p.x = e.clientX; p.y = e.clientY;
      if (pointers.size === 2 && pinchStart) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        cam.dist = pinchStart.dist * pinchStart.d / Math.max(d, 1);
        kit.pan(mx - pinchStart.mx, my - pinchStart.my);
        pinchStart.mx = mx; pinchStart.my = my;
      } else if (p.button === 2 || p.shift) {
        kit.pan(dx, dy);
      } else {
        cam.yaw += dx * 0.008;
        cam.pitch += dy * 0.006;
      }
      kit.clampCam();
      touch();
    });
    function endPointer(e) {
      const p = pointers.get(e.pointerId);
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchStart = null;
      if (!pointers.size) canvas.classList.remove("dragging");
      if (p && dragMoved < 6 && e.type === "pointerup") kit.tapAt(e.clientX, e.clientY);
    }
    canvas.addEventListener("pointerup", endPointer);
    canvas.addEventListener("pointercancel", endPointer);
    canvas.addEventListener("pointerleave", () => { if (opts.onHover) opts.onHover(null, 0, 0); });
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      cam.dist *= Math.exp(e.deltaY * 0.0012);
      kit.clampCam();
      touch();
    }, {passive: false});
    canvas.addEventListener("keydown", (e) => {
      if (opts.onKey && opts.onKey(e)) { e.preventDefault(); return; }
      const k = e.key;
      if (k === "ArrowLeft") cam.yaw -= 0.08;
      else if (k === "ArrowRight") cam.yaw += 0.08;
      else if (k === "ArrowUp") cam.pitch += 0.05;
      else if (k === "ArrowDown") cam.pitch -= 0.05;
      else if (k === "+" || k === "=") cam.dist *= 0.9;
      else if (k === "-" || k === "_") cam.dist *= 1.1;
      else return;
      e.preventDefault();
      kit.clampCam();
      touch();
    });

    /* --------------------------------------------------------- quality */
    let qStart = 0, qFrames = 0, settleUntil = performance.now() + settleMs;  // first frames compile shaders
    let slowSeen = false;
    const fpsEl = q.get("fps") === "1" ? document.body.appendChild(Object.assign(document.createElement("div"), {
      className: "sk-fps",
      style: "position:fixed;right:8px;bottom:8px;font:12px monospace;background:#000a;color:#fff;" +
             "padding:2px 6px;border-radius:4px;z-index:9;pointer-events:none",
    })) : null;
    document.addEventListener("visibilitychange", () => { qStart = 0; settleUntil = performance.now() + settleMs / 2; });
    kit.stats = function () {
      const s = mod._sk_stats() >> 2, h = mod.HEAP32;
      return {primitives: h[s], points: h[s + 1], lines: h[s + 2], lights: h[s + 3], terrain: h[s + 4],
              quality: h[s + 5], width: h[s + 6], height: h[s + 7], frames: h[s + 8], pointsRejected: h[s + 9],
              meshes: statsHaveMeshes ? h[s + 10] : 0,
              dpr: kit.dpr, dprCap: kit.dprCap};
    };
    function adaptQuality(now) {
      if (!qStart) { qStart = now; qFrames = 0; return; }
      qFrames++;
      if (now - qStart < windowMs) return;
      const ms = (now - qStart) / qFrames;
      kit.frameMs = ms;
      if (fpsEl) {
        const st = kit.stats();
        fpsEl.textContent = `${(1000 / ms).toFixed(0)} fps · q${kit.quality} · ${kit.dpr.toFixed(2)}x · ` +
          `${st.primitives} prims · ${st.meshes} meshes · ${st.points} pts · ${st.lines} lines · ${st.lights} lights · ${st.terrain} ground`;
      }
      qStart = now; qFrames = 0;
      if (forcedQuality != null || now < settleUntil || ms <= slowMs) return;
      if (kit.dpr > minDprCap + 0.01) {
        kit.dprCap = Math.max(minDprCap, kit.dpr * 0.75);
        console.log(`strata-kit: frames are slow (${ms.toFixed(0)} ms); pixel density ${kit.dprCap.toFixed(2)}`);
      } else if (kit.quality > 0) {
        kit.setQuality(kit.quality - 1);
        console.log(`strata-kit: frames are slow (${ms.toFixed(0)} ms); quality ${kit.quality}`);
      } else {
        // Nothing left to give up: tell the page once, so it can offer a
        // lighter view (a clip, a 2D page).
        if (!slowSeen && opts.onSlow) opts.onSlow({frameMs: ms});
        slowSeen = true;
        return;
      }
      settleUntil = now + settleMs;
    }

    /* ----------------------------------------------------------- frame */
    let lastNow = 0;
    function draw(t, now) {
      const dt = lastNow ? Math.min((now - lastNow) / 1000, 0.25) : 0;
      lastNow = now;
      if (opts.frame) opts.frame(t, now, dt);
      mod._sk_set_camera(cam.yaw, cam.pitch, cam.dist, cam.tx, cam.ty, cam.tz);
      mod._sk_frame(t);
      if (opts.after) opts.after(t, now);
    }
    function loop(now) {
      if (!kit.running || kit.lost) return;
      kit.resize();
      if (idle && !reducedMotion && now - kit.lastInput > idle.afterMs && !kit.selected) cam.yaw += idle.rate;
      draw(now / 1000, now);
      adaptQuality(now);
      requestAnimationFrame(loop);
    }
    kit.start = function () {
      if (driven || kit.running) return kit;
      kit.running = true;
      requestAnimationFrame(loop);
      return kit;
    };
    // For tools (?drive=1): draw scene time t (seconds) now, synchronously.
    // Everything the host animates must come from t for frames to repeat.
    kit.drive = function (t, o = {}) {
      kit.resize();
      if (o.yaw != null) cam.yaw = o.yaw;
      if (o.pitch != null) cam.pitch = o.pitch;
      if (o.dist != null) cam.dist = o.dist;
      draw(t, t * 1000);
      return kit.stats();
    };

    /* ------------------------------------------------- context loss */
    let lostAt = [], restoreTimer = 0;
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();  // without this the browser never restores it
      kit.lost = true;
      const now = performance.now();
      lostAt = lostAt.filter((x) => now - x < 60000).concat(now);
      if (opts.onLost) opts.onLost();
      clearTimeout(restoreTimer);
      if (lostAt.length > 2) return giveUp("The 3D view lost its graphics context again.");
      restoreTimer = setTimeout(() => giveUp("The 3D view lost its graphics context and it did not come back."), 5000);
    });
    canvas.addEventListener("webglcontextrestored", () => {
      clearTimeout(restoreTimer);
      if (kit.fatal) return;
      kit.resize();
      const rc2 = mod._sk_restore();
      if (rc2 !== 0) return giveUp("The 3D view could not be rebuilt after losing its graphics context.");
      mod._sk_resize(canvas.width, canvas.height);
      mod._sk_set_quality(kit.quality);
      kit.lost = false;
      if (opts.onRestore) opts.onRestore();
      qStart = 0; settleUntil = performance.now() + settleMs;
      if (kit.running) requestAnimationFrame(loop);
      else if (driven) draw(lastNow / 1000, lastNow);
    });
    function giveUp(msg) {
      kit.fatal = true;
      kit.running = false;
      onFatal("context-lost", msg);
    }
    // For tests: lose and restore the context through WEBGL_lose_context.
    let loseExt = null;
    kit.loseContext = function () {
      // getContext returns the context the engine already made on this canvas.
      const gl = canvas.getContext("webgl2");
      loseExt = gl && gl.getExtension("WEBGL_lose_context");
      if (!loseExt) return false;
      loseExt.loseContext();
      return true;
    };
    kit.restoreContext = function () { if (loseExt) { loseExt.restoreContext(); return true; } return false; };

    /* ------------------------------------------------ for tools */
    window.__strataKit = {
      kit, cam,
      get ready() { return kit.ready && !kit.lost; },
      get lastTapId() { return kit.lastTapId; },
      get quality() { return kit.quality; },
      get lost() { return kit.lost; },
      drive: kit.drive,
      stats: kit.stats,
      project: kit.project,
      pickAt: (x, y) => idOf(kit.pickAt(x, y)),
      tapAt: (x, y) => idOf(kit.tapAt(x, y)),
      targets: () => (opts.targets ? opts.targets() : []),
      loseContext: kit.loseContext,
      restoreContext: kit.restoreContext,
    };
    return kit;
  }

  /* ---------------------------------------------------------- labels */
  // Places HTML labels over the canvas without overlaps, steadily. Each item
  // is {el, q, rank, depth}: q from kit.project (null hides it), lower rank
  // wins, and depth breaks ties (nearer first). A label with rank <= 0 is
  // never hidden: it steps up until it is clear. Rank 1 steps up once;
  // anything else that collides hides. A label only changes between shown
  // and hidden after the change has held for showMs/hideMs, shown labels keep
  // priority over hidden ones of the same rank, and each keeps the step it
  // last used, so a turning camera or a moving thing does not make labels
  // blink. Labels are anchored bottom-centre at q (CSS
  // `transform: translate(-50%, -100%)`); hidden ones get the class
  // sk-label-off (opacity 0 by default, so a transition can fade them).
  function placeLabels(items, o = {}) {
    const now = performance.now();
    const hideMs = o.hideMs != null ? o.hideMs : 350, showMs = o.showMs != null ? o.showMs : 200;
    const gap = o.gap != null ? o.gap : 2;
    injectStyle();
    const taken = [];
    const overlaps = (r) => taken.some((t) => r.x0 < t.x1 && r.x1 > t.x0 && r.y0 < t.y1 && r.y1 > t.y0);
    // Read every size before writing any style: one layout per frame.
    for (const it of items) {
      const el = it.el;
      if (el._skW == null || el._skText !== el.textContent) {
        el.style.display = "";
        el._skW = el.offsetWidth || 80;
        el._skH = el.offsetHeight || 18;
        el._skText = el.textContent;
      }
      if (it.depth == null) it.depth = it.q ? it.q.w : 1e9;
    }
    items.sort((a, b) => a.rank - b.rank || (b.el._skShown ? 1 : 0) - (a.el._skShown ? 1 : 0) || a.depth - b.depth);
    for (const it of items) {
      const el = it.el;
      if (!it.q) {
        el.style.display = "none";
        el._skShown = false; el._skPending = 0;
        continue;
      }
      const w = el._skW, h = el._skH;
      const tries = it.tries != null ? it.tries : (it.rank <= 0 ? 6 : (it.rank === 1 ? 2 : 1));
      const rectAt = (k) => {
        const y = it.q.y - k * (h + gap);
        return {x0: it.q.x - w / 2, x1: it.q.x + w / 2, y0: y - h, y1: y, k};
      };
      let r = null;
      const prevK = el._skK || 0;
      if (prevK < tries && !overlaps(rectAt(prevK))) r = rectAt(prevK);
      for (let k = 0; !r && k < tries; k++) if (!overlaps(rectAt(k))) r = rectAt(k);
      if (!r && it.rank <= 0) r = rectAt(tries - 1);
      const want = !!r;
      if (el._skShown == null) { el._skShown = want; el._skPending = 0; }  // first sight: no delay
      else if (want !== !!el._skShown) {
        if (!el._skPending) el._skPending = now;
        if (now - el._skPending >= (want ? showMs : hideMs) || it.rank <= 0) { el._skShown = want; el._skPending = 0; }
      } else {
        el._skPending = 0;
      }
      // A label still waiting to hide stays where it was drawn last.
      if (el._skShown && !r) r = rectAt(Math.min(prevK, tries - 1));
      if (el._skShown) {
        taken.push(r);
        el._skK = r.k;
        el.style.left = it.q.x + "px";
        el.style.top = r.y1 + "px";
      }
      el.style.display = "";
      el.classList.toggle("sk-label-off", !el._skShown);
    }
  }
  let styled = false;
  function injectStyle() {
    if (styled) return;
    styled = true;
    const s = document.createElement("style");
    s.textContent = ".sk-label-off{opacity:0;visibility:hidden}" +
      "@media (prefers-reduced-motion: no-preference){.sk-label-off{transition:opacity .2s,visibility .2s}}";
    document.head.appendChild(s);
  }

  window.StrataKit = {version: KIT_VERSION, create, placeLabels, store};
})();
