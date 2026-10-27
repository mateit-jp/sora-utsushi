"""警報・注意報の電文の二段の取り込み（tools/fetch_jma_warning.py）を、見本と作り物のフィードで試す（計画書 9.4 の試験9〜26）。"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import common  # noqa: E402
import fetch  # noqa: E402
import fetch_jma_warning as fjw  # noqa: E402

FIXTURES = helpers.ROOT / "tests" / "fixtures" / "jma"
FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
ATOM = "http://www.w3.org/2005/Atom"
NOW = datetime(2026, 10, 24, 5, 0, 0, tzinfo=timezone.utc)
FEED_UPDATED = "2026-10-24T13:41:33+09:00"  # 見本の extra.xml の updated（時差つき）
# 見本の電文と、その URL（SOURCES.txt）
TELEGRAMS = {
    "20261023191528_0_VPWW61_130000.xml": "vpww-VPWW61-01.xml",
    "20261024010526_0_VPWW61_270000.xml": "vpww-VPWW61-02.xml",
    "20261023191528_0_VPWW59_130000.xml": "vpww-VPWW59-01.xml",
    "20261024010405_0_VPWW56_270000.xml": "vpww-VPWW56-01.xml",
    "20261022230149_0_VPWW58_130000.xml": "vpww-VPWW58-01.xml",
    "20261024010405_0_VPWW55_270000.xml": "vpww-VPWW55-01.xml",
    "20261021114643_0_VPWW59_270000.xml": "vpww-VPWW59-02.xml",
    "20261021114643_0_VPWW58_270000.xml": "vpww-VPWW58-02.xml",
    "20261023145648_0_VPWW59_130000.xml": "vpww-VPWW59-03.xml",
    "20261023200009_0_VPWW56_270000.xml": "vpww-VPWW56-02.xml",
}
SAMPLE = (FIXTURES / "vpww-VPWW59-01.xml").read_bytes()


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def link(stamp: str, kind: str, office: str) -> str:
    return f"{fjw.LINK_PREFIX}{stamp}_0_{kind}_{office}.xml"


def make_feed(entries: list[tuple[str, str]], updated: str = FEED_UPDATED) -> bytes:
    """作り物のフィード。entries は (リンク, entry の updated)。"""
    parts = [f'<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="{ATOM}" lang="ja"><title>作り物</title><updated>{updated}</updated>']
    for href, when in entries:
        parts.append(
            f"<entry><title>気象警報・注意報</title><id>{href}</id><updated>{when}</updated>"
            f'<author><name>気象台</name></author><link type="application/xml" href="{href}"/><content type="text">x</content></entry>'
        )
    parts.append("</feed>")
    return "".join(parts).encode("utf-8")


class Web(helpers.FakeWeb):
    """偽の取得関数（同時に何本取りに行っているかも数える）。"""

    def __init__(self, routes=None, delay: float = 0.0):
        super().__init__(routes)
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, url, **kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return super().__call__(url, **kwargs)
        finally:
            with self.lock:
                self.active -= 1

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def telegram_urls(self) -> list[str]:
        return [u for u in self.urls() if u.startswith(fjw.LINK_PREFIX)]


class FetchJmaWarningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "root"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")

    def tearDown(self):
        self._tmp.cleanup()

    def run_warn(self, web: Web, *, base: str | None = None, now: datetime = NOW, clock=time.monotonic) -> dict:
        return fjw.run(self.root, fetcher=web, previous_base_url=base, now=now, clock=clock)

    def saved(self) -> dict:
        return json.loads((self.root / "data" / "warn.json").read_text(encoding="utf-8"))

    def real_web(self) -> Web:
        routes = {FEED_URL: fixture("extra.xml"), fjw.BACKFILL_FEED_URL: fixture("extra_l.xml")}
        for name, fname in TELEGRAMS.items():
            routes[fjw.LINK_PREFIX + name] = fixture(fname)
        return Web(routes)

    def two_runs(self, first: Web, second: Web) -> dict:
        self.run_warn(first)
        return self.run_warn(second, now=NOW.replace(minute=30))

    # 9
    def test_stale_watcher_makes_warn_unreadable_after_build(self):
        feed = fixture("extra.xml")
        self.assertEqual(feed.count(FEED_UPDATED.encode()), 1)
        stale = feed.replace(FEED_UPDATED.encode(), b"2026-10-24T11:41:33+09:00")  # 2時間前
        web = self.real_web()
        web.routes[FEED_URL] = stale
        fetch.run(self.root, fetcher=web, previous_base_url=None, now=NOW)
        rec = self.run_warn(web)
        self.assertEqual(rec["state"], common.STATE_OK)  # 取り込みの直後の state.json では ok のまま（heartbeat は build が当てる）
        out = Path(self._tmp.name) / "dist"
        build.build(self.root, out, production=False, now=NOW)
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources"]["jma-feed"]["state"], common.STATE_UNREADABLE)
        self.assertEqual(status["sources"]["warn"]["state"], common.STATE_UNREADABLE)
        self.assertIn("見張り役（jma-feed）", status["sources"]["warn"]["error"])

    # 10
    def test_real_telegrams_are_kept_character_for_character(self):
        rec = self.run_warn(self.real_web())
        self.assertEqual(rec["state"], common.STATE_OK, rec)
        payload = self.saved()
        self.assertEqual(payload["report_time"], "2026-10-24T04:41:33Z")
        self.assertEqual(len(payload["items"]), 8)  # 2官署 × 見本の種類（東京都 58・59・61、大阪府 55・56・58・59・61）

        def local(elem: ET.Element) -> str:
            return elem.tag.rsplit("}", 1)[-1]

        def child(node: ET.Element, name: str) -> ET.Element:
            return next(c for c in node if local(c) == name)

        for item in payload["items"]:
            name = item["url"][len(fjw.LINK_PREFIX):]
            raw = ET.fromstring(fixture(TELEGRAMS[name]))  # 取り込みの道具とは別の読み方で、電文の文字を取り出して比べる
            control, head, body = child(raw, "Control"), child(raw, "Head"), child(raw, "Body")
            self.assertEqual(item["control_title"], child(control, "Title").text)
            self.assertEqual(item["publishing_office"], child(control, "PublishingOffice").text)
            self.assertEqual(item["head_title"], child(head, "Title").text)
            self.assertEqual(item["report_datetime"], child(head, "ReportDateTime").text)
            self.assertEqual(item["headline"], child(child(head, "Headline"), "Text").text or "")
            expected_areas = [child(a, "Name").text for a in body.iter() if local(a) == "Area"]
            expected_kinds = [next((c.text for c in k if local(c) == "Name"), None) for k in body.iter() if local(k) == "Kind"]
            self.assertEqual([it["area"] for block in item["warnings"] for it in block["items"]], expected_areas)
            self.assertEqual([k["name"] for block in item["warnings"] for it in block["items"] for k in it["kinds"]], expected_kinds)
            self.assertEqual(item["office"], name.split("_")[3][:6])
            self.assertEqual(item["kind"], name.split("_")[2])

    # 11
    def test_links_outside_the_prefix_are_not_followed(self):
        good = link("20261024000000", "VPWW59", "130000")
        # 外のリンクは、良いリンクと別の官署×種類にする（同じ組だと「最新の1本」の選び方で外れて、前置きの検査を試せない）
        bad1 = "https://example.invalid/developer/xml/data/20261024000000_0_VPWW56_130000.xml"
        bad2 = link("20261024000000", "VPWW61", "270000").replace("https://", "http://")
        web = Web({FEED_URL: make_feed([(good, "2026-10-24T00:00:00Z"), (bad1, "2026-10-24T00:00:00Z"), (bad2, "2026-10-24T00:00:00Z")]),
                   fjw.BACKFILL_FEED_URL: make_feed([]), good: SAMPLE})
        self.assertEqual(self.run_warn(web)["state"], common.STATE_OK)
        self.assertEqual(web.telegram_urls(), [good])
        self.assertNotIn(bad1, web.urls())
        self.assertNotIn(bad2, web.urls())

    # 12
    def test_one_broken_telegram_makes_the_whole_warn_unreadable(self):
        a = link("20261024000000", "VPWW59", "130000")
        b = link("20261024000000", "VPWW56", "270000")
        first = Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE})
        second = Web({FEED_URL: make_feed([(a, "2026-10-24T04:40:00Z"), (b, "2026-10-24T04:40:00Z")]),
                      fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE, b: fetch.FetchError("HTTP 500")})
        rec = self.two_runs(first, second)
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("HTTP 500", rec["error"])
        self.assertTrue(rec["showing_previous"])
        self.assertEqual([i["url"] for i in self.saved()["items"]], [a])  # 前回分を保つ

    # 13
    def test_only_max_links_are_followed_in_one_run(self):
        offices = [f"{i:02d}0000" for i in range(10, 10 + fjw.MAX_LINKS_PER_RUN + 1)]
        routes = {FEED_URL: make_feed([(link("20261024000000", "VPWW59", o), "2026-10-24T00:00:00Z") for o in offices]),
                  fjw.BACKFILL_FEED_URL: make_feed([])}
        routes.update({link("20261024000000", "VPWW59", o): SAMPLE for o in offices})
        web = Web(routes)
        with mock.patch.object(fjw, "LIMIT_TO_PREPARING_OFFICES", False):
            rec = self.run_warn(web)
        self.assertEqual(len(web.telegram_urls()), fjw.MAX_LINKS_PER_RUN)
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)  # 辿れなかった分を「取れた」にしない
        self.assertIn("上限", rec["error"])

    # 14
    def test_telegrams_in_the_previous_data_are_not_fetched_again(self):
        a = link("20261024000000", "VPWW59", "130000")
        feed = make_feed([(a, "2026-10-24T00:00:00Z")])
        first = Web({FEED_URL: feed, fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE})
        second = Web({FEED_URL: feed, fjw.BACKFILL_FEED_URL: make_feed([])})
        rec = self.two_runs(first, second)
        self.assertEqual(rec["state"], common.STATE_OK, rec)
        self.assertEqual(second.telegram_urls(), [])
        self.assertEqual([i["url"] for i in self.saved()["items"]], [a])

    # 15
    def test_telegram_with_a_doctype_is_unreadable(self):
        a = link("20261024000000", "VPWW59", "130000")
        evil = SAMPLE.replace(b"<Report", b'<!DOCTYPE r [<!ENTITY x "x">]><Report', 1)
        web = Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: make_feed([]), a: evil})
        rec = self.run_warn(web)
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("DOCTYPE", rec["error"])
        self.assertFalse((self.root / "data" / "warn.json").exists())  # 前回分が無ければ作らない

    # 16
    def test_telegram_limit_is_three_times_the_largest_fixture(self):
        largest = max(len(fixture(name)) for name in TELEGRAMS.values())
        self.assertEqual(largest, 26_802)
        self.assertLessEqual(largest * 3, fjw.MAX_BYTES_PER_LINK)

    # 18
    def test_only_the_preparing_offices_are_followed(self):
        entries = [(link("20261024000000", "VPWW59", o), "2026-10-24T00:00:00Z") for o in ("130000", "270000", "400000")]
        no_code = f"{fjw.LINK_PREFIX}20261024000000_0_VPWW59_.xml"
        entries.append((no_code, "2026-10-24T00:00:00Z"))
        routes = {FEED_URL: make_feed(entries), fjw.BACKFILL_FEED_URL: make_feed([]), no_code: SAMPLE}
        routes.update({href: SAMPLE for href, _ in entries})
        web = Web(routes)
        self.run_warn(web)
        self.assertEqual(sorted(web.telegram_urls()), sorted(link("20261024000000", "VPWW59", o) for o in fjw.PREPARING_OFFICES))
        (self.root / "data" / "warn.json").unlink()
        web = Web(routes)
        with mock.patch.object(fjw, "LIMIT_TO_PREPARING_OFFICES", False):
            self.run_warn(web)
        self.assertEqual(sorted(web.telegram_urls()), sorted(href for href, _ in entries[:3]))

    # 19
    def test_backfill_is_skipped_when_the_feed_reaches_back_to_the_previous_run(self):
        a = link("20261024000000", "VPWW59", "130000")
        first = Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE})
        # 前回の fetched_at は 05:00Z。今回のフィードの最古の entry（04:50Z）はそれより古い
        second = Web({FEED_URL: make_feed([(a, "2026-10-24T04:50:00Z")])})
        rec = self.two_runs(first, second)
        self.assertEqual(rec["state"], common.STATE_OK, rec)
        self.assertNotIn(fjw.BACKFILL_FEED_URL, second.urls())

    # 20
    def test_backfill_is_read_when_something_may_have_been_missed(self):
        a = link("20261024000000", "VPWW59", "130000")
        b = link("20261023000000", "VPWW56", "270000")
        backfill = make_feed([(a, "2026-10-24T00:00:00Z"), (b, "2026-10-23T00:00:00Z")])
        # 前回分が無い → 長期フィードを読み、両方に載った同じ電文は1回だけ取りに行く
        web = Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: backfill, a: SAMPLE, b: SAMPLE})
        self.assertEqual(self.run_warn(web)["state"], common.STATE_OK)
        self.assertIn(fjw.BACKFILL_FEED_URL, web.urls())
        self.assertEqual(sorted(web.telegram_urls()), sorted([a, b]))
        # 前回分あり・今回の最古の entry（05:20Z）が前回の fetched_at（05:00Z）より新しい → 読む
        web = Web({FEED_URL: make_feed([(a, "2026-10-24T05:20:00Z")]), fjw.BACKFILL_FEED_URL: backfill})
        self.run_warn(web, now=NOW.replace(minute=30))
        self.assertIn(fjw.BACKFILL_FEED_URL, web.urls())
        # entry の時刻が読めない → 読む
        web = Web({FEED_URL: make_feed([(a, "いつか")]), fjw.BACKFILL_FEED_URL: backfill})
        self.run_warn(web, now=NOW.replace(minute=45))
        self.assertIn(fjw.BACKFILL_FEED_URL, web.urls())

    # 21
    def test_unreadable_backfill_makes_the_whole_warn_unreadable(self):
        a = link("20261024000000", "VPWW59", "130000")
        web = Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: fetch.FetchError("HTTP 503"), a: SAMPLE})
        rec = self.run_warn(web)
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("HTTP 503", rec["error"])
        self.assertEqual(web.telegram_urls(), [])

    # 22
    def test_feed_limits_are_three_times_the_fixtures(self):
        self.assertLessEqual(len(fixture("extra.xml")) * 3, fjw.FEED_MAX_BYTES)
        self.assertLessEqual(len(fixture("extra_l.xml")) * 3, fjw.BACKFILL_FEED_MAX_BYTES)
        self.assertEqual(fjw.FEED_MAX_BYTES, 3 * 312_433)  # 受入記録 4.4: extra.xml の3回の最大
        self.assertEqual(fjw.BACKFILL_FEED_MAX_BYTES, 3 * len(fixture("extra_l.xml")))

    # 23
    def test_previous_is_read_from_the_live_site_only_when_asked(self):
        a = link("20261024000000", "VPWW59", "130000")
        feed = make_feed([(a, "2026-10-24T04:50:00Z")])
        self.run_warn(Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE}))
        written = (self.root / "data" / "warn.json").read_bytes()
        # 書いた data/warn.json を、次の回の fetch.load_previous_one が前回分として読める
        src = next(s for s in common.load_sources(self.root, production=False) if s["id"] == "warn")
        prev, _ = fetch.load_previous_one(src, None, self.root / "data", Web(), "ua")
        self.assertIsNotNone(prev)
        self.assertTrue(fetch.valid_payload(prev))
        common.parse_iso_z(prev["fetched_at"])
        common.parse_iso_z(prev["report_time"])
        self.assertEqual(prev["report_time"], "2026-10-24T04:41:33Z")  # +09:00 の updated を UTC の Z の形で
        # あり: 公開中のサイトの /data/warn.json を1回読み、その中身を前回分に使う
        (self.root / "data" / "warn.json").unlink()
        live = Web({FEED_URL: feed, f"{helpers.LIVE}data/warn.json": written})
        rec = self.run_warn(live, base=helpers.LIVE, now=NOW.replace(minute=30))
        self.assertEqual(rec["state"], common.STATE_OK, rec)
        self.assertEqual(live.urls().count(f"{helpers.LIVE}data/warn.json"), 1)
        self.assertEqual(live.telegram_urls(), [])
        # なし: 公開中のサイトに要求せず、作業場の data/warn.json を読む
        local = Web({FEED_URL: feed})
        self.assertEqual(self.run_warn(local, now=NOW.replace(minute=45))["state"], common.STATE_OK)
        self.assertFalse(any(u.startswith(helpers.LIVE) for u in local.urls()))
        self.assertEqual(local.telegram_urls(), [])
        # 公開中の前回分が 404 → 作業場の data/warn.json → それも無ければ前回分なし（長期フィードを読む）
        missing = Web({FEED_URL: feed})
        self.assertEqual(self.run_warn(missing, base=helpers.LIVE, now=NOW.replace(minute=50))["state"], common.STATE_OK)
        self.assertNotIn(fjw.BACKFILL_FEED_URL, missing.urls())
        (self.root / "data" / "warn.json").unlink()
        none = Web({FEED_URL: feed, fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE})
        self.run_warn(none, base=helpers.LIVE, now=NOW.replace(minute=55))
        self.assertIn(fjw.BACKFILL_FEED_URL, none.urls())

    # 24
    def test_deadline_stops_further_fetches_and_keeps_the_previous(self):
        a = link("20261024000000", "VPWW59", "130000")
        b = link("20261024000000", "VPWW56", "270000")
        self.run_warn(Web({FEED_URL: make_feed([(a, "2026-10-24T00:00:00Z")]), fjw.BACKFILL_FEED_URL: make_feed([]), a: SAMPLE}))
        ticks = iter([0.0] + [fjw.DEADLINE_SECONDS + 1.0] * 100)
        web = Web({FEED_URL: make_feed([(a, "2026-10-24T04:50:00Z"), (b, "2026-10-24T04:50:00Z")]), b: SAMPLE})
        rec = self.run_warn(web, now=NOW.replace(minute=30), clock=lambda: next(ticks))
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("締切", rec["error"])
        self.assertEqual(web.telegram_urls(), [])
        self.assertEqual([i["url"] for i in self.saved()["items"]], [a])

    # 25
    def test_no_more_than_max_workers_at_once(self):
        offices = [f"{i:02d}0000" for i in range(10, 22)]
        routes = {FEED_URL: make_feed([(link("20261024000000", "VPWW59", o), "2026-10-24T00:00:00Z") for o in offices]),
                  fjw.BACKFILL_FEED_URL: make_feed([])}
        routes.update({link("20261024000000", "VPWW59", o): SAMPLE for o in offices})
        web = Web(routes, delay=0.05)
        with mock.patch.object(fjw, "LIMIT_TO_PREPARING_OFFICES", False):
            self.assertEqual(self.run_warn(web)["state"], common.STATE_OK)
        self.assertEqual(len(web.telegram_urls()), len(offices))
        self.assertLessEqual(web.max_active, fjw.MAX_WORKERS)

    # 26
    def test_each_office_keeps_one_telegram_per_kind(self):
        old56 = link("20261023000000", "VPWW56", "130000")
        old59 = link("20261023000000", "VPWW59", "130000")
        new59 = link("20261024040000", "VPWW59", "130000")
        first = Web({FEED_URL: make_feed([(old56, "2026-10-24T00:00:00Z"), (old59, "2026-10-24T00:00:00Z")]),
                     fjw.BACKFILL_FEED_URL: make_feed([]), old56: SAMPLE, old59: SAMPLE})
        self.run_warn(first)
        self.assertEqual(sorted(i["url"] for i in self.saved()["items"]), sorted([old56, old59]))
        # 新しい VPWW59 だけが来た（VPWW56 はフィードから消えた）→ VPWW59 だけ置き換わり、VPWW56 は持ち越す
        second = Web({FEED_URL: make_feed([(new59, "2026-10-24T04:50:00Z")]), new59: SAMPLE})
        rec = self.run_warn(second, now=NOW.replace(minute=30))
        self.assertEqual(rec["state"], common.STATE_OK, rec)
        self.assertEqual(sorted(i["url"] for i in self.saved()["items"]), sorted([old56, new59]))
        self.assertEqual(second.telegram_urls(), [new59])

    def test_state_json_gets_the_warn_record(self):
        self.run_warn(self.real_web())
        state = json.loads((self.root / "data" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sources"]["warn"]["state"], common.STATE_OK)
        self.assertEqual(state["sources"]["warn"]["items"], 8)


if __name__ == "__main__":
    unittest.main()
