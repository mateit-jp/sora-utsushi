from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import check_dist  # noqa: E402

NOW = helpers.NOW


class BuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, root: Path, *, production: bool = True) -> Path:
        out = self.tmp / "dist"
        build.build(root, out, production=production, now=NOW)
        return out

    def test_clean_build_passes_check_dist(self):
        root = helpers.make_root(self.tmp)
        out = self.build(root)
        problems, unreadable = check_dist.check_dist(out, production=True, now=NOW, cfg=helpers.load_cfg(root))
        self.assertEqual(problems, [])
        self.assertEqual(unreadable, [])

    def test_template_as_copied_cannot_be_built_for_production(self):
        # 未設定の印が残った site（写したままの雛形の値）は公開用に作れない。実物は値を入れたので、雛形の値を写しに書き戻して試す
        root = self.tmp / "root"
        shutil.copytree(helpers.ROOT / "site", root / "site")
        cfg = json.loads((root / "site" / "config.json").read_text(encoding="utf-8"))
        cfg["site_name"] = "サイトの名前（未設定）"
        (root / "site" / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(build.BuildError) as cm:
            build.build(root, self.tmp / "dist", production=True, now=NOW)
        self.assertIn("未設定", str(cm.exception))

    def test_production_lists_every_placeholder(self):
        root = helpers.make_root(self.tmp, config={"contact_url": "https://example.invalid/contact"})
        with self.assertRaises(build.BuildError) as cm:
            self.build(root)
        self.assertIn("contact_url", str(cm.exception))

    def test_non_production_build_allows_placeholders(self):
        out = self.tmp / "dist"
        build.build(helpers.ROOT, out, production=False, now=NOW)
        self.assertTrue((out / "index.html").is_file())

    def test_meta_csp_is_header_csp_without_frame_ancestors(self):
        out = self.build(helpers.make_root(self.tmp))
        rules = check_dist.parse_headers_file((out / "_headers").read_text(encoding="utf-8"))
        header_csp, _ = check_dist.parse_csp(rules["/*"]["content-security-policy"])
        self.assertEqual(header_csp["frame-ancestors"], ["'none'"])
        scan = check_dist.PageScan()
        scan.feed((out / "index.html").read_text(encoding="utf-8"))
        meta_csp, _ = check_dist.parse_csp(scan.meta_csp[0])
        self.assertNotIn("frame-ancestors", meta_csp)
        self.assertEqual(meta_csp, {k: v for k, v in header_csp.items() if k != "frame-ancestors"})

    def test_source_never_fetched_is_unreadable_not_zero(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        out = self.build(root)  # data/state.json が無い ＝ 一度も取れていない
        html = (out / "status.html").read_text(encoding="utf-8")
        self.assertIn("取得できませんでした", html)
        self.assertIn("まだ一度も取得していません", html)
        self.assertNotIn("0件", html)
        self.assertNotIn("0 件", html)
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources"]["s1"]["state"], "unreadable")

    def test_broken_state_record_falls_back_to_unreadable(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        helpers.write_state(root, {"s1": {"state": "unknown-word"}})
        out = self.build(root)
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources"]["s1"]["state"], "unreadable")

    def test_empty_source_says_the_source_had_zero(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        helpers.write_state(root, {"s1": {"state": "empty", "checked_at": "2026-10-23T05:59:00Z", "last_success_at": "2026-10-23T05:59:00Z", "items": 0, "error": None, "showing_previous": False}})
        html = (self.build(root) / "status.html").read_text(encoding="utf-8")
        self.assertIn("元のデータが0件でした", html)

    def test_previous_data_is_labelled_as_previous(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        helpers.write_state(root, {"s1": {"state": "unreadable", "checked_at": "2026-10-23T05:59:00Z", "last_success_at": "2026-10-23T03:00:00Z", "items": 3, "error": "HTTP 503", "showing_previous": True}})
        html = (self.build(root) / "status.html").read_text(encoding="utf-8")
        self.assertIn("前回（2026年10月23日 12:00（日本時間））に取得した内容を表示しています", html)
        self.assertIn("理由: HTTP 503", html)

    def test_status_json_carries_the_freshness_limit_for_the_banner(self):
        out = self.build(helpers.make_root(self.tmp))
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["max_status_age_minutes"], 180)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/freshness.js" defer></script>', html)
        self.assertIn("data-stale-banner hidden", html)

    def test_report_time_is_shown_on_the_status_page(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()])
        helpers.write_state(root, {"s1": {"state": "ok", "checked_at": "2026-10-23T05:59:00Z", "last_success_at": "2026-10-23T05:59:00Z", "items": 2, "report_time": "2026-10-23T02:00:00Z", "error": None, "showing_previous": False}})
        html = (self.build(root) / "status.html").read_text(encoding="utf-8")
        self.assertIn("元の発表時刻 2026年10月23日 11:00（日本時間）", html)

    def test_heartbeat_is_applied_to_records_written_by_a_site_specific_fetcher(self):
        feed = helpers.source(id="hb", format="xml", items_path=["entry"])
        warn = helpers.source(id="warn", url="https://data.example.com/warn.json", format="custom", heartbeat="hb")
        root = helpers.make_root(self.tmp, sources=[feed, warn])
        ok = {"checked_at": "2026-10-23T05:59:00Z", "last_success_at": "2026-10-23T05:59:00Z", "items": 1, "error": None, "showing_previous": False}
        helpers.write_state(root, {"hb": {**ok, "state": "unreadable", "error": "古い"}, "warn": {**ok, "state": "ok"}})
        status = json.loads((self.build(root) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources"]["warn"]["state"], "unreadable")
        self.assertIn("見張り役（hb）", status["sources"]["warn"]["error"])

    # ---- 第5版: サイト専用の呼び口・設定の厳しさ・データの公開 ----
    def test_unknown_config_key_is_refused(self):
        root = helpers.make_root(self.tmp, config={"weather_sections_enabled": False})
        with self.assertRaises(build.BuildError) as cm:
            self.build(root)
        self.assertIn("知らない項目", str(cm.exception))

    def test_site_settings_need_a_rule_in_site_hooks(self):
        root = helpers.make_root(self.tmp, config={"site": {"weather_sections_enabled": True, "warning_delay_minutes": None, "no_rule_yet": 1}})
        with self.assertRaises(build.BuildError) as cm:
            self.build(root)
        self.assertIn("site_hooks.py", str(cm.exception))

    def test_extra_pages_from_site_hooks_are_rendered_with_the_banner(self):
        root = helpers.make_root(self.tmp)
        pages = {"area/130000.html": "<h1>東京都</h1>\n<p>見本</p>"}
        with mock.patch.object(build.site_hooks, "pages", return_value=pages):
            out = self.build(root)
        html = (out / "area" / "130000.html").read_text(encoding="utf-8")
        self.assertIn("data-unofficial-banner", html)
        self.assertIn("<title>東京都 | 試験のサイト</title>", html)
        problems, _ = check_dist.check_dist(out, production=True, now=NOW, cfg=helpers.load_cfg(root))
        self.assertEqual(problems, [])

    def test_extra_page_names_are_checked(self):
        # ★名前の検査を壊した版（検算）でも外へ書かないよう、絶対パスは試さない（../ は一時フォルダの中に収まる）
        for bad in ("../x.html", "x.htm", "status.html", "A.html", "index.html"):
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)))
            with mock.patch.object(build.site_hooks, "pages", return_value={bad: "<h1>x</h1>"}):
                with self.assertRaises(build.BuildError, msg=bad):
                    build.build(root, Path(tempfile.mkdtemp(dir=self.tmp)) / "dist", production=True, now=NOW)

    def test_publish_data_false_keeps_data_out_of_dist(self):
        root = helpers.make_root(self.tmp, sources=[helpers.source()], config={"publish_data": False})
        (root / "data").mkdir()
        (root / "data" / "s1.json").write_text('{"fetched_at": "2026-10-23T06:00:00Z", "items": []}', encoding="utf-8")
        out = self.build(root)
        self.assertFalse((out / "data").exists())
        cfg = json.loads((root / "site" / "config.json").read_text(encoding="utf-8"))
        cfg = {**build.common.CONFIG_OPTIONAL, **cfg}
        problems, _ = check_dist.check_dist(out, production=True, now=NOW, cfg=cfg)
        self.assertEqual(problems, [])
        (out / "data").mkdir()
        problems, _ = check_dist.check_dist(out, production=True, now=NOW, cfg=cfg)
        self.assertTrue(any("publish_data が false なのに" in p for p in problems), problems)

    def test_strings_from_outside_are_escaped(self):
        evil = helpers.source(name="<script>alert(1)</script>", attribution='"><img src=x onerror=alert(1)>')
        root = helpers.make_root(self.tmp, sources=[evil])
        helpers.write_state(root, {"s1": {"state": "unreadable", "checked_at": None, "last_success_at": None, "items": None, "error": "<b>x</b>", "showing_previous": False}})
        out = self.build(root)
        for name in ("sources.html", "status.html"):
            html = (out / name).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn("<b>x</b>", html)
        problems, _ = check_dist.check_dist(out, production=True, now=NOW, cfg=helpers.load_cfg(root))
        self.assertEqual(problems, [])

    def test_page_named_like_a_generated_page_is_refused(self):
        root = helpers.make_root(self.tmp)
        (root / "site" / "pages" / "status.html").write_text("<h1>x</h1>", encoding="utf-8")
        with self.assertRaises(build.BuildError):
            self.build(root)

    def test_page_without_h1_is_refused(self):
        root = helpers.make_root(self.tmp)
        (root / "site" / "pages" / "index.html").write_text("<p>題がない</p>", encoding="utf-8")
        with self.assertRaises(build.BuildError):
            self.build(root)

    def test_refuses_a_dist_that_is_a_symlink(self):
        root = helpers.make_root(self.tmp)
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("消えてはいけない", encoding="utf-8")
        (root / "dist").symlink_to(victim, target_is_directory=True)
        # ★関所を試す試験が関所に守られてはいけない: 消す処理を先に塞いでから呼ぶ
        with mock.patch.object(build.shutil, "rmtree", side_effect=AssertionError("rmtree が呼ばれた")) as rmtree:
            with self.assertRaises(build.BuildError):
                build.reset_out_dir(root / "dist", root)
        rmtree.assert_not_called()
        self.assertTrue((victim / "keep.txt").exists())

    def test_headers_json_values_that_could_split_lines_are_refused(self):
        for key, value in (("csp", {"frame-ancestors": ["'none'\u000b  X: y"]}), ("csp", {"script-src": ["'self'; script-src-elem 'unsafe-inline'"]}), ("headers", {"/x": "y"}), ("headers", {"X-Test": "a\nb"})):
            root = helpers.make_root(Path(tempfile.mkdtemp(dir=self.tmp)))
            spec = json.loads((root / "site" / "headers.json").read_text(encoding="utf-8"))
            spec[key].update(value)
            (root / "site" / "headers.json").write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaises(build.BuildError, msg=str(value)):
                self.build(root)

    def test_refuses_to_reset_a_dir_outside_dist_and_temp(self):
        root = helpers.make_root(self.tmp)
        # ★関所を試す試験が関所に守られてはいけない: 消す・作る処理を先に塞いでから呼ぶ
        with mock.patch.object(build.shutil, "rmtree", side_effect=AssertionError("rmtree が呼ばれた")) as rmtree, \
                mock.patch.object(build.Path, "mkdir", side_effect=AssertionError("mkdir が呼ばれた")) as mkdir:
            for target in (Path.home(), Path("/")):
                with self.assertRaises(build.BuildError):
                    build.reset_out_dir(target, root)
        rmtree.assert_not_called()
        mkdir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
