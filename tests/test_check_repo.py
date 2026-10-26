from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

import check_repo  # noqa: E402  (helpers が tools/ を sys.path に入れる)

# wrangler.jsonc の1行目の項目。name の値（手順3 で入れる）に依らない差し込み口
SCHEMA = '"$schema": "./node_modules/wrangler/config-schema.json",'
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"


class CheckRepoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        shutil.copytree(helpers.ROOT, self.root, ignore=shutil.ignore_patterns(".git", "dist", "data", "node_modules", "__pycache__"))

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self, *, production: bool = False) -> list[str]:
        problems, unreadable = check_repo.check_repo(self.root, production=production)
        self.assertEqual(unreadable, [])
        return problems

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def assertCaught(self, needle: str, *, production: bool = False) -> None:
        problems = self.problems(production=production)
        self.assertTrue(any(needle in p for p in problems), f"{needle!r} が検出されませんでした: {problems}")

    def test_template_is_clean(self):
        self.assertEqual(self.problems(), [])

    def test_site_hooks_are_called(self):
        with mock.patch.object(check_repo.site_hooks, "check_repo", return_value=(["サイト専用の違反"], [])):
            self.assertIn("サイト専用の違反", self.problems())

    def put_placeholders_back(self) -> None:
        wrangler = self.root / "wrangler.jsonc"
        text = wrangler.read_text(encoding="utf-8")
        text, n = re.subn(r'^  "name": "[^"]*",$', '  "name": "%s",' % check_repo.PLACEHOLDER_WORKER, text, count=1, flags=re.M)
        self.assertEqual(n, 1, "wrangler.jsonc の name の行が見つかりません")
        wrangler.write_text(text, encoding="utf-8")
        owners = self.root / ".github" / "CODEOWNERS"
        text = owners.read_text(encoding="utf-8")
        text, n = re.subn(r"^\* @\S+$", "* " + check_repo.PLACEHOLDER_OWNER, text, count=1, flags=re.M)
        self.assertEqual(n, 1, "CODEOWNERS の * の行が見つかりません")
        owners.write_text(text, encoding="utf-8")

    def test_template_placeholders_block_production(self):
        self.put_placeholders_back()
        self.assertCaught("safe-site-change-me", production=True)
        self.assertCaught("@OWNER", production=True)

    def test_unpinned_action(self):
        self.edit(".github/workflows/ci.yml", CHECKOUT, "actions/checkout@v7")
        self.assertCaught("uses は")

    def test_pinned_without_version_comment(self):
        self.edit(".github/workflows/ci.yml", CHECKOUT, CHECKOUT.split(" #")[0])
        self.assertCaught("uses は")

    def test_docker_without_digest(self):
        self.edit(".github/workflows/ci.yml", "      - name: 試験\n", "      - uses: docker://alpine:3\n      - name: 試験\n")
        self.assertCaught("uses は")

    def test_pull_request_target(self):
        self.edit(".github/workflows/ci.yml", "  pull_request:\n", "  pull_request_target:\n")
        self.assertCaught("pull_request_target")

    def test_expression_inside_run(self):
        self.edit(".github/workflows/ci.yml", "run: python3 -I -m unittest discover -s tests -v", "run: echo ${{ github.event.pull_request.title }}")
        self.assertCaught("run: の中に")

    def test_expression_inside_a_run_block(self):
        self.edit(".github/workflows/ci.yml", "          python3 -I tools/check_dist.py dist\n", "          echo ${{ github.head_ref }}\n")
        self.assertCaught("run: の中に")

    def test_checkout_without_persist_credentials_false(self):
        self.edit(".github/workflows/ci.yml", "          persist-credentials: false\n", "")
        self.assertCaught("persist-credentials: false")

    def test_missing_top_level_permissions(self):
        self.edit(".github/workflows/deploy.yml", "\npermissions: {}\n", "\n")
        self.assertCaught("permissions: {}")

    def test_secrets_outside_deploy(self):
        self.edit(".github/workflows/ci.yml", "      - name: 試験\n", "      - name: 試験\n        env:\n          T: ${{ secrets.X }}\n")
        self.assertCaught("秘密は deploy.yml の deploy ジョブの env")

    def test_cache(self):
        self.edit(".github/workflows/ci.yml", '          python-version: "3.13"\n', '          python-version: "3.13"\n          cache: pip\n')
        self.assertCaught("キャッシュは使いません")

    def test_wrangler_preview_urls(self):
        self.edit("wrangler.jsonc", '"preview_urls": false', '"preview_urls": true')
        self.assertCaught("preview_urls")

    def test_wrangler_assets_directory(self):
        self.edit("wrangler.jsonc", '"directory": "./dist"', '"directory": "./site"')
        self.assertCaught("assets.directory")

    def test_wrangler_worker_code_needs_api_only_routes(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "worker.js").write_text("export default {};\n", encoding="utf-8")
        self.edit("wrangler.jsonc", SCHEMA, SCHEMA + '\n  "main": "src/worker.js",')
        self.assertCaught("run_worker_first")

    def test_wrangler_main_file_must_exist(self):
        self.edit("wrangler.jsonc", SCHEMA, SCHEMA + '\n  "main": "src/worker.js",')
        self.assertCaught("src/worker.js がありません")

    def test_worker_code_with_placeholder_blocks_production(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "worker.js").write_text('export const UPSTREAM = "https://api.example.invalid/v1/items";\n', encoding="utf-8")
        self.edit(
            "wrangler.jsonc",
            '"html_handling": "auto-trailing-slash"',
            '"html_handling": "auto-trailing-slash",\n    "binding": "ASSETS",\n    "run_worker_first": ["/api/*"]',
        )
        self.edit("wrangler.jsonc", SCHEMA, SCHEMA + '\n  "main": "src/worker.js",')
        self.assertEqual(self.problems(), [])
        self.assertCaught("src/worker.js: 未設定の印", production=True)

    def test_package_caret_version(self):
        # 版は Dependabot が上げるので、文字列を固定せずに今の版を package.json から読む
        ver = json.loads((self.root / "package.json").read_text(encoding="utf-8"))["devDependencies"]["wrangler"]
        self.edit("package.json", f'"wrangler": "{ver}"', f'"wrangler": "^{ver}"')
        self.assertCaught("1.2.3 の形で固定")

    def test_package_install_hook(self):
        self.edit("package.json", '"private": true,', '"private": true,\n  "scripts": {"postinstall": "node x.js"},')
        self.assertCaught("scripts")

    def test_dependabot_without_cooldown(self):
        # 1つの更新だけ cooldown の日数を外す（注記の「cooldown」の語には当たらないこと）
        self.edit(".github/dependabot.yml", "      default-days: 7\n", "")
        self.assertCaught("cooldown")

    def test_dependabot_cooldown_zero(self):
        self.edit(".github/dependabot.yml", "      default-days: 7\n", "      default-days: 0\n")
        self.assertCaught("cooldown")

    # ---- 第3版: 審査役の指摘（YAML の書き方の揺れ）----
    def test_flow_mapping_uses(self):
        self.edit(".github/workflows/ci.yml", "      - name: 試験\n", "      - {uses: actions/checkout@main}\n      - name: 試験\n")
        self.assertCaught("フロー形式")
        self.assertCaught("uses は")

    def test_quoted_and_spaced_uses_keys(self):
        for variant in ('      - "uses": actions/checkout@main\n', "      - uses : actions/checkout@main\n", "      - uses:\n          actions/checkout@main\n"):
            with self.subTest(variant=variant):
                self.setUp()
                self.edit(".github/workflows/ci.yml", "      - name: 試験\n", variant + "      - name: 試験\n")
                self.assertCaught("uses は")

    def test_expression_in_github_script(self):
        self.edit(".github/workflows/ci.yml", "      - name: 試験\n", "      - name: x\n        with:\n          script: console.log(\"${{ github.event.issue.title }}\")\n      - name: 試験\n")
        self.assertCaught("許していない式")

    def test_uppercase_secrets_outside_deploy(self):
        self.edit(".github/workflows/ci.yml", "      - name: 試験\n", "      - name: 試験\n        env:\n          T: ${{ SECRETS.X }}\n")
        self.assertCaught("秘密は deploy.yml の deploy ジョブの env")

    def test_secret_in_the_build_job_of_deploy(self):
        self.edit(".github/workflows/deploy.yml", "      - name: 公開用に生成する（未設定が残っていれば止まる）\n",
                  "      - name: 公開用に生成する（未設定が残っていれば止まる）\n        env:\n          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n")
        self.assertCaught("秘密は deploy.yml の deploy ジョブの env")

    def test_write_permission(self):
        self.edit(".github/workflows/ci.yml", "      contents: read\n", "      contents: write\n")
        self.assertCaught("書き込みの権限")

    def test_padding_persist_credentials_does_not_help(self):
        text = (self.root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        text = text.replace("        with:\n          persist-credentials: false\n", "", 1)
        text = text.replace("      - name: 試験\n", "      - name: 試験\n        with:\n          persist-credentials: false\n", 1)
        (self.root / ".github/workflows/ci.yml").write_text(text, encoding="utf-8")
        self.assertCaught("その手順の中で persist-credentials: false")

    def test_local_composite_actions_dir(self):
        (self.root / ".github" / "actions" / "x").mkdir(parents=True)
        (self.root / ".github" / "actions" / "x" / "action.yml").write_text("runs: {using: composite, steps: []}\n", encoding="utf-8")
        self.assertCaught(".github/actions は置きません")

    def test_self_hosted_any_case(self):
        self.edit(".github/workflows/ci.yml", "    runs-on: ubuntu-24.04\n", "    runs-on: Self-Hosted\n")
        self.assertCaught("self-hosted")

    def test_package_manager_key(self):
        self.edit("package.json", '"private": true,', '"private": true,\n  "packageManager": "npm@11.0.0",')
        self.assertCaught("packageManager")

    def test_wrangler_json_takes_precedence(self):
        (self.root / "wrangler.json").write_text('{"name": "x", "assets": {"directory": "./site"}}', encoding="utf-8")
        self.assertCaught("wrangler.json は置きません")

    def test_wrangler_build_command(self):
        self.edit("wrangler.jsonc", SCHEMA, SCHEMA + '\n  "build": {"command": "env"},')
        self.assertCaught("許していない項目")

    def test_npmrc(self):
        (self.root / ".npmrc").write_text("registry=https://evil.example/\n", encoding="utf-8")
        self.assertCaught(".npmrc は置きません")

    def test_lockfile_resolved_outside_the_registry(self):
        path = self.root / "package-lock.json"
        text = path.read_text(encoding="utf-8")
        self.assertIn('"resolved": "https://registry.npmjs.org/', text)
        path.write_text(text.replace('"resolved": "https://registry.npmjs.org/', '"resolved": "https://evil.example/', 1), encoding="utf-8")
        self.assertCaught("npm の公式の置き場ではありません")

    def test_generated_dirs_tracked_by_git(self):
        import subprocess
        (self.root / "dist").mkdir()
        (self.root / "dist" / "x.html").write_text("x", encoding="utf-8")
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(self.root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=env)
        self.assertEqual(self.problems(), [])  # 手元に作っただけ（git に入れていない）なら見逃す
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "dist/x.html"], check=True, env=env)
        self.assertCaught("生成物が git に入っています")

    def test_editor_config_dirs_are_refused(self):
        for name in (".claude", ".vscode", ".devcontainer"):
            (self.root / name).mkdir()
            (self.root / name / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
            self.assertCaught(f"{name} は置きません")
            (self.root / name / "settings.json").unlink()
            (self.root / name).rmdir()

    def test_jsonc_comment_stripping_keeps_urls_in_strings(self):
        text = '{\n  // 注釈\n  "a": "https://example.com/x", /* 注釈 */ "b": 1\n}'
        self.assertEqual(check_repo.strip_jsonc(text).replace(" ", "").replace("\n", ""), '{"a":"https://example.com/x","b":1}')


if __name__ == "__main__":
    unittest.main()
