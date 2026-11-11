# Phase 1 ビルドレポート — ゲーム音楽レコード アーカイブ／通知システム

作成: 2026-11-08（ビルド統括者）
対象: 実装プロンプト v2 §7 Phase 1 のみ。Phase 2 以降は未着手。
成果物配置: `game-record-tool/`（コード直下、ユーザーデータは `data/` に分離）

## 1. 実装したモジュール（src/grt/）

| モジュール | 内容 |
|---|---|
| clock.py | 単一時刻抽象。JST固定、`test_clock_offset_seconds`（env > settings > ローカル）で全時刻読みをずらせる。実時間待ちを排除 |
| config.py | パス解決（data/ 集約）、data/.env 読み込み、settings 読み書き |
| db.py / schema.sql | SQLite（WAL）。§5 の全テーブル＋辞書テーブル群を冪等 DDL で実装 |
| migrate.py | シード移行（冪等）。records.json 3,777件投入、前後件数照合レポートを data/logs/ に出力。辞書シード（NO_ROUTE接頭辞7件・プロモ手がかり語6件・ステージ語・コンディション汎用行） |
| normalize.py | 正規化パイプライン（NFKC→大文字→ハイフン統一→空白/ピリオド除去キー、プロモ語分離、JAN-13チェックデジット） |
| matcher.py | 統合マッチング: スコア表・拒否権・ルートキャップ・同人複合キー・バーコード統合ゲート P1/P2/E1/E2＋事例5/6、ブロッキング、同点保留、match_decisions 監査記録 |
| evidence.py | 証拠パック、正規化ハッシュ（自前ページ差分）、厳格終了判定（成功取得2回連続不在→ended、失敗/BLOCKEDは不在に数えない）、物理コピーグループ判定 |
| health.py | fetch_runs 台帳、source_health 状態機械（3回失敗→DEGRADED、24h無成功→DOWN、2回連続成功→HEALTHY）、0件異常（中央値≥5かつ0件2回→PARSE_EMPTY/SUSPECT）、沈黙検知、blocked 棚、観測空白、heartbeat |
| outbox.py | 通知アウトボックス。検出と同一トランザクション投入、冪等キー（価格を除く）、価格下落ルール（前回通知比≥10%かつ実変動）、静穏 not_before、再試行 1分→5分→30分→2h→12h・最大5回→dead_letter |
| policy.py | ポリシー版管理（policy_versions、diff記録）、シャドウ評価（直近30日の match_decisions を新重みで決定的に再計算）、フロア保証を壊す変更の検証却下 |
| scoring.py | 発見スコア（合意-15、帯判定＋列挙フロア保証 floor=60）、レビュー優先度（合意-05） |
| digest.py | 日次ヘルスレポート（種別ごと成功/失敗/BLOCKED、dead_letter数、自動降格報告、静穏例外件数、締切未確定数、フリマ公式アラート依存の定型注記）、発見ダイジェスト（切り捨てなし）、21:45 締切スイープ枠組み |
| telegram_io.py | Telegram Bot API 輸送層（httpx、無料）。テスト用 MockTransport |
| bot.py | /start（初回 chat_id 登録）/watch /unwatch /list /status /quiet、候補の承認/却下ボタン（却下理由コード必須の二段式）、初期設定ゲート（静穏例外の明示選択まで通常運用を開始しない） |
| fetchers/base.py | 全 fetcher 共通基底（低頻度・逐次、fetch_runs 記録、エラー分類、証拠保存） |
| fetchers/discogs.py | Discogs API: 監視対象の統計スナップショット（前回成功観測時刻を併記）、0→1件 出現検出。トークン無しでは無効化を明示記録し、データを捏造しない |
| fetchers/blipblop.py | Blip Blop RSS: 新譜候補登録（指紋で重複集約）、レビューキュー投入、スコア帯に応じた即時通知 |
| watchdog.py | 独立死活監視（15分周期想定）。heartbeat 20分超え→ system_events 記録＋再起動試行、復旧時に停止期間を明示した復旧通知を投入 |
| main.py / run.py / watchdog.py（直下） | 常駐プロセス（APScheduler: heartbeat 5分、outbox 1分、ヘルス評価15分、Discogs/BlipBlop 6時間、日次ダイジェスト 07:00、スイープ 21:45、Telegram 長ポーリング） |

設置パッケージ: setup.bat / SETUP_GUIDE.md / .env.template / README.md / requirements.txt（全て無料公開パッケージのピン留め）。setup.bat は Python 3.12+ 確認（無ければ winget 案内を表示して停止）→ venv → 依存導入 → data/.env 作成（既存は上書きしない）→ DB初期化＋シード → タスクスケジューラ登録（本体 ONLOGON＋ウォッチドッグ15分）まで冪等に実行し、data/ を決して消さない。

## 2. 受け入れ基準（§7・13項目）の結果

テスト: pytest 全18件合格（tests/）。ゴールデンセット検証は tests/golden_cases.json（100件）。

| # | 基準 | 結果 |
|---|---|---|
| 1 | 移行2回で Edition 3,777 / Variant / 価格紐付け数が一致、照合レポート出力 | **実施・合格**（実シードで自動テスト。2回目投入0件、件数完全一致） |
| 2 | ゴールデン100件 差分0、価格「高」誤爆0 | **実施・合格**（100/100一致、誤爆0） |
| 3 | 強制終了→20分超で異常記録＋再起動試行、復旧通知が Telegram に届く | **実施（代替）・合格**: 異常検出・記録・再起動試行・復旧通知の生成と配送を、再起動関数モック＋MockTransport で検証。実 Telegram 配送のみ **未実施（理由: トークンなし）** |
| 4 | 3回失敗→DEGRADED 警告、24h無成功→DOWN、2回連続成功→HEALTHY | **実施・合格**（時刻オフセット使用） |
| 5 | 中央値≥5で0件2回→PARSE_EMPTY→SUSPECT | **実施・合格** |
| 6 | 同一イベント二重検出→outbox 1行・送信1回 | **実施・合格**（送信は MockTransport で代替。実送信はトークンなしのため未実施） |
| 7 | 静穏時間中の即時イベントは not_before で保持され送信されない | **実施・合格** |
| 8 | 再試行 1分→5分→30分→2h→12h、5回で dead_letter、翌朝ダイジェストに件数 | **実施・合格**（時刻オフセット使用、実時間待ちなし） |
| 9 | 成功取得1回不在→sold_candidate、2回→ended、失敗取得は数えない | **実施・合格** |
| 10 | 静穏例外の明示選択が完了するまで通常運用が開始しない（未選択を検出可能） | **実施・合格**（ゲート状態を /status でも表示） |
| 11 | /watch /unwatch /list /status は許可 chat_id からのみ動作 | **実施・合格**（Bot ロジックで検証。他 chat は無言で無視） |
| 12 | 同一 update の再処理で二重承認・二重実行なし | **実施・合格**（telegram_updates の主キー＋解決済みガード） |
| 13 | 通知ゼロの日も日次ヘルスレポートが届き、必須項目を含む | **実施（代替）・合格**: 本文の必須項目（種別ごと成功/失敗/BLOCKED、dead_letter数、自動降格報告、フリマ公式アラート依存の注記、締切未確定数）を検証し、配送は MockTransport で確認。実 Telegram での朝配送は **未実施（理由: トークンなし・運用開始後）** |

実 Discogs API 呼び出し: **未実施（理由: トークンなし）**。フェッチャー本体は実装済みで、トークン未設定時は「無効化」を system_events に記録して沈黙・捏造しないことをコードで担保。Blip Blop 実フィード取得もテストではネットワークを使っていない（登録ロジックは単体で検証可能）。

## 3. ビルド中に確定した解釈（設計側の確認待ちとして記録）

ゴールデンセットは U-2（事例数割当）が未決のため統括者が割当を決定した（バーコード系を厚めに配分）。マッチャーとの差分25件を精査し、仕様文と整合する側に統一した主な解釈:
- タイトル拒否権は出品タイトルが「存在しない」場合は発火しない（証拠の不在は矛盾ではない）
- スコア60未満のペアは拒否権があっても「低」のまま（拒否権は格上げしない）
- バーコードゲートにもルートキャップ（GOODS上限・プロモ手がかり・distribution unknown の価格「中」止まり）を適用する
- 事例5の扱い: promo 形態（見本・非売品）は強制レビュー、その他非一般流通は候補登録止まり
- P2 はタイトル完全一致があっても成立する（E1 は variant_gate=false の場合と定義）
- E2 の価格確信度は「低」（価格なし）
- format_detail の明示的矛盾（Mono/Stereo、2LP set/1LP）はフォーマット拒否権。フォーマットクラスは枚数区別（2xLP ≠ LP）

## 4. §12 未決点の扱い（値を発明していない）

- 締切種別辞書（deadline_kind_dict）は**空**で出荷。実ページの文言確認までは deadline_kind='unknown' のまま人手割当
- review_queue.kind / duplicate_suspects.kind の値域は暫定値（match_review / candidate_review / biblio_verify 等、仕様文に現れる語のみ）— 確定値ではない
- cond_rank の導出元・want 条件の単一ランク換算（§12 内部未決1・2）は未実装。該当判断はレビューキュー行きとし、自動判定しない
- barcode_field_tier は一次確認まで 2 のまま（ティア1単独の P2 は実データでは発火しない）
- U-1〜U-7 の仮置き値は仮置きのまま実装し、設定・定数で切替可能にした（静穏例外=推奨OFF提示＋初期設定で明示選択必須、U-6 ダッシュボードは作らない）

## 5. シード移行の判断

- records.json の価格証拠（中央値・最終成約日）は price_snapshots に移していない（スナップショットは運用開始後の実観測専用。種データは同梱 seed/records.json に保持）
- discogs_id はプロジェクト既存データ（deals_current.json）から 284件のみ裏付け付きで補完。music_type は監査ファイル（music_type_audit.json）から全件補完。どちらも推測ではない
- 流通2軸は根拠URLがないため全件 'unknown'（推測の一括割当は仕様で禁止）。そのため運用初期、価格確信度は仕様通り「中」止まりになる
- catalog_norm は複合表記（例: CD/Vinyl 併記）を決定的に結合したベストエフォートキー

## 6. 残っている未完了事項

- Phase 2 以降は一切未着手（指示通り）: インジェスト運用・自動降格の運用開始、締切モデルの運用（列定義のみ実装）、ソース発見・拡張エンジン（§C-11）、Telegram 全コマンド（/level /want /price /queue /deadlines /coverage /pause /resume）、GOODS/DOUJIN_EVENT/BIBLIO 固有フェッチャー
- 実トークンでの最終確認（Telegram 実送信、Discogs 実 API、BotFather 登録フロー）はユーザーの PC で SETUP_GUIDE.md の手順に沿って行う段階のもの
- 本 VM の .venv はテスト実行用であり納品物ではない（setup.bat が Windows 側で新規作成する）。ZIP 化の際は .venv と __pycache__ を除外すること

## 7. ビルド中の出来事

書き込み経路の不具合で5ファイル（bot / digest / watchdog / fetchers 2件）のインデントが欠落したため、別経路で書き直した。全モジュールはコンパイル確認済みで、上記テスト結果は書き直し後の最終状態に対するものである。
