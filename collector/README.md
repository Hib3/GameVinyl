# VGM Vinyl 常時収集DB

## 利用

- カタログサイト：<http://127.0.0.1:8766/>
- 取得状況・ウォッチ画面：<http://127.0.0.1:8766/collector/>
- AIに渡す候補：<http://127.0.0.1:8766/api/recommendation-candidates>
- 稼働状況：<http://127.0.0.1:8766/api/status>
- 元の一覧：<http://127.0.0.1:8766/archive/>
- 重要盤のウォッチ：<http://127.0.0.1:8766/#watch>。保存済みの出品と、観測時点の終了・欠品・価格をSQLなしで確認できます。

Windows上のPythonプロセスが動いている間、追加指示やLLM呼び出しなしで収集します。統合インストールのログオン時にWindowsタスク`GameRecordToolCollector`が起動します。ログオフ・電源停止・スリープ中は実行されません。スリープ設定は変更していません。サイトはlocalhostのみで、別PC/クラウドから直接は読めません。

## 何を収集するか

現行の自動取得先は画面の「対象サイトと取得状況」と `sources.json` に表示されます。重要盤はYahoo!オークションを優先し、設定した検索の先頭ページからタイトルまたは型番の候補を選び、個別ページで確認します。終了済みを通知・現行価格に含めません。候補は盤や付属品の同一性を保証しません。

設定は`collector/sources.json`。Yahoo!オークションは通常10分ごと、同サイトの取得間隔は60秒以上。全体180 URL/時のうち12枠を重要盤用に確保します。タイムアウト・拒否・停止中は遅延します。ブラウザ通知はページを開き許可したときのみ。閉じた状態で受け取るには、画面から各市場の公式通知を本人のアカウントで設定してください。

**全3777盤、全市場、全SNSを取得済みではありません。** 登録先以外は検索リンク・公式通知が中心で、自動取得件数に含めません。重要盤は `watches` にレコードID、検索語、照合語を設定できます（変更後は既存タスクを再起動）。

## 情報の扱い

- 原本は変更せず、継承値は未検証と表示。
- 自動収集はURL単位の候補。ニュース記事1件がレコード1盤に対応するとは限りません。
- 商品代・通貨・送料・在庫を分離。送料不明は0円にしない。
- JSON-LDの単一Offer、または確認済み店舗別セレクタから抽出。SKUを型番に変換しない。複数商品・AggregateOffer・買取価格から価格を推定しない。
- Hard OffでJSON-LDの期限切れ税抜価格と表示税込価格が違う実例に対応。元の値と差異フラグを残す。
- 購入ボタン表示は「購入ボタンを観測」であって注文成功や在庫保証ではない。
- 404・403・robots拒否・接続失敗は失敗履歴。売切に変換しない。
- 価格低下イベントも未検証。状態・盤色・送料が違えば安いとは断定しない。
- 新規取得ではHTML全文を保存せず、抽出値・ハッシュ・日時・根拠抜粋のみ保持。同じ抽出値は新しい行を作りません。既存の本体DBと取得済み原文を一括削除・圧縮し直す処理は行っていません。
- 日次のフルバックアップは停止。既存の最新1個を復元用に残し、手動バックアップのみ必要時に実行します。既存観測の保有上限は従来どおりURLごとに30件です。
- ローカル再抽出は元の取得日時を保持し、修正前の値を`extraction_revised`イベントへ記録。新しいネット観測とはしない。

## 運用（この作業フォルダー内から）

```powershell
# 状態
.\.venv-collector\Scripts\python.exe collector/manage.py status
Get-ScheduledTask -TaskName GameRecordToolCollector

# 停止・再開
Stop-ScheduledTask -TaskName GameRecordToolCollector
Start-ScheduledTask -TaskName GameRecordToolCollector

# 設定変更後は停止→開始
# robots拒否を回避するために設定変更しないこと

# 手動バックアップ（SQLiteオンラインバックアップ）
.\.venv-collector\Scripts\python.exe collector/manage.py backup

# 保存済みHTMLを新しい抽出器で再評価。外部アクセスなし
.\.venv-collector\Scripts\python.exe collector/manage.py reextract

# 自動起動を無効化（DBは削除しません）
Stop-ScheduledTask -TaskName GameRecordToolCollector
Disable-ScheduledTask -TaskName GameRecordToolCollector

# テスト
.\.venv-collector\Scripts\python.exe -m unittest discover -s collector -p test_collector.py
```

統合インストールではデータは`data/collector/collector.sqlite3`、ログは`data/collector/collector.log`です。Botの`data/grt.db`は読み取り専用でAR-ID結合します。ログは1MB×最大4ファイル。Collectorは起動時と1時間ごとにUTC日単位の復元点を確認し、`data/collector/backups/daily-YYYYMMDD.sqlite3`へ最大1世代保存します。空き容量が20%を保てない場合は作成を見送り、`/api/status`の`meta.daily_backup_status`に理由・必要容量を記録し、ログにも警告します。容量が戻れば1時間以内に再試行します。SQLite backup APIで一時ファイルへ複製し、`quick_check`成功後に原子的に確定します。新しい復元点が検証されるまで旧復元点を保持します。手動バックアップは最新の検証済み1点だけを保持します。`meta.storage_protection`には次回バックアップの予測容量・20%枠を維持して作成可能か・不足量も含まれます。

復元はタスク停止後に現在のDBと`-wal`/`-shm`を別フォルダーへ退避してから、バックアップを`collector.sqlite3`へコピーして再開します。現DBを直接上書きして退避なしに戻すことはしないでください。

## 安全性・制約

任意URL取得/DB更新のWeb APIはありません。登録ドメインと公開IPを確認し、HTTPは検証済みIPへ接続、リダイレクトも検証。Playwrightブラウザは登録ホスト以外・非GET/HEAD・WebSocketをブロックします。ブラウザ経由の外部リソース制限で表示が欠けるサイトは、成功率を誤魔化さず個別に検証します。

起動タスクはこのユーザーの対話ログオン権限で動きます。Windowsの実際の再起動/再ログオン試験は別途必要です。異常終了時には期限切れrunningジョブを3分後に再実行します。頻繁な失敗はバックオフし、認証・CAPTCHAを自動突破しません。

ブラウザでの長期メモリ安定性、全サイトの利用条件と自動取得対応、外部からの閲覧、個別嗜好の自動学習、自動購入、汎用検索APIは対象外/未検証です。
