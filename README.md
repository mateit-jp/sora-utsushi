# （サイトの名前）

**非公式**のサイトです。止まっているサービスの事業者とは関係がありません。
最新で正しい情報は、必ず公式の発表で確かめてください。

## しくみ

- 静的なページだけです（ログイン・入力欄・Cookie・広告・解析タグはありません）。
- データは `site/sources.json` に書いた出典から、利用条件に従って取り込みます（公開ページの「出典と利用条件」）。
- 取り込めなかったときは、前回の内容を「前回の内容」と明記して表示します。「0件」とは表示しません。
- 更新が止まったときは、ページの上に「更新が止まっています」と表示します。
- 配信は Cloudflare の静的アセット、生成と検査は GitHub Actions です。

## 開発（どれも Python の標準ライブラリだけ）

```sh
python3 -I -m unittest discover -s tests        # 試験
python3 -I tests/mutation_check.py              # 検算（関所を壊すと試験が赤になるか）
python3 -I tools/check_repo.py                  # 設定ファイルの掟
python3 -I tools/build.py --out dist && python3 -I tools/check_dist.py dist
node --test 'tests_js/*.test.mjs'                # JS の試験（Node の標準の試験だけ）
```

掟は `CLAUDE.md`、店主の手順と止まったときの見方は `docs/運用.md`。

## 連絡

- 内容の誤り: サイトの「訂正のご連絡」から
- セキュリティの問題: `SECURITY.md`（公開の Issue には書かないでください）
