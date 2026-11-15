// Collage layout selection and per-tile fit.
//
// ES5 only: the frames run an old Android WebView. No DOM access here, so the
// same file runs under node for the tests.
(function (root) {
  "use strict";

  // Track sizes are fr weights; areas name the tile in each grid cell. Every
  // layout is scored against the real screen size, so ones that do not suit
  // the display's shape are simply never picked.
  var LAYOUTS = [
    { name: "single", rows: [1], cols: [1], areas: [["a"]], weight: 2 },
    { name: "stack2", rows: [1, 1], cols: [1], areas: [["a"], ["b"]], weight: 2 },
    { name: "stack3", rows: [1, 1, 1], cols: [1], areas: [["a"], ["b"], ["c"]], weight: 1 },
    { name: "strip-top", rows: [1, 2], cols: [1], areas: [["a"], ["b"]], weight: 1 },
    { name: "strip-bottom", rows: [2, 1], cols: [1], areas: [["a"], ["b"]], weight: 1 },
    { name: "wide-top", rows: [1, 1], cols: [1, 1], areas: [["a", "a"], ["b", "c"]], weight: 1 },
    { name: "wide-bottom", rows: [1, 1], cols: [1, 1], areas: [["a", "b"], ["c", "c"]], weight: 1 },
    { name: "grid4", rows: [1, 1], cols: [1, 1], areas: [["a", "b"], ["c", "d"]], weight: 1 },
    {
      name: "big-top", rows: [2, 1], cols: [1, 1, 1],
      areas: [["a", "a", "a"], ["b", "c", "d"]], weight: 1
    },
    { name: "big-left", rows: [1, 1], cols: [2, 1], areas: [["a", "b"], ["a", "c"]], weight: 1 },
    { name: "side2", rows: [1], cols: [1, 1], areas: [["a", "b"]], weight: 1 },
    { name: "side3", rows: [1], cols: [1, 1, 1], areas: [["a", "b", "c"]], weight: 1 }
  ];

  // Lowest acceptable tile score. Score is the share of the photo kept by a
  // crop, or the share of the tile filled when letterboxed.
  var MIN_TILE = 0.55;
  // Letterboxing inside a collage leaves black bands, so it costs extra.
  var CONTAIN_PENALTY = 0.25;
  // A lone photo letterboxes over the blurred backdrop below this ratio.
  var SINGLE_CONTAIN_BELOW = 0.74;
  // Two clips decoding at once stutter on the frame's SoC.
  var MAX_VIDEOS = 1;

  function sum(list) {
    var s = 0;
    for (var i = 0; i < list.length; i++) s += list[i];
    return s;
  }

  function tracks(fr, total, gap) {
    var free = total - gap * (fr.length - 1);
    var unit = free / sum(fr);
    var out = [];
    for (var i = 0; i < fr.length; i++) out.push(fr[i] * unit);
    return out;
  }

  function span(sizes, first, last, gap) {
    var s = 0;
    for (var i = first; i <= last; i++) s += sizes[i];
    return s + gap * (last - first);
  }

  // Tile sizes in px, in order of first appearance in the areas grid.
  // Computed rather than measured: tiles are not in the document yet.
  function tileRects(layout, width, height, pad, gap) {
    var colSizes = tracks(layout.cols, width - 2 * pad, gap);
    var rowSizes = tracks(layout.rows, height - 2 * pad, gap);
    var order = [];
    var ext = {};
    for (var r = 0; r < layout.areas.length; r++) {
      for (var c = 0; c < layout.areas[r].length; c++) {
        var n = layout.areas[r][c];
        if (!ext[n]) {
          ext[n] = { r0: r, r1: r, c0: c, c1: c };
          order.push(n);
        }
        var e = ext[n];
        if (r > e.r1) e.r1 = r;
        if (c > e.c1) e.c1 = c;
        if (c < e.c0) e.c0 = c;
      }
    }
    var out = [];
    for (var i = 0; i < order.length; i++) {
      var x = ext[order[i]];
      out.push({
        name: order[i],
        w: span(colSizes, x.c0, x.c1, gap),
        h: span(rowSizes, x.r0, x.r1, gap)
      });
    }
    return out;
  }

  function cssTracks(fr) {
    var parts = [];
    for (var i = 0; i < fr.length; i++) parts.push(fr[i] + "fr");
    return parts.join(" ");
  }

  function cssAreas(areas) {
    var rows = [];
    for (var r = 0; r < areas.length; r++) rows.push("'" + areas[r].join(" ") + "'");
    return rows.join(" ");
  }

  function boxFits(box, vw, vh) {
    return !!box && box[2] <= vw + 0.001 && box[3] <= vh + 0.001;
  }

  // object-position that centres box inside the visible window of a cover
  // crop. vw/vh are the visible fraction of the image on each axis.
  function coverPosition(box, vw, vh) {
    var px = 50, py = 50;
    if (vw < 0.999) {
      var left = Math.min(Math.max(box[0] + box[2] / 2 - vw / 2, 0), 1 - vw);
      px = left / (1 - vw) * 100;
    }
    if (vh < 0.999) {
      var top = Math.min(Math.max(box[1] + box[3] / 2 - vh / 2, 0), 1 - vh);
      py = top / (1 - vh) * 100;
    }
    return px.toFixed(1) + "% " + py.toFixed(1) + "%";
  }

  // How one item should sit in a tile of tw x th px.
  // item: {w, h, focus} where focus is {a, f} face boxes or null.
  // Returns {fit, pos, score}.
  function planFit(item, tw, th, single) {
    var center = { fit: "cover", pos: "50% 50%", score: 0 };
    if (!item || !item.w || !item.h || !tw || !th) return center;
    var iAR = item.w / item.h;
    var tAR = tw / th;
    var r = iAR > tAR ? tAR / iAR : iAR / tAR;

    if (single && r < SINGLE_CONTAIN_BELOW) {
      return { fit: "contain", pos: "50% 50%", score: r };
    }

    var focus = item.focus;
    if (!focus || !focus.a) {
      return { fit: "cover", pos: "50% 50%", score: r };
    }
    var scale = Math.max(tw / item.w, th / item.h);
    var vw = tw / (item.w * scale);
    var vh = th / (item.h * scale);
    // Prefer every clear face, settle for the frequent ones, otherwise
    // letterbox rather than cut a face.
    if (boxFits(focus.a, vw, vh)) {
      return { fit: "cover", pos: coverPosition(focus.a, vw, vh), score: r };
    }
    if (boxFits(focus.f, vw, vh)) {
      return { fit: "cover", pos: coverPosition(focus.f, vw, vh), score: r * 0.9 };
    }
    return { fit: "contain", pos: "50% 50%", score: r - CONTAIN_PENALTY };
  }

  // Best placement of pool items into a layout's tiles. pool[0] must be
  // placed so the playlist keeps moving. Returns null if nothing qualifies.
  function bestAssignment(pool, rects, single) {
    var n = rects.length;
    if (n > pool.length) return null;
    var scores = [];
    for (var t = 0; t < n; t++) {
      var row = [];
      for (var p = 0; p < pool.length; p++) {
        row.push(planFit(pool[p], rects[t].w, rects[t].h, single).score);
      }
      scores.push(row);
    }

    var best = null;
    var used = [];
    var pick = [];
    for (var u = 0; u < pool.length; u++) used.push(false);

    function walk(t, total, videos) {
      if (t === n) {
        if (!used[0]) return;
        // Small tie-break toward older items keeps playback near list order.
        var order = 0;
        for (var k = 0; k < n; k++) order += pick[k];
        var value = total - order * 0.001;
        if (!best || value > best.value) {
          best = { value: value, picks: pick.slice(0), mean: total / n };
        }
        return;
      }
      for (var p = 0; p < pool.length; p++) {
        if (used[p]) continue;
        var s = scores[t][p];
        if (s < MIN_TILE) continue;
        var v = videos + (pool[p].video ? 1 : 0);
        if (v > MAX_VIDEOS && n > 1) continue;
        used[p] = true;
        pick[t] = p;
        walk(t + 1, total + s, v);
        used[p] = false;
      }
    }
    walk(0, 0, 0);
    return best;
  }

  // Choose a layout and placement for the head of the pool.
  // pool: ready items in playlist order. Returns
  // {layout, tiles: [{name, index, fit, pos}]} with index into pool.
  function choose(pool, width, height, pad, gap, lastName, rand) {
    rand = rand || Math.random;
    var candidates = [];
    var fallback = null;
    for (var i = 0; i < LAYOUTS.length; i++) {
      var layout = LAYOUTS[i];
      var rects = tileRects(layout, width, height, pad, gap);
      if (rects[0].w <= 0 || rects[0].h <= 0) continue;
      var single = rects.length === 1;
      if (single) {
        fallback = { layout: layout, rects: rects, picks: [0], mean: 0 };
      }
      var best = bestAssignment(pool, rects, single);
      if (!best) continue;
      candidates.push({ layout: layout, rects: rects, picks: best.picks, mean: best.mean });
    }

    var chosen = null;
    if (candidates.length) {
      var pickFrom = [];
      for (var c = 0; c < candidates.length; c++) {
        if (candidates[c].layout.name !== lastName) pickFrom.push(candidates[c]);
      }
      if (!pickFrom.length) pickFrom = candidates;
      var weights = [];
      var total = 0;
      for (var w = 0; w < pickFrom.length; w++) {
        var q = pickFrom[w].mean;
        var wt = pickFrom[w].layout.weight * q * q;
        weights.push(wt);
        total += wt;
      }
      var roll = rand() * total;
      chosen = pickFrom[pickFrom.length - 1];
      for (var x = 0; x < pickFrom.length; x++) {
        roll -= weights[x];
        if (roll < 0) { chosen = pickFrom[x]; break; }
      }
    } else {
      // Nothing fits the head item well (a panorama, say): show it alone,
      // letterboxed over the blurred backdrop.
      chosen = fallback;
    }
    if (!chosen) return null;

    var tiles = [];
    var isSingle = chosen.rects.length === 1;
    for (var k = 0; k < chosen.rects.length; k++) {
      var rect = chosen.rects[k];
      var item = pool[chosen.picks[k]];
      var plan = planFit(item, rect.w, rect.h, isSingle);
      tiles.push({ name: rect.name, index: chosen.picks[k], fit: plan.fit, pos: plan.pos });
    }
    return { layout: chosen.layout, tiles: tiles };
  }

  var api = {
    LAYOUTS: LAYOUTS,
    MIN_TILE: MIN_TILE,
    tileRects: tileRects,
    cssTracks: cssTracks,
    cssAreas: cssAreas,
    coverPosition: coverPosition,
    planFit: planFit,
    bestAssignment: bestAssignment,
    choose: choose
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.SmartFrameCollage = api;
  }
})(this);
