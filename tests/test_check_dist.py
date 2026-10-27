from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import helpers

import build  # noqa: E402  (helpers が tools/ を sys.path に入れる)
import check_dist  # noqa: E402

NOW = helpers.NOW


class CheckDistTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "dist"
        self.root = helpers.make_root(self.tmp)
        self.cfg = helpers.load_cfg(self.root)
        build.build(self.root, self.out, production=True, now=NOW)

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, *, production: bool = True) -> list[str]:
        problems, unreadable = check_dist.check_dist(self.out, production=production, now=NOW, cfg=self.cfg)
        self.assertEqual(unreadable, [])
        return problems

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.out / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def assertCaught(self, needle: str, *, production: bool = True) -> None:
        problems = self.problems(production=production)
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} が検出されませんでした: {problems}")

    def test_clean_build_has_no_problem(self):
        self.assertEqual(self.problems(), [])

    def test_site_hooks_are_called_with_the_data_dir(self):
        seen = {}

        def hook(**kwargs):
            seen.update(kwargs)
            return ["サイト専用の違反"], []

        with mock.patch.object(check_dist.site_hooks, "check_dist", side_effect=hook):
            problems, _ = check_dist.check_dist(self.out, production=True, now=NOW, data_dir=self.tmp / "work-data", cfg=self.cfg)
        self.assertIn("サイト専用の違反", problems)
        self.assertEqual(seen["data_dir"], self.tmp / "work-data")

    # ---- ページの中身 ----
    def test_inline_script(self):
        self.edit("index.html", "</main>", "<script>alert(1)</script></main>")
        self.assertCaught("本文つきの <script>")

    def test_external_script(self):
        self.edit("index.html", "</main>", '<script src="https://cdn.example.com/x.js"></script></main>')
        self.assertCaught("外部から読み込もうとしています")

    def test_protocol_relative_image(self):
        self.edit("index.html", "</main>", '<img src="//evil.example/x.png" alt=""></main>')
        self.assertCaught("外部から読み込もうとしています")

    def test_external_stylesheet(self):
        self.edit("index.html", "</head>", '<link rel="stylesheet" href="https://fonts.example.com/a.css"></head>')
        self.assertCaught("外部から読み込もうとしています")

    def test_event_attribute(self):
        self.edit("index.html", "<main>", '<main onclick="x()">')
        self.assertCaught("イベント属性 onclick")

    def test_style_attribute(self):
        self.edit("index.html", "<main>", '<main style="color:red">')
        self.assertCaught("属性 style")

    def test_style_element(self):
        self.edit("index.html", "</head>", "<style>p{}</style></head>")
        self.assertCaught("<style> は使えません")

    def test_form(self):
        self.edit("index.html", "</main>", "<form></form></main>")
        self.assertCaught("<form> は使えません")

    def test_password_input(self):
        self.edit("index.html", "</main>", '<input type="password"></main>')
        self.assertCaught("個人情報を入力させる欄")

    def test_iframe(self):
        self.edit("index.html", "</main>", '<iframe src="/x.html"></iframe></main>')
        self.assertCaught("<iframe> は使えません")

    def test_javascript_link_hidden_with_a_tab(self):
        self.edit("index.html", "</main>", '<a href="java&#x09;script:alert(1)">x</a></main>')
        self.assertCaught("許していないリンク先")

    def test_plain_http_link(self):
        self.edit("index.html", "</main>", '<a href="http://example.com/">x</a></main>')
        self.assertCaught("許していないリンク先")

    def test_external_link_without_noopener(self):
        self.edit("index.html", "</main>", '<a href="https://example.com/">x</a></main>')
        self.assertCaught("noopener")

    def test_meta_refresh(self):
        self.edit("index.html", "</head>", '<meta http-equiv="refresh" content="0;url=https://evil.example/"></head>')
        self.assertCaught("http-equiv=refresh")

    def test_missing_lang(self):
        self.edit("index.html", '<html lang="ja">', "<html>")
        self.assertCaught('lang="ja"')

    # ---- 「非公式」の帯 ----
    def test_missing_banner(self):
        self.edit("index.html", " data-unofficial-banner", "")
        self.assertCaught("帯")

    def test_banner_not_first_in_body(self):
        self.edit("index.html", "<body>", "<body><div>先に出す</div>")
        self.assertCaught("最初の要素")

    def test_banner_without_the_word(self):
        path = self.out / "index.html"
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(<header[^>]*data-unofficial-banner[^>]*>).*?(</header>)", r"\1<p>お知らせ</p>\2", text, flags=re.S)
        path.write_text(text, encoding="utf-8")
        self.assertCaught("「非公式」の文字がありません")

    # ---- CSP とヘッダ ----
    def test_meta_csp_differs_from_header(self):
        self.edit("index.html", "script-src &#x27;self&#x27;; ", "")
        self.assertCaught("食い違っています")

    def test_headers_without_csp(self):
        path = self.out / "_headers"
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if "Content-Security-Policy" not in ln]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertCaught("Content-Security-Policy がありません")

    def test_headers_with_unsafe_inline(self):
        self.edit("_headers", "script-src 'self'", "script-src 'self' 'unsafe-inline'")
        self.assertCaught("許していない値")

    def test_headers_with_external_host(self):
        self.edit("_headers", "script-src 'self'", "script-src 'self' https://cdn.example.com")
        self.assertCaught("許していない値")

    def test_headers_default_src_without_value(self):
        self.edit("_headers", "default-src 'none';", "default-src;")
        self.assertCaught("default-src は")

    def test_headers_without_frame_ancestors(self):
        self.edit("_headers", " frame-ancestors 'none';", "")
        self.assertCaught("frame-ancestors がありません")

    def test_headers_without_trusted_types(self):
        self.edit("_headers", " require-trusted-types-for 'script';", "")
        self.assertCaught("require-trusted-types-for がありません")

    def test_headers_short_hsts(self):
        self.edit("_headers", "max-age=63072000", "max-age=60")
        self.assertCaught("Strict-Transport-Security")

    def test_headers_frame_options_relaxed(self):
        self.edit("_headers", "X-Frame-Options: DENY", "X-Frame-Options: SAMEORIGIN")
        self.assertCaught("x-frame-options")

    def test_security_header_detached_on_a_path(self):
        with (self.out / "_headers").open("a", encoding="utf-8") as f:
            f.write("/data/*\n  ! Content-Security-Policy\n")
        self.assertCaught("外す指定")

    def test_security_header_overridden_on_a_path(self):
        with (self.out / "_headers").open("a", encoding="utf-8") as f:
            f.write("/data/*\n  X-Frame-Options: SAMEORIGIN\n")
        self.assertCaught("/* でだけ書いてください")

    # ---- 第3版: 審査役の指摘（2026-10-23）----
    def test_redirects_file_is_refused(self):
        (self.out / "_redirects").write_text("/* https://evil.example/:splat 302\n", encoding="utf-8")
        self.assertCaught("_redirects: 公開してよい種類のファイルではありません")

    def test_headers_vertical_tab_injection(self):
        self.edit("_headers", "frame-ancestors 'none';", "frame-ancestors 'none'\x0b  X: y; script-src-elem 'unsafe-inline';")
        self.assertCaught("制御文字")

    def test_headers_control_char_in_another_header(self):
        self.edit("_headers", "X-Frame-Options: DENY", "X-Frame-Options: DENY\x0b  Refresh: 0")
        self.assertCaught("制御文字か ASCII 以外の文字があります（Cloudflare と読み方がずれる）")

    def test_headers_line_longer_than_cloudflare_keeps(self):
        self.edit("_headers", "script-src 'self'", "script-src " + "'self' " * 400)
        self.assertCaught("字を超える行は Cloudflare に捨てられる")

    def test_headers_indented_path_line_steals_the_rest(self):
        self.edit("_headers", "  Strict-Transport-Security:", "  /x: y\n  Strict-Transport-Security:")
        self.assertCaught("strict-transport-security")

    def test_headers_unknown_header(self):
        with (self.out / "_headers").open("a", encoding="utf-8") as f:
            f.write("/old\n  Refresh: 0; url=https://evil.example/\n")
        self.assertCaught("refresh は書いてよいヘッダではありません")

    def test_hsts_x_max_age_does_not_count(self):
        self.edit("_headers", "max-age=63072000", "x-max-age=63072000")
        self.assertCaught("Strict-Transport-Security")

    def test_comment_hiding_a_script(self):
        self.edit("index.html", "</main>", "<!--> <script>alert(1)</script> --></main>")
        self.assertCaught("<!-- は使えません")

    def test_cdata_hiding_a_script(self):
        self.edit("index.html", "</main>", "<![CDATA[ > <script>alert(1)</script> ]]></main>")
        self.assertCaught("<![ は使えません")

    def test_script_end_tag_with_junk(self):
        self.edit("index.html", '<script src="/freshness.js" defer></script>', '<script src="/freshness.js" defer></script foo>')
        self.assertCaught("</script の後ろに余計な文字")

    def test_meta_csp_outside_head(self):
        path = self.out / "index.html"
        text = path.read_text(encoding="utf-8")
        start = text.index('<meta http-equiv="Content-Security-Policy"')
        end = text.index(">", start) + 1
        meta = text[start:end]
        path.write_text(text[:start] + text[end:].replace("<main>", "<main>" + meta, 1), encoding="utf-8")
        self.assertCaught("<meta> の CSP は <head> の中に")

    def test_resource_before_meta_csp(self):
        self.edit("index.html", '<meta charset="utf-8">', '<meta charset="utf-8">\n<link rel="stylesheet" href="/style.css">')
        self.assertCaught("CSP より前に読み込み")

    def test_page_without_body(self):
        path = self.out / "index.html"
        text = path.read_text(encoding="utf-8").replace("<body>", "").replace("</body>", "")
        path.write_text(text, encoding="utf-8")
        self.assertCaught("<body> がありません")

    def test_banner_with_hidden(self):
        self.edit("index.html", "data-unofficial-banner>", "data-unofficial-banner hidden>")
        self.assertCaught("帯に ['hidden'] は使えません")

    def test_word_only_inside_a_hidden_span(self):
        path = self.out / "index.html"
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(<header[^>]*data-unofficial-banner[^>]*>).*?(</header>)", r"\1<p>お知らせ<span hidden>非公式</span></p>\2", text, flags=re.S)
        path.write_text(text, encoding="utf-8")
        self.assertCaught("帯の中で ['hidden'] は使えません")

    def test_word_only_in_the_hidden_stale_area(self):
        path = self.out / "index.html"
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(<header[^>]*data-unofficial-banner[^>]*>).*?(</header>)", r"\1<p>お知らせ</p><p role=\"alert\" data-stale-banner hidden>非公式</p>\2", text, flags=re.S)
        path.write_text(text, encoding="utf-8")
        self.assertCaught("帯に「非公式」の文字がありません")

    def test_tag_count_mismatch_between_raw_text_and_parser(self):
        self.edit("index.html", "</main>", "<p title=\"x<script src='/freshness.js'></script>\">y</p></main>")
        self.assertCaught("読み方がずれている")

    def test_stale_banner_may_be_hidden(self):
        self.assertEqual(self.problems(), [])  # 雛形の帯には hidden の data-stale-banner が入っている

    def test_banner_as_closed_details(self):
        self.edit("index.html", "<header role=\"note\" data-unofficial-banner>", "<details role=\"note\" data-unofficial-banner>")
        self.edit("index.html", "</header>", "</details>")
        self.assertCaught("帯は")

    def test_body_hidden(self):
        self.edit("index.html", "<body>", "<body inert>")
        self.assertCaught("ページ全体を隠す属性")

    def test_css_hiding(self):
        with (self.out / "style.css").open("a", encoding="utf-8") as f:
            f.write("body > * { display: none }\n")
        self.assertCaught("見えなくする指定")

    def test_css_targeting_the_banner(self):
        with (self.out / "style.css").open("a", encoding="utf-8") as f:
            f.write("[data-unofficial-banner] { color: #fff1d6 }\n")
        self.assertCaught("帯を狙える選び方")

    def test_css_import_and_external_url(self):
        with (self.out / "style.css").open("a", encoding="utf-8") as f:
            f.write("@import url(https://evil.example/a.css);\nmain { background: url(https://evil.example/x.png) }\n")
        self.assertCaught("@import は使いません")
        self.assertCaught("CSS から外部を読もうとしています")

    def test_pinned_banner_css_changed(self):
        with (self.out / "banner.css").open("a", encoding="utf-8") as f:
            f.write("[data-unofficial-banner] { display: none }\n")
        self.assertCaught("banner.css の中身が固定した物と違います")

    def test_pinned_freshness_js_changed(self):
        with (self.out / "freshness.js").open("a", encoding="utf-8") as f:
            f.write("\n// 何もしない\n")
        self.assertCaught("freshness.js の中身が固定した物と違います")

    def test_page_without_banner_css(self):
        self.edit("index.html", '<link rel="stylesheet" href="/banner.css">\n', "")
        self.assertCaught("/banner.css を読み込んでいません")

    def test_js_navigation_and_banner_removal(self):
        (self.out / "app.js").write_text("document.querySelector('[data-unofficial-banner]').remove(); location.replace('https://evil.example/');", encoding="utf-8")
        self.assertCaught("帯を JS から触っています")
        self.assertCaught("別の場所へ移そう")

    def test_link_canonical_mixed_with_stylesheet(self):
        self.edit("index.html", "</head>", '<link rel="canonical stylesheet" href="https://evil.example/a.css"></head>')
        self.assertCaught("外部から読み込もうとしています")

    def test_imagesrcset(self):
        self.edit("index.html", "</head>", '<link rel="preload" as="image" href="/style.css" imagesrcset="https://evil.example/a.png 1x"></head>')
        self.assertCaught("imagesrcset")

    def test_inline_svg_and_template(self):
        self.edit("index.html", "</main>", "<svg><image href=\"https://evil.example/a.png\"></image></svg><template><p>x</p></template></main>")
        self.assertCaught("<svg> は使えません")
        self.assertCaught("<template> は使えません")

    def test_svg_namespaced_script_and_external_ref(self):
        (self.out / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:s="http://www.w3.org/2000/svg"><s:script>alert(1)</s:script></svg>', encoding="utf-8")
        (self.out / "b.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/x.png"/></svg>', encoding="utf-8")
        problems = self.problems()
        self.assertTrue(any(p.startswith("a.svg") for p in problems), problems)
        self.assertTrue(any(p.startswith("b.svg") for p in problems), problems)

    def test_xml_page_is_refused(self):
        (self.out / "page.xml").write_text('<html xmlns="http://www.w3.org/1999/xhtml"><body><script src="/x.js"/></body></html>', encoding="utf-8")
        self.assertCaught("page.xml: 公開してよい種類のファイルではありません")

    def test_symlink_in_dist(self):
        (self.out / "link.html").symlink_to(self.out / "index.html")
        self.assertCaught("シンボリックリンクは公開しません")

    def test_missing_headers_file(self):
        (self.out / "_headers").unlink()
        self.assertCaught("_headers がありません")

    # ---- ファイル ----
    def test_hidden_file(self):
        (self.out / ".env").write_text("TOKEN=x", encoding="utf-8")
        self.assertCaught("隠しファイル")

    def test_file_type_not_for_publishing(self):
        (self.out / "tool.py").write_text("print(1)", encoding="utf-8")
        self.assertCaught("公開してよい種類のファイルではありません")

    def test_placeholder_only_in_production(self):
        self.edit("about.html", "</main>", "未設定</main>")
        self.assertCaught("未設定の印")
        self.assertEqual(self.problems(production=False), [])

    def test_js_inner_html(self):
        (self.out / "app.js").write_text("el.innerHTML = x;", encoding="utf-8")
        self.assertCaught("HTML を文字列で差し込んでいます")

    def test_js_local_storage(self):
        (self.out / "app.js").write_text("localStorage.setItem('a', 1);", encoding="utf-8")
        self.assertCaught("端末に保存しています")

    def test_svg_with_script(self):
        (self.out / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
        self.assertCaught("SVG の中に")

    # ---- 重さと参照先 ----
    def test_page_too_heavy(self):
        self.edit("index.html", "</main>", "<p>" + "あ" * 110_000 + "</p></main>")
        self.assertCaught("1ページの重さ")

    def test_missing_resource(self):
        self.edit("index.html", "</main>", '<img src="/nothing.png" alt=""></main>')
        self.assertCaught("読み込む先のファイルがありません")

    def test_resource_pointing_outside_dist(self):
        self.edit("index.html", "</main>", '<img src="/../../etc/hosts" alt=""></main>')
        self.assertCaught("読み込む先のファイルがありません")

    def test_big_image(self):
        (self.out / "big.png").write_bytes(b"\x89PNG" + b"\x00" * 250_000)
        self.assertCaught("大きな画像は置きません")

    # ---- status.json ----
    def test_status_without_freshness_limit(self):
        path = self.out / "status.json"
        obj = json.loads(path.read_text(encoding="utf-8"))
        del obj["max_status_age_minutes"]
        path.write_text(json.dumps(obj), encoding="utf-8")
        self.assertCaught("max_status_age_minutes")

    def test_missing_status(self):
        (self.out / "status.json").unlink()
        self.assertCaught("status.json がありません")

    def test_stale_status_in_production(self):
        path = self.out / "status.json"
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["generated_at"] = "2026-10-23T03:00:00Z"
        path.write_text(json.dumps(obj), encoding="utf-8")
        self.assertCaught("generated_at が今と合いません")

    def test_status_with_a_fourth_state(self):
        path = self.out / "status.json"
        path.write_text(json.dumps({"generated_at": "2026-10-23T06:00:00Z", "sources": {"s1": {"state": "none"}}}), encoding="utf-8")
        self.assertCaught("三状態のどれでもありません")

    # ---- 終了コード（違反=1・読めなかった=2） ----
    def test_exit_code_is_1_for_problems(self):
        self.edit("index.html", "</main>", "<script>alert(1)</script></main>")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), mock.patch.object(check_dist.common, "ROOT", self.root):
            self.assertEqual(check_dist.main([str(self.out)]), 1)

    def test_exit_code_is_2_when_a_file_cannot_be_read(self):
        (self.out / "broken.html").write_bytes(b"\xff\xfe\xfa")
        problems, unreadable = check_dist.check_dist(self.out, production=False, now=NOW, cfg=self.cfg)
        self.assertEqual(problems, [])
        self.assertTrue(unreadable)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), mock.patch.object(check_dist.common, "ROOT", self.root):
            self.assertEqual(check_dist.main([str(self.out)]), 2)

    def test_exit_code_is_0_when_clean(self):
        with redirect_stdout(io.StringIO()), mock.patch.object(check_dist.common, "ROOT", self.root):
            self.assertEqual(check_dist.main([str(self.out)]), 0)


if __name__ == "__main__":
    unittest.main()
