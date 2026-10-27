"""第1号の取得先と区域の一覧（site/sources.json・site/areas.json・site/config.json）を、見本の原文と突き合わせる（計画書 9.3）。"""
from __future__ import annotations

import json
import math
import unittest

import helpers

import common  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import site_hooks  # noqa: E402

FIXTURES = helpers.ROOT / "tests" / "fixtures" / "jma"
PREFIXES = ["https://www.jma.go.jp/bosai/", "https://www.data.jma.go.jp/developer/xml/feed/"]
PREPARING_CODES = ["130000", "270000"]  # 準備中公開の2区域（第12章 #28）


def load(rel: str) -> object:
    return json.loads((helpers.ROOT / rel).read_text(encoding="utf-8"))


class SourcesJmaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {s["id"]: s for s in load("site/sources.json")["sources"]}
        cls.sources = common.load_sources(helpers.ROOT, production=True)
        cls.cfg = load("site/config.json")
        cls.areas = load("site/areas.json")["areas"]
        cls.probe = json.loads((FIXTURES / "probe.json").read_text(encoding="utf-8"))["results"]
        cls.offices = json.loads((FIXTURES / "area.json").read_text(encoding="utf-8"))["offices"]

    # 1
    def test_fc_codes_are_in_the_area_list(self):
        codes = {a["code"] for a in self.areas}
        self.assertTrue(set(self.raw["fc"]["each"]["code"]) <= codes)
        self.assertEqual(self.raw["fc"]["each"]["code"], PREPARING_CODES)

    # 2
    def test_ov_codes_are_fc_codes_that_answered_200(self):
        ov = self.raw["ov"]["each"]["code"]
        self.assertTrue(set(ov) <= set(self.raw["fc"]["each"]["code"]))
        for code in ov:
            self.assertEqual(self.probe[code]["ov"], 200, code)

    # 3
    def test_every_url_is_under_the_allowed_prefixes(self):
        self.assertEqual(self.cfg["source_url_prefixes"], PREFIXES)
        for src in self.sources:
            self.assertTrue(any(src["url"].startswith(p) for p in PREFIXES), src["url"])

    # 4
    def test_fetch_fits_in_the_deadline(self):
        timeouts = [s["timeout_seconds"] for s in self.sources]
        self.assertLessEqual(math.ceil(len(self.sources) / 4) * max(timeouts), 600)

    # 5
    def test_config_keys(self):
        self.assertEqual(set(self.cfg), set(common.CONFIG_REQUIRED) | {"site", "publish_data", "source_url_prefixes"})
        self.assertEqual(set(self.cfg["site"]), {"weather_sections_enabled", "warning_delay_minutes"})
        self.assertIs(self.cfg["site"]["weather_sections_enabled"], False)
        self.assertIsNone(self.cfg["site"]["warning_delay_minutes"])
        self.assertIs(self.cfg["publish_data"], False)

    # 6
    def test_area_names_are_the_original_names(self):
        self.assertEqual([a["code"] for a in self.areas], sorted(c for c, r in self.probe.items() if r["fc"] == 200))
        for area in self.areas:
            self.assertEqual(area["name"], self.offices[area["code"]]["name"], area["code"])

    # 7
    def test_fc_and_ov_watch_for_a_silent_stop(self):
        for sid in ("fc", "ov"):
            self.assertEqual(self.raw[sid]["max_report_age_hours"], 24, sid)

    def test_license_and_attribution_follow_the_original(self):
        sources_txt = (FIXTURES / "SOURCES.txt").read_text(encoding="utf-8")
        self.assertIn("出典：気象庁ホームページ （当該ページのURL）", sources_txt)
        self.assertIn("公共データ利用規約（第1.0版）", sources_txt)
        for sid in ("fc", "ov"):
            src = self.raw[sid]
            self.assertEqual(src["license"], "公共データ利用規約（第1.0版）")
            self.assertEqual(src["license_url"], "https://www.jma.go.jp/jma/kishou/info/coment.html")
            self.assertEqual(src["attribution"], "出典：気象庁ホームページ （https://www.jma.go.jp/bosai/forecast/）")

    def test_daily_worst_is_within_the_budget(self):
        worst = site_hooks.daily_worst_bytes(self.sources)
        self.assertEqual(worst, 48 * (200_000 + 50_000) * len(PREPARING_CODES))
        self.assertLessEqual(worst, site_hooks.DAILY_BUDGET_BYTES)


if __name__ == "__main__":
    unittest.main()
