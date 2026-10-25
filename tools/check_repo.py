#!/usr/bin/env python3
"""リポジトリの設定ファイル（workflow・wrangler.jsonc・package.json・dependabot・CODEOWNERS）の掟を見張る。

ネットを使わないので、手元でも試験の中でも走る。YAML の意味まで読むのは zizmor（CI の必須の検査）と、
GitHub の「SHA 固定を必須にする」設定の役目。ここは YAML を解釈せず、**書き方を決まった形に絞る**ことで、
書き方の揺れ（フロー形式・引用符・次の行に値・大文字小文字）ですり抜けられないようにする:
- `${{ }}` は、許した式（ALLOWED_EXPRESSIONS）以外を1つも書かせない（run・github-script・if のどこでも）
- `uses` という語は、決まった形の行（`uses: owner/repo@<40桁> # vX.Y.Z`）にしか書かせない
- `secrets` という語（大文字小文字を問わず）は、deploy.yml の deploy ジョブの中の決まった1か所にしか書かせない

★この見張りが止めるのは「うっかり」。見張りそのものを書き換える PR は止められない（承認0の ruleset では
  PR が tools/ も .github/ も書き換えられる）。tools/・.github/ を変える PR は、店主が差分を見てから merge する。

使い方:
  python3 -I tools/check_repo.py               # 形の検査
  python3 -I tools/check_repo.py --production  # ＋未設定（safe-site-change-me・@OWNER）が残っていないか
終了コード: 0=問題なし / 1=違反あり / 2=読めなかった
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import site_hooks  # noqa: E402

PINNED_USES_RE = re.compile(r"^\s*(-\s+)?uses: ([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}) # v\d+(\.\d+){0,2}\s*$")
DOCKER_USES_RE = re.compile(r"^\s*(-\s+)?uses: docker://\S+@sha256:[0-9a-f]{64}( #.*)?$")
LOCAL_USES_RE = re.compile(r"^\s*(-\s+)?uses: \./\S+\s*$")
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# 書いてよい式（空白を除いて比べる）。足すときは、その式が外から操れないことを確かめてから
ALLOWED_EXPRESSIONS = {"github.ref", "secrets.CLOUDFLARE_API_TOKEN", "vars.CLOUDFLARE_ACCOUNT_ID"}
SECRET_EXPRESSION = "${{ secrets.CLOUDFLARE_API_TOKEN }}"
SECRETS_WORD_RE = re.compile(r"secrets", re.I)
TOP_PERMISSIONS_RE = re.compile(r"^permissions: \{\}$", re.M)
RUN_RE = re.compile(r"^(\s*)(-\s+)?run:\s*(.*)$")
WORKFLOW_FORBIDDEN = [
    (re.compile(r"pull_request_target", re.I), "pull_request_target は使いません（fork のコードが秘密つきで動く）"),
    (re.compile(r"workflow_run", re.I), "workflow_run は使いません"),
    (re.compile(r"write-all|read-all", re.I), "permissions を一括で与えません"),
    (re.compile(r":\s*write\b", re.I), "書き込みの権限は与えません（このリポジトリの workflow は読むだけで足りる）"),
    (re.compile(r"self-hosted", re.I), "self-hosted ランナーは使いません（公開リポジトリ）"),
    (re.compile(r"actions/cache|(?<![\w-])cache\s*:", re.I), "キャッシュは使いません（毒入れの経路）"),
    (re.compile(r"package-manager-cache:\s*true", re.I), "キャッシュは使いません（毒入れの経路）"),
]
# フロー形式（{ } や [ ]）は使わない（書き方の揺れで見張りをすり抜けられる）。式 ${{ }} と「permissions: {}」の行は除いて探す
FLOW_RE = re.compile(r"[{}\[\]]")
WORKER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGE_KEYS = {"name", "private", "description", "devDependencies", "type"}
WRANGLER_KEYS = {"$schema", "name", "compatibility_date", "assets", "workers_dev", "preview_urls", "main", "observability"}
ASSETS_KEYS = {"directory", "not_found_handling", "html_handling", "binding", "run_worker_first"}
REGISTRY = "https://registry.npmjs.org/"
# 注記や別の語の一部に当たらないよう、YAML の「キーの行」として探す（素の部分一致は注意書きに当たる）
ECOSYSTEM_RE = re.compile(r'^\s*-\s+package-ecosystem:\s*"([^"]+)"\s*$', re.M)
COOLDOWN_DAYS_RE = re.compile(r"^\s+default-days:\s*(\d+)\s*$", re.M)
COOLDOWN_MIN_DAYS = 3
# 開いただけ・セッションを始めただけでコマンドが動く設定の置き場（2026-08 の Keyv 起点の攻撃が仕込んだ型）と、
# 取得元を差し替えられる設定。承認0の ruleset では CODEOWNERS が強制されないので、置いたら機械で落とす
FORBIDDEN_PATHS = (".claude", ".vscode", ".devcontainer", ".github/actions", ".npmrc", ".yarnrc", ".yarnrc.yml", "wrangler.json", "wrangler.toml")
# git に入れてはいけない生成物（`git add -f` で入れると、deploy で検査していない物が混ざりうる）
GENERATED_DIRS = ("dist", "data")
PLACEHOLDER_WORKER = "safe-site-change-me"
PLACEHOLDER_OWNER = "@OWNER"


def code_part(line: str) -> str:
    """YAML の1行から注記（引用符の外の「 #」以降）を除く。"""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def strip_jsonc(text: str) -> str:
    """JSONC の // と /* */ の注釈を除く（文字列の中の // は残す）。"""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def job_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    """jobs: の下の、2文字下げのジョブ名ごとの行の範囲（フロー形式を禁じているので字下げで読める）。"""
    ranges: dict[str, tuple[int, int]] = {}
    in_jobs = False
    current = None
    start = 0
    for i, line in enumerate(lines):
        code = code_part(line).rstrip()
        if not code:
            continue
        if not code.startswith(" "):
            if current:
                ranges[current] = (start, i)
                current = None
            in_jobs = code == "jobs:"
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):$", code)
        if in_jobs and m:
            if current:
                ranges[current] = (start, i)
            current, start = m.group(1), i
    if current:
        ranges[current] = (start, len(lines))
    return ranges


def check_workflow(name: str, text: str) -> list[str]:
    problems = []
    where = f".github/workflows/{name}"
    lines = text.splitlines()
    codes = [code_part(line) for line in lines]
    code_text = "\n".join(codes)
    if not TOP_PERMISSIONS_RE.search(code_text):
        problems.append(f"{where}: 一番上に permissions: {{}} がありません（ジョブごとに最小の権限を与える）")
    for pattern, why in WORKFLOW_FORBIDDEN:
        if pattern.search(code_text):
            problems.append(f"{where}: {why}")
    flow_text = "\n".join("" if c.strip() == "permissions: {}" else EXPR_RE.sub("", c) for c in codes)
    if FLOW_RE.search(flow_text):
        problems.append(f"{where}: フロー形式（{{ }} や [ ]）は使いません（書き方の揺れで見張りをすり抜けられる）。並びは - で書く")
    # 式は許したものだけ（run・github-script・if・with のどこでも）
    for m in EXPR_RE.finditer(code_text):
        expr = "".join(m.group(1).split())
        if expr not in ALLOWED_EXPRESSIONS:
            problems.append(f"{where}: 許していない式 ${{{{ {m.group(1).strip()} }}}} があります（外から操れる値を run 等に入れない）")
    # secrets という語は、deploy.yml の deploy ジョブの中の決まった1か所だけ
    ranges = job_ranges(lines)
    for i, code in enumerate(codes):
        if not SECRETS_WORD_RE.search(code):
            continue
        in_deploy_job = name == "deploy.yml" and "deploy" in ranges and ranges["deploy"][0] <= i < ranges["deploy"][1]
        if not (in_deploy_job and code.strip() == f"CLOUDFLARE_API_TOKEN: {SECRET_EXPRESSION}"):
            problems.append(f"{where}:{i + 1}: 秘密は deploy.yml の deploy ジョブの env の決まった1行でだけ使います")
    # uses という語は、決まった形の行にしか書かない
    checkout_lines = []
    for i, (line, code) in enumerate(zip(lines, codes)):
        if not re.search(r"(?<![\w-])uses(?![\w-])", code):
            continue
        if LOCAL_USES_RE.match(line) or DOCKER_USES_RE.match(line):
            continue
        m = PINNED_USES_RE.match(line)
        if not m:
            problems.append(f"{where}:{i + 1}: uses は「uses: owner/repo@<40桁の commit SHA> # vX.Y.Z」の形の1行で書いてください")
        elif m.group(2).startswith("actions/checkout@"):
            checkout_lines.append(i)
    # checkout には毎回、その手順の中に persist-credentials: false
    for i in checkout_lines:
        indent = len(lines[i]) - len(lines[i].lstrip(" -"))
        found = False
        for j in range(i + 1, len(lines)):
            code = codes[j]
            if code.strip() and (len(code) - len(code.lstrip(" ")) < indent or code.lstrip().startswith("- ")):
                break
            if code.strip() == "persist-credentials: false":
                found = True
                break
        if not found:
            problems.append(f"{where}:{i + 1}: actions/checkout には、その手順の中で persist-credentials: false を付けてください")
    # run: の中の ${{ }}（上の許可の式でも run の中には入れない）
    i = 0
    while i < len(lines):
        m = RUN_RE.match(codes[i])
        if not m:
            i += 1
            continue
        key_col = len(m.group(1)) + len(m.group(2) or "")
        body = [(i + 1, m.group(3))]
        if m.group(3).strip()[:1] in ("|", ">"):
            body = []
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > key_col):
                body.append((j + 1, lines[j]))
                j += 1
            i = j
        else:
            i += 1
        for lineno, content in body:
            if "${{" in content:
                problems.append(f"{where}:{lineno}: run: の中に ${{{{ }}}} を直接書かない（env: で渡す）")
    return problems


def check_wrangler(text: str, production: bool) -> list[str]:
    try:
        cfg = json.loads(strip_jsonc(text))
    except ValueError as e:
        return [f"wrangler.jsonc を読めませんでした（{e}）"]
    if not isinstance(cfg, dict):
        return ["wrangler.jsonc の一番外がオブジェクトではありません"]
    problems = []
    unknown = set(cfg) - WRANGLER_KEYS
    if unknown:
        problems.append(f"wrangler.jsonc: 許していない項目があります: {sorted(unknown)}（build などはデプロイの時に秘密つきで動く）")
    name = cfg.get("name")
    if not isinstance(name, str) or not WORKER_NAME_RE.match(name):
        problems.append("wrangler.jsonc: name は英小文字・数字・- で63文字以内にしてください")
    elif production and name == PLACEHOLDER_WORKER:
        problems.append(f"wrangler.jsonc: name が未設定（{PLACEHOLDER_WORKER}）のままです")
    assets = cfg.get("assets") if isinstance(cfg.get("assets"), dict) else {}
    if set(assets) - ASSETS_KEYS:
        problems.append(f"wrangler.jsonc: assets に許していない項目があります: {sorted(set(assets) - ASSETS_KEYS)}")
    if assets.get("directory") != "./dist":
        problems.append('wrangler.jsonc: assets.directory は "./dist" にしてください（検査した物だけを配る）')
    if cfg.get("preview_urls") is not False:
        problems.append("wrangler.jsonc: preview_urls を false にしてください（検査していない版を公開しない）")
    if "main" in cfg:
        first = assets.get("run_worker_first")
        if not isinstance(first, list) or not first or not all(isinstance(p, str) and p.startswith("/api/") for p in first):
            problems.append('wrangler.jsonc: Worker のコードを置くなら assets.run_worker_first を "/api/..." だけにしてください（型B）')
    return problems


def check_package(text: str) -> list[str]:
    try:
        pkg = json.loads(text)
    except ValueError as e:
        return [f"package.json を読めませんでした（{e}）"]
    problems = []
    unknown = set(pkg) - PACKAGE_KEYS
    if unknown:
        problems.append(f"package.json: 許していない項目があります: {sorted(unknown)}（scripts・packageManager などは入れない）")
    dev = pkg.get("devDependencies") or {}
    if set(dev) != {"wrangler"}:
        problems.append(f"package.json: devDependencies は wrangler だけにしてください（今: {sorted(dev)}）")
    for dep, ver in dev.items():
        if not isinstance(ver, str) or not EXACT_VERSION_RE.match(ver):
            problems.append(f"package.json: {dep} の版は 1.2.3 の形で固定してください（今: {ver!r}）")
    return problems


def check_lockfile(text: str) -> list[str]:
    try:
        lock = json.loads(text)
    except ValueError as e:
        return [f"package-lock.json を読めませんでした（{e}）"]
    problems = []
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        return ["package-lock.json に packages がありません（lockfileVersion 3 で作る）"]
    for key, entry in packages.items():
        if not key or not isinstance(entry, dict):
            continue
        resolved = entry.get("resolved")
        if entry.get("link"):
            problems.append(f"package-lock.json: {key} が手元のフォルダへのリンクです")
        elif resolved is None:
            problems.append(f"package-lock.json: {key} に resolved がありません")
        elif not resolved.startswith(REGISTRY):
            problems.append(f"package-lock.json: {key} の取得元が npm の公式の置き場ではありません: {resolved}")
        elif not str(entry.get("integrity", "")).startswith("sha512-"):
            problems.append(f"package-lock.json: {key} に sha512 の integrity がありません")
    return problems


def check_repo(root: Path, *, production: bool) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    unreadable: list[str] = []

    def read(rel: str) -> str | None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            unreadable.append(f"{rel}（{type(e).__name__}）")
            return None

    for rel in FORBIDDEN_PATHS:
        path = root / rel
        if path.exists() or path.is_symlink():
            problems.append(f"{rel} は置きません（開いただけで動く設定・取得元や設定の差し替え）")
    if (root / ".git").exists():
        try:
            tracked = subprocess.run(
                ["git", "-C", str(root), "ls-files", "--", *GENERATED_DIRS],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError) as e:
            unreadable.append(f"git ls-files（{type(e).__name__}）")
        else:
            if tracked:
                problems.append(f"生成物が git に入っています（{tracked[:3]}…）。dist/・data/ は git に入れない")
    workflows = sorted((root / ".github" / "workflows").glob("*"))
    if not workflows:
        problems.append(".github/workflows に workflow がありません")
    for path in workflows:
        if path.suffix not in (".yml", ".yaml"):
            problems.append(f".github/workflows/{path.name}: workflow 以外のファイルは置きません")
            continue
        text = read(path.relative_to(root).as_posix())
        if text is not None:
            problems += check_workflow(path.name, text)
    all_workflows = "\n".join(
        code_part(line) for path in workflows if path.suffix in (".yml", ".yaml") for line in (read(path.relative_to(root).as_posix()) or "").splitlines()
    )
    if all_workflows.count(SECRET_EXPRESSION) > 1:
        problems.append("workflow: CLOUDFLARE_API_TOKEN を2か所以上で使っています（deploy ジョブの1か所だけ）")
    text = read("wrangler.jsonc")
    if text is not None:
        problems += check_wrangler(text, production)
        try:
            main_rel = json.loads(strip_jsonc(text)).get("main")
        except (ValueError, AttributeError):
            main_rel = None
        if isinstance(main_rel, str) and not (root / main_rel).is_file():
            problems.append(f"wrangler.jsonc: main に書いた {main_rel} がありません")
        elif isinstance(main_rel, str):
            main_text = read(main_rel)  # 型B: Worker のコードにも未設定を残さない
            if main_text is not None and production:
                marks = [m for m in common.PLACEHOLDER_MARKS if m in main_text]
                if marks:
                    problems.append(f"{main_rel}: 未設定の印が残っています: {marks}")
    text = read("package.json")
    if text is not None:
        problems += check_package(text)
    if not (root / "package-lock.json").is_file():
        problems.append("package-lock.json がありません（npm ci で版と中身を固定する）")
    else:
        text = read("package-lock.json")
        if text is not None:
            problems += check_lockfile(text)
    text = read(".github/dependabot.yml")
    if text is not None:
        ecosystems = ECOSYSTEM_RE.findall(text)
        for ecosystem in ("github-actions", "npm"):
            if ecosystem not in ecosystems:
                problems.append(f".github/dependabot.yml: {ecosystem} の更新がありません")
        days = [int(d) for d in COOLDOWN_DAYS_RE.findall(text)]
        if len(days) < len(ecosystems) or any(d < COOLDOWN_MIN_DAYS for d in days):
            problems.append(f".github/dependabot.yml: cooldown（出たばかりの版を待つ日数・{COOLDOWN_MIN_DAYS}日以上）の無い更新があります")
    text = read(".github/CODEOWNERS")
    if text is not None and production and PLACEHOLDER_OWNER in text:
        problems.append(f".github/CODEOWNERS: {PLACEHOLDER_OWNER} が未設定のままです")
    hook_problems, hook_unreadable = site_hooks.check_repo(root=root, production=production)
    return problems + hook_problems, unreadable + hook_unreadable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="リポジトリの設定ファイルの掟を見張る")
    ap.add_argument("--production", action="store_true", help="未設定の値と、手元の生成物の持ち込みも見る")
    args = ap.parse_args(argv)
    problems, unreadable = check_repo(common.ROOT, production=args.production)
    for p in problems:
        print(f"違反: {p}")
    for u in unreadable:
        print(f"読めませんでした: {u}")
    if problems:
        return 1
    if unreadable:
        return 2
    print("問題ありませんでした")
    return 0


if __name__ == "__main__":
    sys.exit(main())
