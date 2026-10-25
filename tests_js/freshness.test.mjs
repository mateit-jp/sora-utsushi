// node --test 'tests_js/*.test.mjs' で走る（Node の標準の試験だけ。依存なし・ネットに出ない）。
// site/static/freshness.js を、偽の document・fetch・時計の中で動かして、帯の三状態を確かめる。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const CODE = readFileSync(new URL("../site/static/freshness.js", import.meta.url), "utf8");
const NOW = Date.parse("2026-10-23T06:00:00Z");

async function run({ status = 200, body = null, reject = false, withBanner = true } = {}) {
  const banner = { textContent: "", hidden: true };
  const calls = [];
  class FixedDate extends Date {
    static now() {
      return NOW;
    }
  }
  const sandbox = {
    Date: FixedDate,
    Number,
    isFinite,
    Error,
    document: {
      querySelector: (sel) => (withBanner && sel === "[data-stale-banner]" ? banner : null),
      querySelectorAll: () => [],
    },
    fetch: (url, init) => {
      calls.push({ url, init });
      if (reject) return Promise.reject(new Error("つながりません"));
      return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });
    },
  };
  vm.runInNewContext(CODE, sandbox);
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  return { banner, calls };
}

test("新しい: 帯を出さない", async () => {
  const { banner, calls } = await run({ body: { generated_at: "2026-10-23T05:30:00Z", max_status_age_minutes: 90 } });
  assert.equal(banner.hidden, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/status.json");
  assert.equal(calls[0].init.cache, "no-store");
});

test("古い: 「更新が止まっています」を出す", async () => {
  const { banner } = await run({ body: { generated_at: "2026-10-23T03:00:00Z", max_status_age_minutes: 90 } });
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /更新が止まっています/);
  assert.match(banner.textContent, /12:00/); // 03:00Z ＝ 日本時間 12:00
});

test("確かめられない: 黙らずにそう言う（つながらない・HTTP 500・形が変）", async () => {
  for (const args of [{ reject: true }, { status: 500 }, { body: { generated_at: "2026-10-23T05:30:00Z" } }, { body: { generated_at: "x", max_status_age_minutes: 90 } }]) {
    const { banner } = await run(args);
    assert.equal(banner.hidden, false, JSON.stringify(args));
    assert.match(banner.textContent, /確かめられませんでした/);
  }
});

test("帯の場所が無いページでは何もしない（通信もしない）", async () => {
  const { calls } = await run({ withBanner: false });
  assert.equal(calls.length, 0);
});

// ---- 欄ごとの帯（第5版） ----
async function runSections(sectionAttrs) {
  const sections = sectionAttrs.map((attrs) => ({
    attrs,
    textContent: "",
    hidden: true,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
  }));
  const calls = [];
  class FixedDate extends Date {
    static now() {
      return NOW;
    }
  }
  const sandbox = {
    Date: FixedDate,
    Number,
    isFinite,
    Error,
    document: {
      querySelector: () => null, // ページ全体の帯は無い作り（欄ごとの帯だけを見る）
      querySelectorAll: (sel) => (sel === "[data-section-stale]" ? sections : []),
    },
    fetch: (url) => {
      calls.push(url);
      return Promise.reject(new Error("呼ばれないはず"));
    },
  };
  vm.runInNewContext(CODE, sandbox);
  for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r));
  return { sections, calls };
}

test("欄ごと: 新しい欄は出さない・古い欄だけに帯・通信しない", async () => {
  const { sections, calls } = await runSections([
    { "data-fetched-at": "2026-10-23T05:30:00Z", "data-max-age-minutes": "45" },
    { "data-fetched-at": "2026-10-23T04:00:00Z", "data-max-age-minutes": "45" },
  ]);
  assert.equal(sections[0].hidden, true);
  assert.equal(sections[1].hidden, false);
  assert.match(sections[1].textContent, /この欄の更新が止まっています/);
  assert.match(sections[1].textContent, /13:00/); // 04:00Z ＝ 日本時間 13:00
  assert.equal(calls.length, 0);
});

test("欄ごと: 属性が読めなければ「確かめられませんでした」", async () => {
  const { sections } = await runSections([
    { "data-fetched-at": "x", "data-max-age-minutes": "45" },
    { "data-fetched-at": "2026-10-23T05:30:00Z" },
    { "data-fetched-at": "2026-10-23T05:30:00Z", "data-max-age-minutes": "0" },
  ]);
  for (const s of sections) {
    assert.equal(s.hidden, false);
    assert.match(s.textContent, /この欄の更新の状況を確かめられませんでした/);
  }
});
