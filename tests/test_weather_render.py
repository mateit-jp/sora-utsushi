"""区域ページの描画と写しの関所（tools/render_weather.py・site_hooks.check_dist）。計画書 9.5 の試験1〜10。"""
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
import site_hooks  # noqa: E402

FIXTURES = helpers.ROOT / "tests" / "fixtures" / "jma"
CODES = ("130000", "270000")
FC_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{}.json"
OV_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{}.json"
NOW = datetime(2026, 10, 24, 5, 0, 0, tzinfo=timezone.utc)
PREPARING = "<p>準備中です。天気の欄はまだ表示していません。最新の情報は気象庁のサイトで確かめてください。</p>\n"


class Elements(HTMLParser):
    """関所とは別の読み方で、data-copy の要素の文字と、外へのリンクと、<script> を数える。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.copies: list[tuple[str, list, str]] = []
        self.links: list[dict] = []
        self.scripts = 0
        self._open: list[list] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.scripts += 1
        if tag == "a" and (a.get("href") or "").startswith("https://"):
            self.links.append(a)
        if "data-copy" in a:
            self._open.append([tag, a["data-copy"], json.loads(a["data-path"]), []])

    def handle_endtag(self, tag):
        if self._open and self._open[-1][0] == tag:
            _, sid, path, text = self._open.pop()
            self.copies.append((sid, path, "".join(text)))

    def handle_data(self, data):
        if self._open:
            self._open[-1][3].append(data)


def parse(path: Path) -> Elements:
    e = Elements()
    e.feed(path.read_text(encoding="utf-8"))
    e.close()
    return e


class WeatherRenderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "root"
        shutil.copytree(helpers.ROOT / "site", self.root / "site")
        self.routes = {}
        for code in CODES:
            self.routes[FC_URL.format(code)] = (FIXTURES / f"fc-{code}.json").read_bytes()
            self.routes[OV_URL.format(code)] = (FIXTURES / f"ov-{code}.json").read_bytes()

    def tearDown(self):
        self._tmp.cleanup()

    def turn_on(self) -> None:
        """旗を true にし、トップから準備中の文を除く（G-準備中が止めるため。リポジトリの index.html は変えない）。"""
        path = self.root / "site" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["site"]["weather_sections_enabled"] = True
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        index = self.root / "site" / "pages" / "index.html"
        text = index.read_text(encoding="utf-8")
        self.assertIn(PREPARING, text)
        index.write_text(text.replace(PREPARING, ""), encoding="utf-8")

    def build(self, *, fetch_first: bool = True) -> Path:
        if fetch_first:
            fetch.run(self.root, fetcher=helpers.FakeWeb(self.routes), previous_base_url=None, now=NOW)
        out = self.tmp / "dist"
        build.build(self.root, out, production=False, now=NOW)
        return out

    def gate(self, out: Path, data_dir: Path | None = None) -> tuple[list[str], list[str]]:
        cfg = common.load_config(self.root, production=False)
        return check_dist.check_dist(out, production=False, now=NOW, data_dir=data_dir or self.root / "data", cfg=cfg)

    # 1
    def test_every_copy_matches_the_source_and_links_have_rel(self):
        self.turn_on()
        out = self.build()
        total = 0
        for code in CODES:
            page = parse(out / "area" / f"{code}.html")
            self.assertTrue(page.copies)
            for sid, path, text in page.copies:
                value = json.loads((self.root / "data" / f"{sid}.json").read_text(encoding="utf-8"))
                for key in path:
                    value = value[key]
                self.assertEqual(text, value, (sid, path))
                total += 1
            for link in page.links:
                self.assertEqual(link.get("rel"), "noopener noreferrer", link)
            self.assertTrue(any("area_type=offices&area_code=" + code in link["href"] for link in page.links))
        self.assertGreater(total, 50)

    # 2
    def test_every_weather_text_appears_in_a_copy(self):
        self.turn_on()
        out = self.build()
        for code in CODES:
            texts = {text for _, _, text in parse(out / "area" / f"{code}.html").copies}
            fc = json.loads((FIXTURES / f"fc-{code}.json").read_text(encoding="utf-8"))
            for area in fc[0]["timeSeries"][0]["areas"]:
                for weather in area["weathers"]:
                    self.assertIn(weather, texts)
            self.assertNotIn("weatherCodes", (out / "area" / f"{code}.html").read_text(encoding="utf-8"))

    # 3
    def test_report_and_fetch_times_are_shown(self):
        self.turn_on()
        out = self.build()
        html = (out / "area" / "130000.html").read_text(encoding="utf-8")
        fc = json.loads((FIXTURES / "fc-130000.json").read_text(encoding="utf-8"))
        ov = json.loads((FIXTURES / "ov-130000.json").read_text(encoding="utf-8"))
        for report in (fc[0]["reportDatetime"], ov["reportDatetime"]):
            self.assertIn(f'<time datetime="{report}">{common.jst_text(common.parse_report_time(report))}</time>', html)
            self.assertIn(f">{report}</span>", html)
        self.assertEqual(html.count('当サイトの取得時刻: <time datetime="2026-10-24T05:00:00Z">2026年10月24日 14:00（日本時間）</time>'), 2)

    # 4
    def test_unreadable_says_previous_or_not_yet_and_never_zero(self):
        self.turn_on()
        fetch.run(self.root, fetcher=helpers.FakeWeb(self.routes), previous_base_url=None, now=NOW)
        del self.routes[FC_URL.format("130000")]  # 前回分あり → 前回の内容と理由
        del self.routes[OV_URL.format("270000")]
        (self.root / "data" / "ov-270000.json").unlink()  # 前回分なし → まだ取得できていない
        fetch.run(self.root, fetcher=helpers.FakeWeb(self.routes), previous_base_url=None, now=NOW.replace(minute=30))
        out = self.build(fetch_first=False)
        tokyo = (out / "area" / "130000.html").read_text(encoding="utf-8")
        osaka = (out / "area" / "270000.html").read_text(encoding="utf-8")
        self.assertIn("下は前回（2026年10月24日 14:00（日本時間））に取得した内容です", tokyo)
        self.assertIn("理由: HTTP 404", tokyo)
        self.assertIn("この欄はまだ取得できていません（理由: HTTP 404）", osaka)
        for html in (tokyo, osaka):
            self.assertNotIn("0件", html)
            self.assertNotIn("0 件", html)
        self.assertEqual(self.gate(out), ([], []))

    # 5
    def test_script_in_the_source_does_not_add_a_script_element(self):
        self.turn_on()
        fc = json.loads(self.routes[FC_URL.format("130000")])
        fc[0]["timeSeries"][0]["areas"][0]["weathers"][0] = "<script>alert(1)</script>"
        ov = json.loads(self.routes[OV_URL.format("130000")])
        ov["text"] = "</p><script>alert(2)</script>"
        self.routes[FC_URL.format("130000")] = json.dumps(fc, ensure_ascii=False).encode("utf-8")
        self.routes[OV_URL.format("130000")] = json.dumps(ov, ensure_ascii=False).encode("utf-8")
        out = self.build()
        clean = parse(out / "area" / "270000.html").scripts
        self.assertEqual(parse(out / "area" / "130000.html").scripts, clean)
        self.assertEqual(self.gate(out), ([], []))

    # 6
    def test_area_without_waves_has_no_wave_column(self):
        self.turn_on()
        fc = json.loads(self.routes[FC_URL.format("270000")])
        del fc[0]["timeSeries"][0]["areas"][0]["waves"]
        self.routes[FC_URL.format("270000")] = json.dumps(fc, ensure_ascii=False).encode("utf-8")
        out = self.build()
        self.assertNotIn("<th>波</th>", (out / "area" / "270000.html").read_text(encoding="utf-8"))
        self.assertIn("<th>波</th>", (out / "area" / "130000.html").read_text(encoding="utf-8"))
        self.assertEqual(self.gate(out), ([], []))

    # 7
    def test_built_site_with_the_flag_on_passes_check_dist(self):
        self.turn_on()
        out = self.build()
        self.assertEqual(self.gate(out), ([], []))
        size = (out / "area" / "130000.html").stat().st_size
        self.assertLessEqual(size, 307_200)
        index = (out / "area" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<a href="/area/130000.html">東京都</a>', index)
        self.assertIn('<a href="/area/270000.html">大阪府</a>', index)
        self.assertIn("<h1>東京都の天気予報（気象庁の発表の写し）</h1>", (out / "area" / "130000.html").read_text(encoding="utf-8"))
        self.assertIn("<h1>そらの写し（非公式）</h1>", (out / "index.html").read_text(encoding="utf-8"))

    # 8
    def test_no_area_pages_or_data_while_preparing(self):
        out = self.build()
        self.assertFalse((out / "area").exists())
        self.assertFalse((out / "data").exists())

    # 9
    def test_each_section_has_a_stale_marker(self):
        self.turn_on()
        out = self.build()
        html = (out / "area" / "130000.html").read_text(encoding="utf-8")
        sections = re.findall(r'<section data-weather-section="(\w+)">(.*?)</section>', html, re.S)
        self.assertEqual([kind for kind, _ in sections], ["warn", "fc", "ov"])  # 警報欄は PR4（tests/test_warning_panel.py）
        for kind, body in sections[1:]:
            self.assertIn('<p data-section-stale data-fetched-at="2026-10-24T05:00:00Z" data-max-age-minutes="90" hidden></p>', body, kind)

    # 10
    def test_gate_reads_the_work_data_or_the_published_data(self):
        self.turn_on()
        out = self.build()
        # 作業場の data/ に JSON が無く、dist/data/ も無い → 検査できませんでした
        empty = self.tmp / "empty-data"
        empty.mkdir()
        problems, unreadable = self.gate(out, data_dir=empty)
        self.assertEqual(problems, [])
        self.assertTrue(any("fc-130000.json を読めませんでした" in u for u in unreadable), unreadable)
        # 作業場の data/ のフォルダが無く dist/data/ も無い → 検査できませんでした（「問題なし」にしない）
        missing = self.tmp / "no-such-data"
        problems, unreadable = self.gate(out, data_dir=missing)
        self.assertTrue(any("dist/data/ も無い" in u for u in unreadable), unreadable)
        # 作業場の data/ のフォルダが無く dist/data/ がある（publish_data が true の本公開の後）→ それで照合する
        path = self.root / "site" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["publish_data"] = True
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        shutil.copytree(self.root / "data", out / "data")
        self.assertEqual(self.gate(out, data_dir=missing), ([], []))

    # 写しの関所が止めるもの
    def edit(self, out: Path, old: str, new: str) -> None:
        path = out / "area" / "130000.html"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_gate_catches_a_changed_text(self):
        self.turn_on()
        out = self.build()
        fc = json.loads((FIXTURES / "fc-130000.json").read_text(encoding="utf-8"))
        weather = fc[0]["timeSeries"][0]["areas"][0]["weathers"][0]
        self.edit(out, f">{weather}</td>", f">{weather}（当サイト補足）</td>")
        problems, _ = self.gate(out)
        self.assertTrue(any("元の文字列と違います" in p for p in problems), problems)

    def test_gate_catches_a_child_element_and_a_bad_path(self):
        self.turn_on()
        out = self.build()
        fc = json.loads((FIXTURES / "fc-130000.json").read_text(encoding="utf-8"))
        weather = fc[0]["timeSeries"][0]["areas"][0]["weathers"][0]
        self.edit(out, f">{weather}</td>", f"><b>{weather}</b></td>")
        self.edit(out, "&quot;reportDatetime&quot;]", "&quot;nothing&quot;]")
        problems, _ = self.gate(out)
        self.assertTrue(any("子要素があります" in p for p in problems), problems)
        self.assertTrue(any("辿れません" in p for p in problems), problems)

    def test_gate_does_not_read_anything_while_preparing(self):
        out = self.build()
        self.assertEqual(site_hooks.copy_problems(out, self.tmp / "no-such-data"), ([], []))


if __name__ == "__main__":
    unittest.main()
