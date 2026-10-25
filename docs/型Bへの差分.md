# 型Bへの差分（静的＋エッジ関数で外部 API を叩く）

★**まず型A（静的＋30分ごとの取り直し）で足りないかを考える。** 型Bは、元のデータが30分より速く変わり、
しかも利用者がそれを待っている時だけにする。型Bは型Aの**上に足す**（取り直した静的データは、
エッジ関数が読めなかった時の受け皿として残す）。

## 型Aとの違い（2026-10-23 時点の公式文書）

| | 型A（静的のみ） | 型B（`/api/*` だけ Worker のコード） |
|---|---|---|
| 無料の上限 | 静的アセットへの要求は無料・無制限 | Worker が動く要求は Free で1日10万回。超えると `/api/*` は 429 になる（公式）。静的アセットがそのあと配られ続けるかは明記なし → ページは `/api/*` が 429 でも型Aのデータで描けるように作る |
| `_headers` | 効く | **Worker のコードが作った応答には効かない** → コードでヘッダを付ける |
| 縁での使い回し（Cache API） | 不要 | 公式文書が「動く」と書いているのは独自ドメインだけ。workers.dev で効くかは書いていない → ★**独自ドメインを付けてから型Bにする** |
| 元のサイトへの負荷 | 30分に1回 | データセンターごとに使い回す（全体では毎秒数回になりうる）。元が弱いなら型Aのまま |
| 秘密 | Cloudflare のトークンだけ | 元の API のキーが要るなら `wrangler secret put` で Worker に入れる（リポジトリにも CI にも置かない） |

## 足すもの

1. `wrangler.jsonc` に `main` と、`assets` に `binding`・`run_worker_first` を足す（`tools/check_repo.py` は
   `main` があるとき `run_worker_first` が `/api/...` だけかを見る）:

```jsonc
  "main": "src/worker.js",
  "assets": {
    "directory": "./dist",
    "not_found_handling": "404-page",
    "html_handling": "auto-trailing-slash",
    "binding": "ASSETS",
    "run_worker_first": ["/api/*"]
  },
```

2. `src/worker.js`（下）。`UPSTREAM` を本物の URL にする（未設定のままだと `check_repo.py --production` が止める）。
3. `tests_js/worker.test.mjs`（下）。Node の標準の試験だけで走る（依存なし）。
4. 試験は CI の `wrangler-dry-run` ジョブの「JS の試験」（`node --test 'tests_js/*.test.mjs'`）がそのまま拾う（手は要らない）。
5. `package.json` に `"type": "module"` を足す（試験から `src/worker.js` を ES モジュールとして読むため）。
6. ページの JS は `/api/items` を読み、`state` が `unreadable` なら `/data/<id>.json`（型Aの取り直し）を
   「前回の内容」と明記して出す。★`textContent` と `createElement` だけで描く（`innerHTML` は Trusted Types で止まる）。
7. CodeQL の default setup の言語に `javascript-typescript` を足す（`repo_settings/codeql.json`）。

## src/worker.js（2026-10-23 に Node 24 で試験8件が通った版）

```js
// 型B: /api/items だけ Worker のコードが応答する。それ以外は静的アセット（_headers が効く）。
// ★取りに行く先は1つに固定する。利用者の入力（クエリ・パス）で URL を作らない（中継の踏み台にしない）。
// ★_headers は Worker のコードが作った応答には効かないので、ここで安全のヘッダを付ける。
// ★三状態: ok / empty / unreadable。unreadable のとき items は null（[] にしない＝「0件」と描かせない）。
export const UPSTREAM = "https://api.example.invalid/v1/items";
const API_PATH = "/api/items";
const OK_SECONDS = 60; // 取れた答えを縁（データセンターごと）で使い回す秒数
const UNREADABLE_SECONDS = 15; // 取れなかった答えも短く使い回す（障害中に元を連打しない）
const TIMEOUT_MS = 8000;
const MAX_BYTES = 1_000_000;

export const SECURITY_HEADERS = {
  "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "Strict-Transport-Security": "max-age=31536000",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
};

function json(body, status, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...SECURITY_HEADERS, ...extra },
  });
}

async function readLimited(res, max) {
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      throw new Error("大きすぎます");
    }
    chunks.push(value);
  }
  const all = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(all);
}

export async function fetchItems(fetchImpl = fetch) {
  try {
    const res = await fetchImpl(UPSTREAM, {
      headers: { "User-Agent": "safe-site-edge/1" },
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (res.status !== 200) throw new Error(`HTTP ${res.status}`);
    const data = JSON.parse(await readLimited(res, MAX_BYTES));
    if (!Array.isArray(data) || !data.every((x) => x !== null && typeof x === "object" && !Array.isArray(x))) {
      throw new Error("形が変です");
    }
    return { state: data.length ? "ok" : "empty", items: data, error: null };
  } catch (e) {
    return { state: "unreadable", items: null, error: String((e && e.message) || e).slice(0, 200) };
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== API_PATH) return json({ error: "not found" }, 404);
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405, { Allow: "GET" });
    const cache = caches.default;
    const key = new Request(url.origin + API_PATH); // クエリを捨てる（クエリを変えた連打で使い回しを破らせない）
    const hit = await cache.match(key);
    if (hit) return hit;
    const result = await fetchItems();
    const seconds = result.state === "unreadable" ? UNREADABLE_SECONDS : OK_SECONDS;
    const res = json({ ...result, checked_at: new Date().toISOString() }, 200, {
      "Cache-Control": `public, max-age=${seconds}`,
    });
    ctx.waitUntil(cache.put(key, res.clone()));
    return res;
  },
};
```

## tests_js/worker.test.mjs

```js
// node --test tests_js/ で走る（Node の標準の試験だけ。依存なし）。本物のネットには出ない。
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import worker, { SECURITY_HEADERS, UPSTREAM, fetchItems } from "../src/worker.js";

class FakeCache {
  constructor() { this.store = new Map(); }
  async match(req) { const r = this.store.get(req.url); return r ? r.clone() : undefined; }
  async put(req, res) { this.store.set(req.url, res.clone()); }
}

let calls;
function upstream(status, body, headers = {}) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(body, { status, headers });
  };
}
function ctx() {
  const waits = [];
  return { waits, waitUntil: (p) => waits.push(p) };
}
async function get(path, method = "GET") {
  const c = ctx();
  const res = await worker.fetch(new Request(`https://site.example.com${path}`, { method }), {}, c);
  await Promise.all(c.waits);
  return res;
}

beforeEach(() => {
  calls = [];
  globalThis.caches = { default: new FakeCache() };
});

test("取れた: ok と行の並び", async () => {
  upstream(200, JSON.stringify([{ a: 1 }, { a: 2 }]));
  const body = await (await get("/api/items")).json();
  assert.equal(body.state, "ok");
  assert.equal(body.items.length, 2);
});

test("元が0件: empty（unreadable と分ける）", async () => {
  upstream(200, "[]");
  const body = await (await get("/api/items")).json();
  assert.deepEqual([body.state, body.items], ["empty", []]);
});

test("取れない: unreadable で items は null（0件にしない）", async () => {
  for (const [status, text] of [[503, "x"], [301, ""], [200, "{not json"], [200, '{"a": 1}'], [200, "[1, 2]"]]) {
    globalThis.caches = { default: new FakeCache() };
    upstream(status, text);
    const body = await (await get("/api/items")).json();
    assert.equal(body.state, "unreadable", `${status} ${text}`);
    assert.equal(body.items, null);
  }
});

test("大きすぎる答えは unreadable", async () => {
  const result = await fetchItems(async () => new Response("[" + " ".repeat(1_000_001) + "]", { status: 200 }));
  assert.equal(result.state, "unreadable");
});

test("取りに行く先は固定（利用者のクエリやパスで変わらない）", async () => {
  upstream(200, "[]");
  await get("/api/items?url=https://evil.example/");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, UPSTREAM);
  assert.equal(calls[0].init.redirect, "manual");
});

test("クエリを変えても縁の使い回しは破れない（元へは1回）", async () => {
  upstream(200, JSON.stringify([{ a: 1 }]));
  await get("/api/items?x=1");
  await get("/api/items?x=2");
  assert.equal(calls.length, 1);
});

test("/api/items 以外は 404、GET 以外は 405", async () => {
  upstream(200, "[]");
  assert.equal((await get("/api/other")).status, 404);
  assert.equal((await get("/api/items", "POST")).status, 405);
  assert.equal(calls.length, 0);
});

test("安全のヘッダを Worker の応答にも付ける（_headers は効かないため）", async () => {
  upstream(200, "[]");
  for (const res of [await get("/api/items"), await get("/api/nothing")]) {
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
      assert.equal(res.headers.get(name), value, name);
    }
  }
});
```

★この試験は Node の上での確かめで、Cloudflare の本物の実行環境（workerd）ではない。初回は
`npx wrangler dev` か、公開後に `/api/items` を開いて `state` と応答ヘッダを目で確かめる。
