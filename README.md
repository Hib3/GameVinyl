# Game Record Tool

ゲーム音楽レコードのアーカイブ／通知システムです。Windows の PC 上で常駐して動きます。

## 三本柱

- **発見** — 登録済みAPI/RSSと、許可条件を確認した情報源を定期的に確認します。全市場・全3,777盤の出品網羅は保証しません。
- **カタログ** — 発売されたレコードを、在庫の有無にかかわらずカタログとして積み上げていきます。同梱のシードデータは 3,777 件です。
- **通知** — 見つけた情報を Telegram の Bot 経由で、あなたのスマホ／PC に届けます。通知の範囲は Telegram からいつでも変更できます。

## 設計の約束

- **AI 非内蔵** — このツール自体の中に AI（LLM、画像認識など）は入っていません。判定は決められたルール（パターンマッチと正規化）で行い、迷うものは Telegram の確認キューで人が判断します。AI はこのツールを作るための道具であり、成果物ではありません。
- **完全無料で運用可能** — 使うライブラリは無料の公開パッケージのみ、通知は Telegram（無料）、レコード情報の照合は Discogs の無料トークンを使います。有料 API は使いません。

自動巡回はDiscogsの監視対象盤統計API、認証設定済みの場合のYahoo!ショッピング／楽天市場API、提供元RSS/Atom、公開ページの許可された限定取得に絞ります。RSSは新着告知であり、出品の在庫・価格の証拠とは区別します。サイト鮮度ジョブはBlip Blop RSS、Data Discsの固定商品JSONとrobots.txt確認済み一覧ページ、MusicBrainz APIを使います。Data Discs一覧とYahoo!オークションの公開検索結果はHTML解析を含みます。Yahoo!オークションは60秒以上の間隔、検索36件/時（優先盤最大12件）、個別ページ24件/時の上限で巡回します。曖昧な候補は人が確認します。Mercariなど許可された取得経路がない市場へログイン巡回はせず、手動検索リンクを表示します。Yahoo!ショッピング／楽天市場APIの資格がない場合も、各市場とeBay／Discogsの検索リンクを表示します。検索リンクは商品や在庫の存在を示しません。MusicBrainzに版候補がある場合はCover Art Archiveの画像候補を開けますが、版一致・画像利用権は未確認です。eBay Browse APIは自動取得を停止中で、保存済みeBay行も集計・通知から除外します。

## フォルダ構成

```text
game-record-tool/
├── setup.bat          セットアップ用スクリプト（最初に1回実行）
├── check.bat          診断ツールの起動（困ったときにダブルクリック）
├── diagnose.py        診断ツール本体（check.bat から呼ばれる）
├── SETUP_GUIDE.md     セットアップ手順書（まずここを読む）
├── .env.template      設定ファイルのひな形
├── requirements.txt   必要な Python パッケージの一覧
├── README.md          このファイル
├── run.py             本体の起動（自動起動から呼ばれる）
├── watchdog.py        ウォッチドッグ（本体が止まっていないか見守る）
├── setup_tasks.py     自動起動の登録（3方式を順に試し、確認まで行う）
├── task_launcher.py  共有データ場所を指定して各常駐プロセスを起動
├── wait_start.py      setup.bat の最終確認（本体が実際に起動したか）
├── watchdog_loop.py   スタートアップ方式のときのウォッチドッグ（15分間隔ループ）
├── register_tasks.ps1 / make_startup_shortcuts.ps1
│                      setup_tasks.py が使う登録用スクリプト
├── pyproject.toml     プロジェクト設定
├── seed/
│   ├── records.json         シードデータ（3,777件）
│   ├── deals_current.json   お買い得リスト（初期の監視レベル付けに使用）
│   └── music_type_audit.json 音楽種別の監査データ（初期取り込みに使用）
├── src/
│   └── grt/           本体のコード
└── (shared data)      D:\GameRecordTool\data（版フォルダの外に1つだけ置く）
    ├── grt.db         データベース
    ├── .env           あなたのトークンなどの設定
    └── logs           ログ
```

- `D:\GameRecordTool\data\` だけが、あなたのデータの置き場所です。データベース（`grt.db`）と設定（`.env`）は版ごとのコードフォルダの外に置きます。
- コード一式を新しい版に差し替えても、`data\` は上書きされません。詳しくは `SETUP_GUIDE.md` の「9. 更新版への差し替え手順」を見てください。

## Muse データの読み取り監査

`tools\audit_muse_handoff.py` は Muse handoff ZIP と `data\site\vgm_site.sqlite3` を照合する決定的な監査ツールです。ZIP は展開せず、ネット接続・AI・DB書き込みも行いません。件数・出典差分・根拠不足を標準出力に表示します。

```powershell
python tools\audit_muse_handoff.py --archive path\to\handoff.zip --site-db data\site\vgm_site.sqlite3
```

必要なら `--bot-db data\grt.db` を追加するとBot DBも読み取り専用で照合します。`--issues-csv path\to\review.csv` は欠けている盤別価格出典URLと、Muse調査中のDiscogs Release URLから抽出した未確認ID候補を人手確認用CSVに出します。候補IDはBot DBへ自動登録しません。同じRelease IDが複数盤に現れる場合は曖昧候補として残します。

人の確認用CSVが必要なときだけ `--issues-csv path\to\review.csv` を追加してください。価格や候補を正本へ自動反映せず、監査結果が `review_required` の場合は未確認のまま保ちます。

## はじめ方

**詳しい手順は [SETUP_GUIDE.md](SETUP_GUIDE.md) を見てください。** 大まかな流れは次のとおりです。

1. ZIP を解凍する
2. `data\.env` に Telegram Bot と Discogs のトークンを貼る（既存の設置を引き継ぐ場合は不要）
3. `setup.bat` を実行する（自動起動を登録し、その場で本体を起動して確認まで行います）
4. PC を再起動する（登録した自動起動により本体が起動します）
5. Telegram で Bot に `/start` を送る
6. Bot の案内に従い、静音例外（締切24時間前の夜間通知）の ON/OFF をボタンで選ぶ（この選択が終わるまで通常運用は始まりません）
7. `/status` で動作を確認する

## 困ったときは

- 止め方・自動起動の解除・トラブルシューティングは `SETUP_GUIDE.md` の 10 と 11 にあります。
- Bot が返事をしない等の場合は、まず `check.bat` をダブルクリックして診断ツール（`diagnose.py`）の結果を確認してください。結果は `data\logs\diagnose.txt` にも保存されます。
