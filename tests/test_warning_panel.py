"""警報欄（tools/render_weather.py）と禁止句の関所（site_hooks.check_dist）。計画書 9.6 の試験1〜11。"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import check_dist  # noqa: E402
import common  # noqa: E402
import fetch  # noqa: E402
import fetch_jma_warning as fjw  # noqa: E402
import site_hooks  # noqa: E402
from test_fetch_jma_warning import FEED_URL, TELEGRAMS  # noqa: E402

FIXTURES = helpers.ROOT / "tests" / "fixtures" / "jma"
CODES = ("130000", "270000")
FC_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{}.json"
OV_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{}.json"
NOW = datetime(2026, 10, 24, 5, 0, 0, tzinfo=timezone.utc)
PREPARING = "<p>準備中です。天気の欄はまだ表示していません。最新の情報は気象庁のサイトで確かめてください。</p>\n"
NOT_MEANING = "警報・注意報が出ていないという意味ではありません"
NULL_NOTICE = "この欄の遅れの大きさはまだ測っていません。数時間遅れたり止まったりすることがあります。必ず気象庁の警報・注意報のページで確かめてください。"
TIME_LINE = re.compile(r"最後に取得を試みた時刻: [^／<]+／フィードの最終更新: [^<]+</p>")


class Copies(HTMLParser):
    """関所とは別の読み方で、警報欄の data-copy の要素（場所と文字）を集める。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[list, str]] = []
        self._open: list | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-copy") == "warn":
            self._open = [tag, json.loads(a["data-path"]), []]

    def handle_endtag(self, tag):
        if self._open and self._open[0] == tag:
            self.items.append((self._open[1], "".join(self._open[2])))
            self._open = None

    def handle_data(self, data):
        if self._open:
            self._open[2].append(data)


class Scenario:
    """1つの条件の作り物（一時の site/・偽の取得関数の中身・取り込みと build）。"""

    def __init__(self, base: Path):
        self.tmp = base
        self.root = self.tmp / "root"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")
        self.routes = {FEED_URL: (FIXTURES / "extra.xml").read_bytes(), fjw.BACKFILL_FEED_URL: (FIXTURES / "extra_l.xml").read_bytes()}
        for name, fname in TELEGRAMS.items():
            self.routes[fjw.LINK_PREFIX + name] = (FIXTURES / fname).read_bytes()
        for code in CODES:
            self.routes[FC_URL.format(code)] = (FIXTURES / f"fc-{code}.json").read_bytes()
            self.routes[OV_URL.format(code)] = (FIXTURES / f"ov-{code}.json").read_bytes()

    def turn_on(self, delay: int | None = None) -> None:
        path = self.root / "site" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["site"] = {"weather_sections_enabled": True, "warning_delay_minutes": delay}
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        index = self.root / "site" / "pages" / "index.html"
        index.write_text(index.read_text(encoding="utf-8").replace(PREPARING, ""), encoding="utf-8")

    def fetch_all(self, now: datetime = NOW) -> None:
        web = helpers.FakeWeb(self.routes)
        fetch.run(self.root, fetcher=web, previous_base_url=None, now=now)
        fjw.run(self.root, fetcher=web, previous_base_url=None, now=now)

    def build(self) -> Path:
        out = self.tmp / "dist"
        build.build(self.root, out, production=False, now=NOW)
        return out

    def gate(self, out: Path) -> tuple[list[str], list[str]]:
        cfg = common.load_config(self.root, production=False)
        return check_dist.check_dist(out, production=False, now=NOW, data_dir=self.root / "data", cfg=cfg)

    def warn_json(self) -> dict:
        return json.loads((self.root / "data" / "warn.json").read_text(encoding="utf-8"))

    # ---- 5条件を作る ----
    def condition1(self) -> Path:
        self.turn_on()
        self.fetch_all()
        return self.build()

    def condition2(self) -> Path:
        self.turn_on()
        self.fetch_all()
        payload = self.warn_json()
        payload["items"] = [i for i in payload["items"] if i["office"] != "130000"]  # 取得した範囲に東京都の電文が無い
        (self.root / "data" / "warn.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.build()

    def condition3(self) -> Path:
        self.turn_on()
        self.fetch_all()
        self.routes[fjw.LINK_PREFIX + "20261024010526_0_VPWW61_270000.xml"] = fetch.FetchError("HTTP 500")
        payload = self.warn_json()
        payload["items"] = [i for i in payload["items"] if i["kind"] != "VPWW61" or i["office"] != "270000"]
        (self.root / "data" / "warn.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        fjw.run(self.root, fetcher=helpers.FakeWeb(self.routes), previous_base_url=None, now=NOW.replace(minute=30))
        return self.build()

    def condition4(self) -> Path:
        self.turn_on()
        del self.routes[FEED_URL]
        self.fetch_all()
        return self.build()

    def condition5(self) -> Path:
        self.turn_on()
        name = "20261024010526_0_VPWW61_270000.xml"
        body = self.routes[fjw.LINK_PREFIX + name]
        self.routes[fjw.LINK_PREFIX + name] = re.sub(rb"(<Headline>\s*<Text>)[^<]*(</Text>)", rb"\1\2", body, count=1)
        self.fetch_all()
        return self.build()

    def delay45(self) -> Path:
        self.turn_on(delay=45)
        self.fetch_all()
        return self.build()


def panel(out: Path, code: str = "130000") -> str:
    html = (out / "area" / f"{code}.html").read_text(encoding="utf-8")
    return re.search(r'<section data-weather-section="warn">.*?</section>', html, re.S).group(0)


class WarningPanelTest(unittest.TestCase):
    """条件ごとの作り物は、組の最初に1回だけ作って使い回す（試験は読むだけ。書き換える試験は写しで行う）。

    条件を作るたびに取り込み（長期フィード 3.7MB を含む）と build を流すと重く、検算（壊した版ごとに試験一式を流す）で
    CI の test ジョブの時間の大半を占めたため（PR #7 で 13分38秒）。
    """

    CONDITIONS = ("condition1", "condition2", "condition3", "condition4", "condition5", "delay45")

    @classmethod
    def setUpClass(cls):
        cls._shared = tempfile.TemporaryDirectory()
        cls.made: dict[str, tuple[Scenario, Path]] = {}
        for name in cls.CONDITIONS:
            scenario = Scenario(Path(cls._shared.name) / name)
            cls.made[name] = (scenario, getattr(scenario, name)())

    @classmethod
    def tearDownClass(cls):
        cls._shared.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def out(self, name: str) -> Path:
        return self.made[name][1]

    def copy_of(self, name: str) -> Path:
        """書き換える試験のための、作り物の dist の写し。"""
        target = self.tmp / f"{name}-dist"
        shutil.copytree(self.out(name), target)
        return target

    def panel(self, out: Path, code: str = "130000") -> str:
        return panel(out, code)

    # 1
    def test_each_condition_shows_its_text(self):
        out = self.out("condition1")
        self.assertIn("東京都波浪警報・注意報</h3>", self.panel(out))
        self.assertIn("この電文のほかに発表中の警報・注意報がある場合があります", self.panel(out))
        self.assertIn("この区域の警報・注意報の電文は、取得した範囲にありませんでした。", self.panel(self.out("condition2")))
        p3 = self.panel(self.out("condition3"), "270000")
        self.assertIn("最新の取得に失敗したか、新しさを確かめられませんでした（理由: HTTP 500）", p3)
        self.assertIn("警報・注意報を取得できませんでした（理由: HTTP 404）", self.panel(self.out("condition4")))
        self.assertIn("この電文の見出し文は空でした。", self.panel(self.out("condition5"), "270000"))

    # 2
    def test_notice_and_link_in_every_condition(self):
        for name, code in (("condition1", "130000"), ("condition2", "130000"), ("condition3", "270000"),
                           ("condition4", "130000"), ("condition5", "270000")):
            panel = self.panel(self.out(name), code)
            self.assertIn(NULL_NOTICE, panel, name)
            self.assertIn(f'<a href="https://www.jma.go.jp/bosai/warning/#area_type=offices&amp;area_code={code}" rel="noopener noreferrer">', panel)
            self.assertIn("「発表」「継続」の行だけを載せています", panel)

    # 3
    def test_not_meaning_none_and_previous(self):
        self.assertIn(NOT_MEANING, self.panel(self.out("condition2")))
        self.assertIn(NOT_MEANING, self.panel(self.out("condition4")))
        self.assertIn(NOT_MEANING, self.panel(self.out("condition5"), "270000"))
        p3 = self.panel(self.out("condition3"), "270000")
        self.assertIn("下は前回（", p3)
        self.assertIn("理由: HTTP 500", p3)

    # 4
    def test_forbidden_phrases_are_caught(self):
        out = self.copy_of("condition1")
        page = out / "about.html"
        text = page.read_text(encoding="utf-8")
        for bad in ("警報は　ありません", "警報等はありません", "注意報は\n解除されています", "ﾘｱﾙﾀｲﾑ", "常に最新"):
            page.write_text(text.replace("</main>", f"<p>{bad}</p></main>"), encoding="utf-8")
            problems, _ = site_hooks.phrase_problems(out)
            self.assertTrue(any("禁止句" in p and "about.html" in p for p in problems), bad)
        page.write_text(text, encoding="utf-8")
        js = out / "extra.js"
        js.write_text('var s = "警報は ありません";\n', encoding="utf-8")
        problems, _ = site_hooks.phrase_problems(out)
        self.assertTrue(any("extra.js" in p for p in problems), problems)
        js.unlink()
        # 警報欄の中だけ「発表なし」「0件」も
        area = out / "area" / "130000.html"
        html = area.read_text(encoding="utf-8")
        area.write_text(html.replace("<h2>気象警報・注意報</h2>", "<h2>気象警報・注意報</h2><p>発表なし</p>"), encoding="utf-8")
        problems, _ = site_hooks.phrase_problems(out)
        self.assertTrue(any("発表なし" in p for p in problems), problems)
        self.assertIn("元のデータが0件でした", "取得できました（元のデータが0件でした）")  # 取得状況のページの文は全体には掛けない

    # 5
    def test_own_texts_are_not_caught(self):
        for name in ("condition1", "condition2", "condition3", "condition4", "condition5"):
            scenario, out = self.made[name]
            self.assertEqual(site_hooks.phrase_problems(out), ([], []), name)
            self.assertEqual(scenario.gate(out), ([], []), name)
        out = self.out("delay45")
        self.assertIn("この欄は、当サイトの観測では最大45分遅れました（気象庁側の遅れは含みません）。", self.panel(out))
        self.assertEqual(site_hooks.phrase_problems(out), ([], []))

    # 6
    def test_phrases_inside_data_copy_are_not_caught(self):
        out = self.copy_of("condition1")
        area = out / "area" / "130000.html"
        html = area.read_text(encoding="utf-8")
        self.assertIn(">注意報を解除します。</p>", html)  # 気象庁の原文の見出し文（data-copy の中）
        self.assertEqual(site_hooks.phrase_problems(out), ([], []))
        area.write_text(html.replace(">注意報を解除します。</p>", ">注意報を解除します。</p><p>注意報は解除されています</p>", 1), encoding="utf-8")
        self.assertTrue(site_hooks.phrase_problems(out)[0])

    # 7
    def test_telegram_strings_are_copied_character_for_character(self):
        scenario, out = self.made["condition1"]
        payload = scenario.warn_json()
        for code in CODES:
            scan = Copies()
            scan.feed((out / "area" / f"{code}.html").read_text(encoding="utf-8"))
            self.assertTrue(scan.items)
            for path, text in scan.items:
                value = payload
                for key in path:
                    value = value[key]
                self.assertEqual(text, value, path)
        self.assertEqual(scenario.gate(out), ([], []))

    # 8
    def test_section_max_age_is_the_delay_or_90(self):
        self.assertIn('data-max-age-minutes="90"', self.panel(self.out("condition1")))
        panel = self.panel(self.out("delay45"))
        self.assertIn('<p data-section-stale data-fetched-at="2026-10-24T05:00:00Z" data-max-age-minutes="45" hidden></p>', panel)

    # 9
    def test_time_line_in_conditions_2_and_4(self):
        self.assertRegex(self.panel(self.out("condition2")), TIME_LINE)
        panel = self.panel(self.out("condition4"))
        self.assertRegex(panel, TIME_LINE)
        self.assertIn("フィードの最終更新: 確かめられませんでした", panel)  # jma-feed の記録が無い

    # 10
    def test_stale_watcher_shows_condition_3_or_4(self):
        scenario = Scenario(self.tmp)  # この試験だけの作り物（見張り役が古い）
        scenario.turn_on()
        stale = scenario.routes[FEED_URL].replace(b"2026-10-24T13:41:33+09:00", b"2026-10-24T11:41:33+09:00")  # 2時間前
        # 見張り役が古い（取り込みは ok で warn.json はある）→ 条件3（理由に見張り役）
        scenario.routes[FEED_URL] = stale
        scenario.fetch_all()
        out = scenario.build()
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["sources"]["warn"]["state"], common.STATE_UNREADABLE)
        panel = self.panel(out)
        self.assertIn("下は前回（", panel)
        self.assertIn("見張り役（jma-feed）", panel)
        self.assertNotIn("この区域の警報・注意報の電文は", panel)
        # warn.json も無い → 条件4（理由に見張り役）
        (scenario.root / "data" / "warn.json").unlink()
        panel = self.panel(scenario.build())
        self.assertIn("警報・注意報を取得できませんでした（理由: 見張り役（jma-feed）", panel)

    # 11
    def test_two_kinds_of_the_same_office_both_appear(self):
        panel = self.panel(self.out("condition1"))
        titles = re.findall(r"<h3[^>]*>([^<]+)</h3>", panel)
        self.assertGreaterEqual(len(titles), 2)
        self.assertIn("東京都波浪警報・注意報", titles)
        self.assertIn("東京都雷注意報", titles)

    def test_only_issued_and_continued_rows_are_shown(self):
        scenario, out = self.made["condition1"]
        payload = scenario.warn_json()
        shown = 0
        for code in CODES:
            scan = Copies()
            scan.feed((out / "area" / f"{code}.html").read_text(encoding="utf-8"))
            for path, _ in scan.items:
                if "kinds" in path:
                    kind = payload
                    for key in path[:-1]:
                        kind = kind[key]
                    self.assertIn(kind["status"], ("発表", "継続"), path)
                    shown += 1
        self.assertGreater(shown, 0)
        # 解除だけの電文（東京都の暴風（雪））は、行が無いことを「出ていない」と読ませない
        self.assertIn("この電文には、状態が「発表」「継続」の行がありませんでした。" + NOT_MEANING, self.panel(out))


if __name__ == "__main__":
    unittest.main()
