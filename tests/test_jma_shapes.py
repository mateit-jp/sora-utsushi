"""府県天気予報（fc）・天気概況（ov）の見本を、偽の取得関数で fetch.py に通す（計画書 9.4 の試験1〜8）。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import helpers

import common  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import fetch  # noqa: E402

FIXTURES = helpers.ROOT / "tests" / "fixtures" / "jma"
CODES = ("130000", "270000")
FC_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{}.json"
OV_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{}.json"
FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
# 見本の発表時刻（fc 2026-10-24T11:00+09:00・ov 10:36+09:00）とフィードの updated（13:41:33+09:00）の後
NOW = datetime(2026, 10, 24, 5, 0, 0, tzinfo=timezone.utc)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class JmaShapesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "root"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")
        self.routes = {FEED_URL: fixture("extra.xml")}
        for code in CODES:
            self.routes[FC_URL.format(code)] = fixture(f"fc-{code}.json")
            self.routes[OV_URL.format(code)] = fixture(f"ov-{code}.json")

    def tearDown(self):
        self._tmp.cleanup()

    def run_fetch(self, now: datetime = NOW) -> dict:
        return fetch.run(self.root, fetcher=helpers.FakeWeb(self.routes), previous_base_url=None, now=now)

    def write_previous(self, sid: str, *, items: int, report_time: str) -> None:
        (self.root / "data").mkdir(exist_ok=True)
        payload = {"fetched_at": "2026-10-24T03:00:00Z", "report_time": report_time, "items": [{"n": i} for i in range(items)]}
        (self.root / "data" / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # 1
    def test_fc_and_ov_are_ok_with_2_and_1_items(self):
        records = self.run_fetch()
        for code in CODES:
            self.assertEqual(records[f"fc-{code}"]["state"], common.STATE_OK, records[f"fc-{code}"])
            self.assertEqual(records[f"fc-{code}"]["items"], 2)
            self.assertEqual(records[f"ov-{code}"]["state"], common.STATE_OK, records[f"ov-{code}"])
            self.assertEqual(records[f"ov-{code}"]["items"], 1)
        self.assertEqual(records["jma-feed"]["state"], common.STATE_OK, records["jma-feed"])

    # 2
    def test_report_time_is_the_report_datetime_in_utc(self):
        records = self.run_fetch()
        for code in CODES:
            fc = json.loads(fixture(f"fc-{code}.json"))
            ov = json.loads(fixture(f"ov-{code}.json"))
            self.assertEqual(records[f"fc-{code}"]["report_time"], common.iso_z(common.parse_report_time(fc[0]["reportDatetime"])))
            self.assertEqual(records[f"ov-{code}"]["report_time"], common.iso_z(common.parse_report_time(ov["reportDatetime"])))
            saved = json.loads((self.root / "data" / f"fc-{code}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["report_time"], records[f"fc-{code}"]["report_time"])
            self.assertEqual(saved["source"]["attribution"], "出典：気象庁ホームページ （https://www.jma.go.jp/bosai/forecast/）")

    # 3
    def test_report_older_than_the_previous_is_unreadable(self):
        fc = json.loads(fixture("fc-130000.json"))
        newer = common.parse_report_time(fc[0]["reportDatetime"]) + timedelta(hours=1)
        self.write_previous("fc-130000", items=2, report_time=common.iso_z(newer))
        rec = self.run_fetch()["fc-130000"]
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("発表時刻が前回より古い", rec["error"])
        self.assertTrue(rec["showing_previous"])

    # 4
    def test_fc_with_one_element(self):
        fc = json.loads(fixture("fc-130000.json"))
        self.routes[FC_URL.format("130000")] = json.dumps(fc[:1], ensure_ascii=False).encode("utf-8")
        older = "2026-10-23T02:00:00Z"
        self.write_previous("fc-130000", items=2, report_time=older)
        self.assertEqual(self.run_fetch()["fc-130000"]["state"], common.STATE_OK)
        self.write_previous("fc-130000", items=3, report_time=older)
        rec = self.run_fetch()["fc-130000"]
        self.assertEqual(rec["state"], common.STATE_UNREADABLE)
        self.assertIn("急減", rec["error"])

    # 5
    def test_404_is_unreadable_and_keeps_the_previous(self):
        del self.routes[FC_URL.format("130000")]
        del self.routes[OV_URL.format("130000")]
        self.write_previous("fc-130000", items=2, report_time="2026-10-23T02:00:00Z")
        records = self.run_fetch()
        self.assertEqual(records["fc-130000"]["state"], common.STATE_UNREADABLE)
        self.assertTrue(records["fc-130000"]["showing_previous"])
        self.assertEqual(len(json.loads((self.root / "data" / "fc-130000.json").read_text(encoding="utf-8"))["items"]), 2)
        self.assertEqual(records["ov-130000"]["state"], common.STATE_UNREADABLE)
        self.assertFalse((self.root / "data" / "ov-130000.json").exists())

    # 6
    def test_fixtures_are_within_a_third_of_max_bytes(self):
        sources = {s["id"]: s for s in common.load_sources(self.root, production=False)}
        for code in CODES:
            self.assertLessEqual(len(fixture(f"fc-{code}.json")) * 3, sources[f"fc-{code}"]["max_bytes"])
            self.assertLessEqual(len(fixture(f"ov-{code}.json")) * 3, sources[f"ov-{code}"]["max_bytes"])

    # 7
    def test_report_older_than_24_hours_is_unreadable(self):
        records = self.run_fetch(now=NOW + timedelta(hours=25))
        for code in CODES:
            for sid in (f"fc-{code}", f"ov-{code}"):
                self.assertEqual(records[sid]["state"], common.STATE_UNREADABLE, sid)
                self.assertIn("元が止まっている疑い", records[sid]["error"])

    # 8
    def test_report_time_without_timezone_is_unreadable(self):
        for code in CODES:
            for url, name in ((FC_URL.format(code), f"fc-{code}.json"), (OV_URL.format(code), f"ov-{code}.json")):
                body = fixture(name)
                self.assertIn(b"+09:00", body)
                self.routes[url] = body.replace(b"+09:00", b"")
        records = self.run_fetch()
        for code in CODES:
            for sid in (f"fc-{code}", f"ov-{code}"):
                self.assertEqual(records[sid]["state"], common.STATE_UNREADABLE, sid)
                self.assertIn("発表時刻を読めませんでした", records[sid]["error"])


if __name__ == "__main__":
    unittest.main()
