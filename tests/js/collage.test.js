"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const C = require(path.join(__dirname, "..", "..", "app", "static", "collage.js"));

// The frame page: 1080x1920 portrait, 16px padding, 6px gap.
const W = 1080, H = 1920, PAD = 16, GAP = 6;

const landscape = (focus) => ({ w: 1080, h: 810, focus: focus || null });   // 4:3
const portrait = (focus) => ({ w: 810, h: 1080, focus: focus || null });    // 3:4
const phone = () => ({ w: 1080, h: 1920, focus: null });                    // 9:16
const video = (w, h) => ({ w, h, video: true, focus: null });

function layout(name) {
  return C.LAYOUTS.find((l) => l.name === name);
}

function seq(values) {
  let i = 0;
  return () => values[i++ % values.length];
}

// Every tile the chooser returns must clear the threshold.
function assertAllTilesFit(plan, pool) {
  const single = plan.tiles.length === 1;
  const rects = C.tileRects(plan.layout, W, H, PAD, GAP);
  plan.tiles.forEach((t, k) => {
    const s = C.planFit(pool[t.index], rects[k].w, rects[k].h, single).score;
    assert.ok(s >= C.MIN_TILE, `${plan.layout.name}/${t.name} scored ${s.toFixed(2)}`);
  });
}

test("tile rects match the CSS grid arithmetic", () => {
  const r = C.tileRects(layout("big-left"), W, H, PAD, GAP);
  assert.equal(r.length, 3);
  assert.equal(r[0].name, "a");
  assert.ok(Math.abs(r[0].w - 694.667) < 0.01);
  assert.equal(r[0].h, 1888);
  assert.ok(Math.abs(r[1].w - 347.333) < 0.01);
  assert.equal(r[1].h, 941);

  const top = C.tileRects(layout("big-top"), W, H, PAD, GAP);
  assert.equal(top[0].w, 1048);
  assert.ok(Math.abs(top[0].h - 1254.667) < 0.01);
});

test("css helpers render grid templates", () => {
  assert.equal(C.cssTracks([2, 1]), "2fr 1fr");
  assert.equal(C.cssAreas([["a", "b"], ["a", "c"]]), "'a b' 'a c'");
});

// The reported frame: a landscape group shot in the tall big-left tile was
// letterboxed to about a quarter of the tile.
test("regression: a landscape group shot is not letterboxed into the tall tile", () => {
  const group = landscape({ a: [0.1, 0.25, 0.8, 0.45], f: null });
  const rects = C.tileRects(layout("big-left"), W, H, PAD, GAP);
  const old = C.planFit(group, rects[0].w, rects[0].h, false);
  assert.equal(old.fit, "contain");
  assert.ok(old.score < C.MIN_TILE);

  const pool = [group, portrait(), portrait(), phone(), landscape()];
  for (let i = 0; i < 50; i++) {
    const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 50]));
    assert.notEqual(plan.layout.name, "big-left");
    assert.ok(plan.tiles.some((t) => t.index === 0), "head item placed");
    assertAllTilesFit(plan, pool);
  }
});

test("head of the pool is always placed", () => {
  const pools = [
    [landscape(), portrait(), portrait(), portrait(), portrait()],
    [portrait(), landscape(), landscape(), landscape(), landscape()],
    [phone(), landscape(), portrait(), phone(), landscape()],
  ];
  for (const pool of pools) {
    for (let i = 0; i < 20; i++) {
      const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 20]));
      assert.ok(plan.tiles.some((t) => t.index === 0));
      assert.equal(new Set(plan.tiles.map((t) => t.index)).size, plan.tiles.length);
    }
  }
});

test("all-landscape pools get landscape-shaped tiles", () => {
  const pool = [landscape(), landscape(), landscape(), landscape(), landscape()];
  const seen = new Set();
  for (let i = 0; i < 40; i++) {
    const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 40]));
    seen.add(plan.layout.name);
    assertAllTilesFit(plan, pool);
  }
  assert.ok(seen.has("stack2") || seen.has("stack3"));
  assert.ok(!seen.has("big-left") && !seen.has("single"));
});

test("portrait pools still get a mix of layouts", () => {
  const pool = [portrait(), phone(), portrait(), phone(), portrait()];
  const seen = new Set();
  for (let i = 0; i < 60; i++) {
    const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 60]));
    seen.add(plan.layout.name);
    assertAllTilesFit(plan, pool);
  }
  assert.ok(seen.size >= 3, [...seen].join(","));
});

test("a panorama with nothing to pair falls back to a single letterbox", () => {
  const pano = { w: 1920, h: 480, focus: null };
  const plan = C.choose([pano], W, H, PAD, GAP, null, () => 0.5);
  assert.equal(plan.layout.name, "single");
  assert.equal(plan.tiles[0].fit, "contain");
});

test("the previous layout is skipped when another fits", () => {
  const pool = [landscape(), landscape(), landscape(), landscape(), landscape()];
  for (let i = 0; i < 20; i++) {
    const first = C.choose(pool, W, H, PAD, GAP, null, seq([i / 20]));
    const next = C.choose(pool, W, H, PAD, GAP, first.layout.name, seq([i / 20]));
    assert.notEqual(next.layout.name, first.layout.name);
  }
});

test("at most one video per collage", () => {
  const pool = [video(1080, 1920), video(1080, 1920), phone(), phone(), phone()];
  for (let i = 0; i < 30; i++) {
    const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 30]));
    const videos = plan.tiles.filter((t) => pool[t.index].video).length;
    assert.ok(videos <= 1);
  }
});

test("landscape video gets a wide tile, not a narrow crop", () => {
  const pool = [video(1920, 1080), landscape(), landscape(), portrait(), portrait()];
  for (let i = 0; i < 20; i++) {
    const plan = C.choose(pool, W, H, PAD, GAP, null, seq([i / 20]));
    assertAllTilesFit(plan, pool);
  }
});

test("faces steer the crop and letterbox only when they cannot fit", () => {
  // Faces near the top of a portrait shown in a wide tile.
  const p = portrait({ a: [0.3, 0.05, 0.4, 0.2], f: [0.3, 0.05, 0.4, 0.2] });
  const fit = C.planFit(p, 1048, 941, false);
  assert.equal(fit.fit, "cover");
  assert.ok(fit.pos.endsWith(" 0.0%"), fit.pos);

  // Group spread too wide for a narrow tile, no frequent face to fall back on.
  const g = landscape({ a: [0.05, 0.3, 0.9, 0.3], f: null });
  const tight = C.planFit(g, 347, 941, false);
  assert.equal(tight.fit, "contain");
  assert.ok(tight.score < C.MIN_TILE);

  // Frequent face fits even though the whole group does not.
  const gf = landscape({ a: [0.05, 0.3, 0.9, 0.3], f: [0.4, 0.3, 0.2, 0.3] });
  const fav = C.planFit(gf, 1048, 941, false);
  assert.equal(fav.fit, "cover");
});

test("landscape displays choose side-by-side layouts for portraits", () => {
  const pool = [portrait(), portrait(), portrait(), portrait(), portrait()];
  const seen = new Set();
  for (let i = 0; i < 30; i++) {
    const plan = C.choose(pool, 1920, 1080, PAD, GAP, null, seq([i / 30]));
    seen.add(plan.layout.name);
    const rects = C.tileRects(plan.layout, 1920, 1080, PAD, GAP);
    plan.tiles.forEach((t, k) => {
      const s = C.planFit(pool[t.index], rects[k].w, rects[k].h, rects.length === 1).score;
      assert.ok(s >= C.MIN_TILE);
    });
  }
  assert.ok(seen.has("side2") || seen.has("side3") || seen.has("grid4"));
});

test("zero-size container yields no plan", () => {
  assert.equal(C.choose([portrait()], 0, 0, PAD, GAP, null, () => 0), null);
});
