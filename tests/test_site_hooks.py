"""第1号の関所（tools/site_hooks.py）: G-名前・設定キー・G-取得量・G-準備中（計画書 5.2・9.3）。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import common  # noqa: E402
import site_hooks  # noqa: E402

NOW = helpers.NOW


class NameAndBudgetTest(unittest.TestCase):
    """check_repo（config・wrangler の名前と取得量）と check_config（設定キー）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")
        shutil.copyfile(helpers.ROOT / "wrangler.jsonc", self.root / "wrangler.jsonc")
        (self.root / "tools").mkdir()
        shutil.copyfile(helpers.ROOT / "tools" / "fetch_jma_warning.py", self.root / "tools" / "fetch_jma_warning.py")

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self) -> list[str]:
        problems, unreadable = site_hooks.check_repo(root=self.root, production=True)
        self.assertEqual(unreadable, [])
        return problems

    def set_config(self, **values) -> None:
        path = self.root / "site" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg.update(values)
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def assertCaught(self, needle: str) -> None:
        problems = self.problems()
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} が検出されませんでした: {problems}")

    def test_default_names_pass(self):
        self.assertEqual(self.problems(), [])

    def test_forbidden_word_in_the_site_name(self):
        for bad in ("気象庁の写し（非公式）", "そらの写し 公式版（非公式）", "tenki うつし（非公式）", "そらの写し 臨時版（非公式）"):
            self.set_config(site_name=bad)
            self.assertCaught("名前に使えない語")

    def test_full_width_and_spaced_words_are_caught(self):
        for bad in ("ＪＭＡの写し（非公式）", "気 象 庁の写し（非公式）", "Ｙａｈｏｏ天気の写し（非公式）"):
            self.set_config(site_name=bad)
            self.assertCaught("名前に使えない語")

    def test_site_name_must_end_with_unofficial(self):
        self.set_config(site_name="そらの写し")
        self.assertCaught("（非公式）」で終わる")

    def test_forbidden_word_in_the_host_name(self):
        self.set_config(site_url="https://jma-utsushi.mateit-jp.workers.dev/")
        self.assertCaught("ホスト名")

    def test_forbidden_word_in_the_worker_name(self):
        path = self.root / "wrangler.jsonc"
        text = path.read_text(encoding="utf-8")
        self.assertIn('"name": "sora-utsushi"', text)
        path.write_text(text.replace('"name": "sora-utsushi"', '"name": "tenki-utsushi"'), encoding="utf-8")
        self.assertCaught("wrangler.jsonc の name")

    def test_daily_budget(self):
        path = self.root / "site" / "sources.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        codes = [a["code"] for a in json.loads((self.root / "site" / "areas.json").read_text(encoding="utf-8"))["offices"]]
        raw["sources"][0]["each"]["code"] = codes
        raw["sources"][0]["max_bytes"] = 1_000_000  # 48 × 1,000,000 × 56 区域 ＞ 2,000,000,000
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertCaught("予算")

    def test_custom_source_without_a_rule_is_not_counted_as_zero(self):
        path = self.root / "site" / "sources.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        warn = next(s for s in raw["sources"] if s["id"] == "warn")
        raw["sources"].append({**warn, "id": "other", "heartbeat": None})
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertCaught("custom の取得先 other の最悪値を計算できません")

    def test_custom_constants_that_cannot_be_read_are_not_counted_as_zero(self):
        path = self.root / "tools" / "fetch_jma_warning.py"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("MAX_LINKS_PER_RUN = 50", "MAX_LINKS_PER_RUN = int('50')"), encoding="utf-8")
        self.assertCaught("custom の取得先 warn の最悪値を計算できません")
        path.unlink()
        self.assertCaught("custom の取得先 warn の最悪値を計算できません")

    def test_custom_worst_is_added_to_the_budget(self):
        # warn の上限を大きくすると、custom でない取得先だけなら予算内でも、足した値で予算を超える
        path = self.root / "tools" / "fetch_jma_warning.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn("MAX_BYTES_PER_LINK = 200_000", text)
        path.write_text(text.replace("MAX_BYTES_PER_LINK = 200_000", "MAX_BYTES_PER_LINK = 1_000_000"), encoding="utf-8")
        self.assertCaught("予算")

    def test_sources_that_cannot_be_read_are_unreadable(self):
        (self.root / "site" / "sources.json").write_text("{", encoding="utf-8")
        _, unreadable = site_hooks.check_repo(root=self.root, production=True)
        self.assertTrue(any("sources.json" in u for u in unreadable), unreadable)

    # ---- 設定キー（計画書 4.6） ----
    def test_site_settings(self):
        ok = {"weather_sections_enabled": False, "warning_delay_minutes": None}
        self.assertEqual(site_hooks.check_config(ok, production=True), [])
        for minutes in (30, 89):
            self.assertEqual(site_hooks.check_config({**ok, "warning_delay_minutes": minutes}, production=True), [], minutes)
        for bad in (29, 90, "60", True, 45.0):
            self.assertTrue(site_hooks.check_config({**ok, "warning_delay_minutes": bad}, production=True), bad)
        self.assertTrue(site_hooks.check_config({**ok, "weather_sections_enabled": "false"}, production=True))
        self.assertTrue(site_hooks.check_config({**ok, "third": 1}, production=True))
        self.assertTrue(site_hooks.check_config({"weather_sections_enabled": False}, production=True))

    def test_build_stops_on_a_bad_site_setting(self):
        self.set_config(site={"weather_sections_enabled": False, "warning_delay_minutes": 90})
        with self.assertRaises(common.ConfigError):
            common.load_config(self.root, production=False)


class PreparingTest(unittest.TestCase):
    """check_dist（トップと全ページの名前・G-準備中）。実物の site/ を作って試す。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")
        self.out = self.tmp / "dist"
        build.build(self.root, self.out, production=True, now=NOW)
        self.cfg = common.load_config(self.root, production=True)

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, cfg: dict | None = None) -> list[str]:
        problems, unreadable = site_hooks.check_dist(dist=self.out, data_dir=self.tmp / "data", cfg=cfg or self.cfg, production=True, now=NOW)
        self.assertEqual(unreadable, [])
        return problems

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.out / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def assertCaught(self, needle: str, cfg: dict | None = None) -> None:
        problems = self.problems(cfg)
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} が検出されませんでした: {problems}")

    def test_real_site_passes(self):
        self.assertEqual(self.problems(), [])

    def test_top_page_says_preparing_and_footer_has_the_source(self):
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("準備中です。天気の欄はまだ表示していません。最新の情報は気象庁のサイトで確かめてください。", html)
        self.assertIn("<title>そらの写し（非公式）</title>", html)
        for name in ("index.html", "about.html", "404.html"):
            page = (self.out / name).read_text(encoding="utf-8")
            self.assertIn("出典：気象庁ホームページ （https://www.jma.go.jp/）", page, name)
            self.assertIn("当サイトが加えた変更は次のとおりです", page, name)
        about = (self.out / "about.html").read_text(encoding="utf-8")
        self.assertIn("運営者: メイトイット（mateit）", about)
        self.assertIn("連絡先: mateit.jp@gmail.com", about)
        self.assertEqual((self.out / "robots.txt").read_text(encoding="utf-8"), "User-agent: *\nDisallow: /\n")

    # ---- G-名前（dist） ----
    def test_forbidden_name_in_the_title(self):
        self.edit("index.html", "<title>そらの写し（非公式）</title>", "<title>気象庁の写し（非公式）</title>")
        self.assertCaught("index.html の <title>")

    def test_forbidden_name_in_the_h1(self):
        self.edit("index.html", "<h1>そらの写し（非公式）</h1>", "<h1>ＮＨＫ天気の写し（非公式）</h1>")
        self.assertCaught("index.html の <h1>")

    def test_forbidden_name_in_a_meta(self):
        self.edit("about.html", "</head>", '<meta property="og:site_name" content="Official Weather"></head>')
        self.assertCaught("og:site_name")

    def test_forbidden_name_in_a_manifest(self):
        (self.out / "manifest.json").write_text(json.dumps({"name": "そらの写し", "short_name": "本家"}, ensure_ascii=False), encoding="utf-8")
        self.assertCaught("manifest.json の short_name")

    # ---- G-準備中（旗が false） ----
    def test_weather_section_while_preparing(self):
        self.edit("index.html", "</main>", '<section data-weather-section="fc"><h2>天気</h2></section></main>')
        self.assertCaught("data-weather-section")

    def test_area_pages_while_preparing(self):
        (self.out / "area").mkdir()
        self.assertCaught("dist/area/")

    def test_publish_data_while_preparing(self):
        self.assertCaught("publish_data", cfg={**self.cfg, "publish_data": True})

    def test_robots_allow_while_preparing(self):
        (self.out / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
        problems = self.problems()
        self.assertTrue(any("Allow: の行" in p for p in problems), problems)
        self.assertTrue(any("Disallow: / がありません" in p for p in problems), problems)

    def test_robots_missing_while_preparing(self):
        (self.out / "robots.txt").unlink()
        self.assertCaught("robots.txt がありません")

    def test_top_without_preparing_text(self):
        self.edit("index.html", "準備中です。", "")
        self.assertCaught("「準備中です」がありません")

    def test_preparing_text_only_outside_main_does_not_count(self):
        self.edit("index.html", "準備中です。", "")
        self.edit("index.html", "<title>", '<meta name="description" content="準備中です"><title>')
        self.assertCaught("「準備中です」がありません")

    # ---- G-準備中（旗が true） ----
    def test_preparing_text_left_when_the_flag_is_true(self):
        on = {**self.cfg, "site": {**self.cfg["site"], "weather_sections_enabled": True}}
        self.assertCaught("「準備中です」が残っています", cfg=on)
        self.edit("index.html", "準備中です。", "")
        self.assertEqual(self.problems(on), [])

    def test_without_cfg_is_unreadable_not_clean(self):
        problems, unreadable = site_hooks.check_dist(dist=self.out, data_dir=self.tmp / "data", cfg=None, production=True, now=NOW)
        self.assertEqual(problems, [])
        self.assertTrue(unreadable)

    def test_no_area_pages_are_made_while_preparing(self):
        self.assertFalse((self.out / "area").exists())
        self.assertFalse((self.out / "data").exists())
        self.assertEqual(site_hooks.pages(root=self.root, cfg=self.cfg, sources=[], state={}, now=NOW), {})


if __name__ == "__main__":
    unittest.main()
