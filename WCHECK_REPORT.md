# Wチェック報告書 — Game Record Tool（納品前二重チェック）

目的: v1.0→v1.8 の8回・5.8時間を繰り返さないための、7視点×
25ラウンドの二重チェック。構成: 5ブロック × 5ラウンド
（R1 7視点の指摘 → R2 最終承認者の検証 → R3 修正 →
R4 同視点の再チェック → R5 回帰テスト固定）＋最終承認。

開始時点の版: v2.0.3（テスト489件・仮想Windows検証16項目）。

---

## 第1ブロック（R1–R5）: Windows導入・起動 — 完了（2026-11-08）

### R1 の指摘（全て再現証拠つき、視点別）
- 卸業者: Route 1（schtasks）登録タスクが Windows 既定の
ExecutionTimeLimit=PT72H を背負い、常駐が72時間で停止される。
検証系は Settings を一切見ていない（本人の PC の現在の登録もこの型）。
他に winsim W-06 の検証法の誤り（TZDIR は zoneinfo が読まない）、
DST fold/gap 締切の無言 confirmed 化、PS ウォッチドッグの
RepetitionDuration 3650日失効。
- コレクター: job_errors.log（v2.0.3 で新設）と
duplicate_instance.log が診断のどこにも出ない（v1.7 と同型の穴）。
§5 スタートアップ方式が.lnk の存在しか見ない。diagnose に
例外隔離がなく diagnose.txt が最後にしか書かれない。
- 中古セラー／マニアA（独立に同件）: 方法Aの手順に
「旧本体を止める」が無く、手順自体が新旧二重起動（409）と
稼働中DBの不安全なコピーを招く。加えて GRT_DATA_DIR の
旧フォルダ絶対パスによる silent bind、wait_start の証拠偽装
（旧スタンプ＋旧版 START 行）。
- AIエンジニア: ウォッチドッグに例外 containment と
失敗記録がない（壊れた heartbeat で無記録のまま死に続ける）。
spawn 成功＝復旧扱いで起動即死する子を二度と起こさない。
storewatch 等のジョブに _log_job_error の包みがない。
- 敵対的QA:.env が UTF-16／Shift_JIS だと load_dotenv
が例外を投げて起動クラッシュループ。BOM でトークンが黙って
無視される。ウォッチドッグ側に入口ロガーがない。
- マニアB: fakewin の PS ルートが実スクリプトを読まない
tautology、winsim W-03 検出器の双方向の壊れ、W-06/W-17 の
走査範囲、PS5.1 互換検査の不在、diagnose §5 照合の無テスト複製。
- W-03 / W-17（winsim の FAIL/NOTE）は7視点全員が「実害なし」と
判定（根拠: 存在探査の定型／ロック専用ハンドル）。

### R3 の修正（→ v2.0.4）
上記の全件を修正。主なもの: Route 1 を /XML 登録に変更し
PS と同一 Settings を明示＋verify／診断が Settings とトリガー
Enabled を検証。方法A §9 に手順0（自動起動の解除→停止）。
.env 復号チェーン（UTF-16/BOM/SJIS/BOMなしUTF-16、例外なし）。
ウォッチドッグの parse ガード・再試行間隔・入口ロガー。
storewatch のソース単位隔離と残りジョブへの _log_job_error 配線。
診断の §1（APIキー・データ場所）／§5（リンク先・Settings）／
§6・§8（全ログ掲載）／例外隔離と逐次保存。wait_start の版照合と
boot_at 限定、スタンプ失敗時の開始ステップ失敗化。DST の
ambiguous 格下げ。winsim／fakewin の検証側修正も同梱。

### R4 の再チェック
6視点が全件「解消」を実行で確認。中古セラーが修正文自体の
自己矛盾を新規に発見（手順0の「再起動が確実」は自動起動解除が
先という順序が抜けていた）→ 順序を直して再梱包。他に残存
軽微4件（BOMなしUTF-16、.env=ディレクトリ時の例外、復旧分岐の
json.loads、診断保存のフォールバックとバナー文言）も同梱で修正。

### R5 の固定
テスト 489 → **503 件全通過**（新規 test_wcheck_b1.py 14件）。
仮想Windows検証 17 項目 **FAIL 0**。v2.0.4 の納品 ZIP で
新規導入・方法A更新の両検証が緑。

---

## 第2ブロック（R6–R10）常駐運用・スケジューラ・健全性 → v2.0.5

### R6 の指摘（7視点、全件再現付き）
- AIエンジニア【重大×6・軽微×3】: 全ジョブが単一 sqlite 接続を
  スレッドプールで共有し干渉・行ロスト（実測 81例外/96行ロスト）。
  misfire_grace_time=1秒で cron が無記録に欠落。ポーリング
  スレッドの死亡が検知不能（非dict要素で except 内 AttributeError
  死を含む）。offset を処理前に永続化（コマンド喪失）。
  process_outbox 多重実行で二重送信。ジョブ例外の containment 穴
  （try 自体がないジョブ、gate が try の外、ingest 外側 SELECT）。
  gate 閉でも last_job_at が進む。max_instances 破棄が無記録。
  固定5秒リトライ＋ログ無制限増殖。
- コレクター（通知到達）: 再送スケジュールの 720 分段が到達不能
  （MAX_ATTEMPTS=5、実効 ≈2.6時間）。出現エピソードの永久抑制。
  quiet 判定が enqueue 時のみ。4096字超で dead-letter。
  dead_letter が終端。並行送信の二重化。壊れた found_at で
  ダイジェスト全滅。優先順無視。静穏例外カウントの生涯累積。
  sweep horizon の 06:30 ハードコード。
- マニアB（outbox 深掘り）: in-process の 'sending' 回収なし。
  クラッシュループで無制限再送。claim が非原子的。enqueue が
  呼び出し側トランザクションを巻き戻す／勝手に commit する。
  fallback キーの秒精度衝突。URL キーの過剰正規化。
- 敵対的QA: 429/401 の固定リトライ。fetch_updates の形状検証
  なし。TelegramError が error_code/retry_after を持たない。
  スケジューラの miss/error がどこにも残らない。
- 卸業者（時間駆動）: expected_cadence_days の writer 不在。
  silence の observation gap が二度と閉じない。ゼロ件異常が
  小規模ソースで発火しない。DOWN/静穏の即時通知なし。
  テスト時計オフセットの無警告。consec バンキングの不整合。
  freqpolicy の 15 分枠レース。
- マニアA（取得レーン）: Discogs の BLOCKED が run を止めない。
  部分成功が全失敗として記録される。固定周期レーンが頻度梯子を
  見ない。storewatch が台帳に一切記録しない。sitemap の [:50]
  スライスで 51 件目以降が永久に候補化しない。blocked shelf が
  読み手なし（write-only）。stats ペイロード検証なし。
  appearance の価格混入経路。
- 中古セラー（肥大・資源）: 保持・上限・掃除が全体に皆無
  （DELETE 0件）。ログ無制限。索引が1本もない。match_review の
  重複蓄積。ディスク監視なし。

### R7 の検証（私の実コード精読で全件確認）
- 再送スケジュールの実効窓はコレクター／マニアB の読みが正しく
  ≈156分。敵対的QA の ≈14.6時間読みは誤りと確定。
- schema.sql に CREATE INDEX がゼロであること、outbox に
  created_at がなく quiet_override 行の時刻代理が not_before で
  あること等、以降の修正の前提を実測で固定。

### R8 の修正
- outbox 全面改修: MAX_ATTEMPTS 6（全段到達、総スパン ≈14.6h）。
  原子的 claim（WHERE state ガード＋rowcount）。recover_sending
  （in-process 10分／起動時、試行消費＋backoff、予算で
  dead-letter）。送信時 quiet 再判定＋quiet_window_end の同日帯
  バグ修正。CHUNK_LIMIT 3900 分割。BATCH_LIMIT 50＋優先順。
  retry_after 優先。enqueue の SAVEPOINT 分離。dead_letter 復活。
  fallback キーへの body ハッシュ。URL キー正規化の適正化。
- main 全面改修: ジョブごとの自前接続。misfire grace（interval
  300秒／日次 3h／週次 12h／四半期 24h）＋listener（miss/error/
  max_instances → system_events＋job_errors.log）。起動時
  キャッチアップ（digest／sweep／staged alerts）。poll は処理後
  にのみ offset 前進、transient は再配送、fetch は漸進 backoff
  （retry_after 優先、10回で poll_degraded、復旧で
  poll_recovered）。Discogs/BlipBlop/同人に is_due ゲート。
  sitemap は新規 fingerprint 50件/回方式へ。
- AIエンジニア遅着報告の追加修正: ポーリングスレッド監督
  （hb が生存監視、死亡で poll_thread_dead 記録＋再起動、
  1時間5回まで、枯渇で poll_thread_restart_exhausted）。
  poll_alive_at 刻印＋diagnose 表示。outbox_job の last_job_at を
  gate 開放時のみ前進。終了経路に join＋接続 close。
- health: ゼロ件異常の拡張（履歴非ゼロで発火）。DOWN／silence の
  即時アラート。gap のクローズ。is_shelved 新設（BaseFetcher が
  入口で強制）。freqpolicy: cadence writer、banking cap、
  BEGIN IMMEDIATE 化。
- fetchers: base._record（台帳失敗がフェッチを壊さない）。
  Discogs の BLOCKED 即時 abort、部分成功の正直な記録、stats
  検証（num_for_sale int 必須、空 {} 拒否）、出現エピソードキー。
  storewatch の台帳化＋梯子連携。
- 新規モジュール: logtrim（全ログ 5MB→1MB、読みは末尾のみ）、
  maintenance（週次: fetch_runs/system_events 180日、
  telegram_updates 90日、evidence 30日、アーカイブ不削除、
  ディスク <500MB 警告）。索引10本を init_schema へ移設。
- digest/deadlines: 壊れた found_at の許容、sweep horizon の
  quiet 終了追随、sweep マーカー、guarantee_cutoff の事後申告。

### R9 の再チェック（7視点全員が再現を当て直し）
- 全視点で R6 欠陥は「解消」。部分的・新規として残ったものは
  その場で追加修正し回帰テスト化した:
  - 中古セラー N-1: maintenance の段階分離がない（1表の異常で
    ディスク監視まで停止）→ ステップ単位の隔離＋常時サマリ記録。
  - コレクター N-1: 復活経路だけが呼び出し側を早期 commit →
    commit を外し通常経路と対称化。N-2: pricedrop キーが価格の
    Python 型表現に依存 → float 正規化（既存キーと完全一致）。
  - マニアA D-8 残余: shelf の適用が BaseFetcher のみ →
    Discogs／storewatch レーンにも相談を追加。観察1
    （lowest_price 異常形状が素の AttributeError）→ stats 検証に
    統合。観察2（run 戻り値が実効結果と不一致）→ 実効結果を返す。
  - 卸業者 N-1: キャッチアップの digest 判定が health キーのみ
    → 両キー必須に変更。
  - 敵対的QA N-1【中】: {"ok": true, result 欠落} が素の
    KeyError を投げ、送信経路で D-3 型のコマンド喪失が再発 →
    _ok_result で TelegramError 化。D-4 残余（生存プロセスの
    長スリープで当日の digest/sweep が再実行されない）→
    _daily_catchup を新設し1時間ごとのジョブ化、sweep は
    スロット記録（last_deadline_sweep_slot）方式へ。
  - AIエンジニア OBS-1: 枯渇通知がプロセス内1回のみ → 予算に
    空きが戻ったら再武装するよう変更。
- テストはブロック開始時 503 → **552 件全通過**
  （test_wcheck_b2.py は 14 → 49 件）。既存テストの変更は
  理由コメント付き（QC R2 のジョブ集合、QC R5 の Discogs
  異常形状トリガーは上流検証への移行に伴い差し替え）。

### 正直な残件（第2ブロック）
- マニアA D-10 の残余: 「初日からずっと0件」のソースは、履歴に
  非ゼロが一度もないため異常検知の原理が働かず HEALTHY のまま
  残る。緩和策は入れたが完全には閉じていない。承認時点で
  実在確認する運用が前提。
- AIエンジニア OBS-2: R9 中に他レビュアーの並行実行と重なった
  1回だけフルスイートで 8 failed/31 errors（resident 系、
  sqlite）が出たが、直後の再実行2回と私の単独実行では再現せず
  （552 全通過）。VM／tmp 競合の可能性が高くコード回帰とは
  判断しないが、記録として残す。
- 保持日数（180/180/90/30日）とディスク閾値（500MB）は暫定の
  運用デフォルト。中古セラー視点で妥当性は確認済みだが、
  利用者の判断で変更しうる値として最終報告で明示する。
- 仮想Windows検証の限界は第1ブロックと同じ（実 Windows の
  スリープ／Modern Standby の時計ジャンプは実機でのみ確定）。

---

## 第3ブロック（R11–R15）価格証拠・マッチング — 完了 → v2.0.6

### R11 指摘（7視点・全件再現付き）
約30クラスタ（A〜K）。骨子：通貨整合性（/price の通貨混合・通貨不明を
円として記録・currency 列を更新しない）、価格の妥当性（負価格・文字列
価格・inf が証拠化）、マッチャ構造（バリアント gate が本番未配線＝
P2 語彙不一致・型番接尾辞の減点なし・doujin 分岐の caps 回避・
case5 の順序）、pricedrop が価格確信度を読まない（9-C12 漏れ）、
同梱の cond 混在、cond ラダー未供給（生産者なし）、欠席ライフサイクル
未配線、完全性チャネルの二重数え、スイープの接尾辞版数問題、候補の
出典URL集約未実装、証拠ボディ未保存。

### R12 検証（私の一次確認）
主要主張は全て一次資料で確認。追加で判明：seed/records.json には
JAN が1件もない（0/3,777）→ バーコード gate は JAN データが登録
されるまで休眠（生産者不在は構造的事実として最終報告へ）。

### R13 修正方針と実装
- 円の意味論を scoring.yen_value に一元化（total_jpy 優先、JPY の
  price のみ円）。pricedrop は価格確信度「高」の decision を持つ
  listing だけを証拠化（9-C12 を pricedrop 内部で強制）。
- マッチャ：全バリアント評価＋決定性のある勝者選択、JAN ブロッ
  キング、型番接尾辞の減点 −15（仕様 C-12.2）、doujin caps 適用、
  形式クラスの誤爆修正、int 解析の例外排除。
- case5 の価格確信度は「中」を維持：ゴールデン9件（G071–75、
  G092–95）が仕様 C-5.4 の定める回帰オラクルであり、「中」は証拠
  経路に一切流れ込まない。C-5.3 の文面との緊張は利用者判断事項
  として残す。
- ingest：価格抽出の文脈判定、売り切れ判定、URL 正規化、通貨
  規律（不明は NULL のまま）、cond 抽出＋否定対応ラダー、per-item
  containment、欠席ラウンドの配線（ingest レーン）。
- 証拠ボディ：失敗ボディの pending registry → fetcher 完了時に
  commit、fetch_runs に raw/normalized ハッシュと body_path。
- テスト 552 → 591 全通過。

### R14 再チェック（7視点・全件再現付き）で残存が判明し、R15で修正
中級 14 件・軽微多数。主なもの：
- 同一個体パスが通貨不明の生金額を円として通知（F1＝3視点一致）
  → 生産側で円換算して渡す契約＋ pricedrop 側で yen_value 必須化、
  負価格・inf の遮断を追加。
- P2 が disc_conflict と型番接尾辞不一致を無視（価格の false
  positive 方向）→ 両方を P2 の前提から除外。
- バリアント勝者選択が確信度を無視し挿入順で反転（F9）→ 勝者
  キーに価格/存在確信度と disposition を追加（順不同を再現で確認）。
- バリアント帰属の捏造（collector A）→ variant_gate を通過した
  判定だけが listing.variant_id を書く。単一バリアント時の強制
  帰属は維持。
- /price：売り切れスナップショット（通貨 NULL）で履歴が消える
  → 系列通貨は「最新の既知通貨」。バケット件数は価格あり行のみ。
- スイープ：予算切断時に未探査の接尾辞へ版数スタンプ（S-1）、
  辞書更新のたび全接尾辞を再探査＋解決済み hit の復活（S-3）、
  空文字列の証拠がゲートを通過（S-2）→ 接尾辞単位の探査マップ
  （suffix_probe_json）に再設計、2パス化（新規番号優先）、
  sweep_hit は同一 payload を二度と投入しない。
- 価格抽出の過剰除外（送料込み/送料別/割引価格）、inf 飽和、¥0
  → 注記フレーズの除去、有限・正値ガード。
- cond：新品同様→5（6 と誤判定）、抽出器の誤キャプチャ（組/付/
  帯）、売り切れ判定の範囲盲、在庫あり文の優先。
- 欠席：保存 URL の canonical 比較、死んだソースのグループ
  デッドロック解消。
- Discogs：負の num_for_sale、value=0 の観測保持、通貨の型
  サニタイズ、NULL prev での出現通知の抑止。
- GTIN：無効な gtin13 が有効な gtin12 を遮蔽 → 有効な値が優先。
- 通貨コード正規化（normalize_currency）を全境界に。
- outbox.enqueue の失敗時 full rollback が呼び出し側の未
  コミット作業を消去 → SAVEPOINT 境界に変更。
- REPRESS 通知の one_line 未適用（最後の sink）、one_line の
  双方向制御文字（Cf 区分）の除去。
- テスト 591 → **627 件全通過**（test_wcheck_b3.py は 39 → 75 件。
  既存テストの変更は全て理由コメント付き：B2 pricedrop フィクス
  チャへの yen 値追加、QC R5 の異常トリガーを num 桁溢れへ移行、
  webpages の GTIN は「有効な値が優先・無効のみなら素通し」へ
  意味論を明示）。

### 正直な残件（第3ブロック）
- edition_variants / editions.jan の生産者が存在しない：seed に
  JAN データがなく、バリアント行を登録する経路もないため、
  バーコード gate・バリアント帰属・完全性スイープは JAN/バリ
  アントデータが登録されるまで休眠。構造的事実であり、Wチェック
  の修正対象ではない（最終報告で明示）。
- バケット基準は仕様どおり MIN のまま。外れ値による基準毒性は
  入力浄化＋確信度ゲート＋欠席配線で緩和したが、原理的には残る。
- match_review の承認を価格「高」decision に変換する消費者が
  いない（R13 中に発見）。E1 の価格がレビュー承認を経て証拠化
  される経路は未実装 — 第4ブロックの明示チェック項目とする。
- v1.8 時代の実 DB に TEXT 型 lowest_price 等の旧行が残っている
  可能性は、この環境からは検証不能（実 DB は利用者の PC 上）。


## 第4ブロック（R16–R20）発見・候補・締切・REPRESS — 完了 → v2.0.7

### R16 指摘（7視点・再現必須）
- レビューキューの出口欠如が構造的欠陥として確定：match_review /
  sweep_hit / copy_bundle_review / seed_promotion の各 kind に
  消費者（承認→実効）も解決経路もなく、承認は行き止まり、
  copy_bundle は承認のたび再投入される無限ループだった。
- 同人レーンの候補は status='doujin_event' のままキュー行すら
  なく不可視・不可解決（4視点が独立に重大と判定）。同人 seed の
  昇格は Gate 1 が構造的に充足不能で到達不能。
- 締切の生産者が皆無（set_schedule の本番呼び出しゼロ、/deadlines
  は恒常的に空に見える）。延期は supersede がなく旧日付が発火
  し続ける。未通知申告の probe が 72h キーを欠き誤申告。
- REPRESS：discogs_new_release は生産者不在（v2.0.3 §7 残件の
  再確認）。announcement（候補 id）と variant_stock（Edition id）
  のキー分裂で連携不能。Blip Blop の再発判定は部分文字列の
  誤検出（再発売・否定文）を含む。
- 独立ソース数（multi_source +25）の生産者がなく、却下候補は
  独立した再発見でも復活しない。/coverage は Discogs スナップ
  ショットのみで店舗出品の価格証拠を数えない。

### R18 修正（627 → 676 件全通過、test_wcheck_b4.py 49件新設）
- 全 kind に qok:/qno: 消費者を実装。match_review 承認は
  価格「高」の superseding decision を作り price_evidence_ok が
  成立する（第3ブロック残件の解消）。copy_bundle 承認は実バンド
  リングを実行し再投入ループを断つ。seed_promotion 承認は
  Gate 1 を満たしてから promote（昇格先は limbo でない pending）。
- 候補承認がカタログ化する：Editions 行（AR-Cxxxxx・watch_level
  0）を作り、REPRESS 信号と締切を候補から Edition へ移行する。
  ※「承認→カタログ登録」は仕様 v3 の読みを採用した実装判断 —
  最終報告で明示し、利用者の判断を仰ぐ。
- 同人レーンを通常フローへ：候補は pending＋candidate_review
  行＋ボタン通知。seed と複合キー一致する収穫は seed_promotion
  要求のみ（候補を作らない）。締切生産者を同人イベント開始日
  から配線し、手動 /deadline コマンドも新設。set_schedule に
  supersede（deadline_history 記録）を導入し全消費者が
  superseded を除外。TZ なしは tz_unknown のまま tzfix で回復。
- REPRESS：digest_notes は却下候補と id なし行を除外し最終信号
  日を併記。Blip Blop 記事タイトルが一意に一致する Edition へ
  announcement を橋渡し。variant_stock は prev='ended' のとき
  のみ武装（1ラウンドの揺らぎでは発火しない）。
- 独立ソース：URL を canonical 化して保持しホスト数を生産。
  新規ホストでの再発見は却下候補を復活（pending＋再レビュー）
  させ、pending 候補の優先度を押し上げる。sitemap は URL
  identity 正規化、fingerprint は中黒「・」を畳む。
- 各候補登録レーンに per-item containment（毒アイテム1件で
  レーン全体が止まらない）。/queue は kind 別件数と先頭詰まり
  解消（stale 自動 settle）を実装。

### R19 再チェック（7視点）とフォローアップ修正（676 → 715 件）
主経路は全視点で確認済み。縁の欠陥を追加修正：
- 同人イベントの「延期」が重複経路では伝播しない（生産者は
  新規作成時だけだった）→ 重複経路でも set_schedule を呼び、
  開始日の後入れも反映。目撃記録はイベント「名前」でなく
  「ID」でキー付けし重複排除、詳細に候補 id を含める。
- seed 昇格時に同じ作品の収穫候補が別個に残る → 昇格時に
  fold（締切・信号を seed へ移行し merged として解決）。
  Gate 1 は承認 payload の found との一致を要求し単回消費に。
- seed と一致する同人収穫が二重候補を作らないよう、seed 事前
  チェックを登録の先頭へ。複数作品抽出はパスセグメント判定＋
  ナビ文言除外にし、画像のみアンカー（alt）を拾う。
- match_review 承認が次の再マッチで上書きされ価格証拠が消える
  → human-review-1 の最新 decision がある出品は再評価で新規
  decision も再キューも作らない（承認の持続性）。消費者は
  decision_id と Edition の整合・score の型・Edition 実在を
  検証し、解決不能な payload は「未処理のまま」と明示して
  行を残す（偽成功の解消）。毒 payload（非 dict 入れ子）は
  /queue を詰まらせない。
- set_schedule：瞬時なし再登録は瞬時あり行を supersede しない
  （「近日中」で確定締切が消えた）。id 両方 None の行は slot
  同一性を持たない。同一瞬時・別チャネルは別登録として残し、
  二重アラートはアラート層で sibling スキップ（staged/sweep）。
  commit は呼び出し側のトランザクションを奪わない。
- 未確定締切の数え方を1定義に統一（unconfirmed_count /
  unconfirmed_summary の partition が一致）。NULL 軸
  （kind NULL・confidence NULL・confirmed だが瞬時なし）を
  全部拾う。21:45 sweep が title=NULL の1件で全体停止した
  問題を修正。却下候補の締切は発火しない。
- REPRESS：register_signal は at を日時に解釈して JST 正準形
  で保存（表記揺れの二重登録と未来クランプ回避を解消）。
  候補 rejection は橋渡し済み（両キー）信号も取り下げる。
  digest_notes は作品単位に再集約。Blip Blop の続報記事
  （重複経路）でも announcement 記録＋信号が立つ。再発判定は
  節単位＋否定語彙の拡充。「オリジナルサウンドトラック」等
  の総称タイトルは橋渡ししない。
- 候補承認の行き先：信号が指す Edition が一意なら AR-C を
  新規発行せず既存 Edition に linked する（重複カタログ防止）。
- 独立ホスト数：ホスト名でなく運営者（registrable domain、
  マルチテナント接尾辞は例外）で数える。末尾ドット正規化。
  2回目以降の復活も通知が届く（idem suffix にホスト名）。
- 在庫遷移：店舗が明示した OutOfStock は「不在推定」と区別
  （oos_declared）。明示の売り切れ→在庫ありは variant_stock
  が立つ。交互パターンでも ended に到達する。新 URL での
  再出品（同店舗・同 Edition の ended 行あり）も信号化。
- その他：outbox.enqueue_candidate_review の id 型検証、
  FeedFetcher の非 dict アイテム containment、page_identity
  のデフォルトポート除去、sitemap 内 identity 重複排除、
  /deadline の「候補#n」表記受理と無効 TZ の明示エラー、
  /coverage は正値の価格のみ計数、sweep_hit 承認が却下済み
  候補に当たったら復活させる、/list・/price のタイトル
  one_line 化。
- 既存テストの変更は理由コメント付き（QC /status の未確定
  件数は新定義により 2→3、B4 復活テストの idem キーは
  prefix 一致へ、B4 flap テストは不在推定由来に限定し明示
  売り切れ由来は R19 テストへ分離）。

### 正直な残件（第4ブロック）
- discogs_new_release の生産者は依然として不在：Discogs
  Master のポーリングという新規機能であり、Wチェックの修正
  対象ではない。REPRESS の 2 系統到達は announcement +
  variant_stock のみ（手で信号を与えれば intake/evaluate は
  動作する）。最終報告で明示する。
- 同名タイトルの橋渡し限界：seed 実測で重複タイトルは 706
  グループ / 3,777 件中 1,946 件（51.5%）。一意一致しない
  タイトルは誤橋渡しの危険が勝つため橋渡ししない（カタログ
  化の時点で信号は合流する）。設計上の限界として記録。
- 「・」畳みの fingerprint 衝突（A・BC と AB・C が同一化）は
  R16 集約修正とのトレードオフとして受容。
- 同人締切はイベント開始日 00:00（開場前最大半日早い）—
  設計判断として記録。
- 締切は (対象, チャネル, 種別) で 1 件というモデルを維持
  （延期は supersede＋履歴で表現）。正当な複数締切を同一
  slot に置く運用は想定しない。
- 改名が未観測のイベントは「未収穫」と区別できないため、
  harvest 異常の申告対象になる（申告自体は仕様どおり）。
- 過去言及（歴史的記事）の event_stage 誤分類は決定論的
  限界として R18 から引き続き記録。

## 第5ブロック（R21–R25）敵対的入力・障害復旧・横断回帰 — 完了 → v2.0.8

### R21 指摘（7視点・再現必須、報告は packaging/r21_reports/）
横断テーマ：敵対的入力（TEXT毒・サロゲート・巨大ボディ）、障害復旧、
第1〜4ブロック修正の回帰確認。主な実欠陥：
- outbox の attempts 列 TEXT 毒（'abc'）で process_outbox 全体と
  recover_sending が毎パス死ぬ（AI 中-1）
- scoring.yen_value の float() 無防備で価格経路が例外死（AI 中-2）
- lone surrogate が送信層で例外死し /queue が詰まる（敵対的 中-1）
- HTTP ボディ無上限・証拠ボディ無上限（敵対的 中-2）
- match_review の解決不能行が却下でも settle 不能（マニアB F-1）、
  優先度 0 で埋没（F-2）、人間判断ガードが listing 単位で他 Edition の
  機械判定まで止める（F-6）
- 宣言OOS がグループ判定の mark_seen で打ち消される（セラー 中-1）、
  幽霊出品（30日以上未観測）が価格基準・/coverage に残る（セラー 中-2）
- host 単位の店舗同一視の取りこぼし（卸 中-1 部分）、チャネル跨ぎの
  延期伝播なし（卸 中-3）、締切の取消手段なし（卸 中-4）
- 昇格済み seed の再収穫で二重候補（マニアA 中-1）、seed 経路に
  締切・目撃が付かない（中-2）、日付形式揺れで締切が消える（中-5）
- /status の種別軸が fetcher taxonomy と不一致（コレクター 中-1）、
  通知が ID のみでタイトルなし（中-3）

### R22 検証（私の一次確認）
全 load-bearing 主張を実コードで確認（outbox/scoring/matcher/bot/
evidence/deadlines_ops/doujin_event/seedops/candidates）。
検証不能・誇張と判定したものは修正対象から除外し、各報告に記録。

### R23 修正（715 → 763 件全通過、test_wcheck_b5.py 48件新設）
上記を全面修正：outbox 試行回数の防御読み＋not_before 修理、
yen_value の数値化ガード、one_line/strip_surrogates の送信境界適用、
HTTP 25MB ストリーミング上限＋証拠ボディ 1MB 上限、コールバック ID の
ASCII 化、/deadline 年範囲チェック、match_review 解決可否プローブ
（却下は settle・承認は検証維持）、優先度 45、queue 未知 kind の
stale settle、人間判断ガードの (listing, edition) ペア単位化、
グループ判定の宣言OOS 保護、鮮度30日（bucket_best・/coverage・
グループガード）、STRUCTURED レーンの不在判定参加、tenant suffix
追加＋IDN 正規化、締切のチャネル跨ぎ延期伝播＋/deadline_cancel 新設、
fingerprint 装飾語彙、同人 seed 経路の締切/目撃配線、イベント日付の
寛容パーサ、/status 軸の統一、通知へのタイトル付与、衝突疑い件数の
ヘルスダイジェスト表示。既存テスト2件は鮮度述語の変更に伴い
理由コメント付きで fixture 更新。

### R24 再チェック（7視点・再現必須、報告は packaging/r24_reports/）→ フォローアップ修正（763 → 794 件、test_wcheck_b5_r24.py 31件新設）
主経路は全視点で閉鎖確認。縁で新たに見つかり修正したもの：
- 防御ヘルパーの例外クラス漏れ：attempts の Inf（OverflowError）・
  負数が同じ症状を再現（AI 中-A）→ _attempts_of を全域化＋クランプ、
  復旧 skip の記録。/price の fmt_amount/as_float/num_for_sale も
  Inf で無言死（AI 中-B）→ isfinite 対称化。yen_value の巨大 int
  （軽微-C）も同型で封鎖
- グループガードの鮮度が「試行」で測られ、常時 BLOCKED の店舗が
  凍結を再現（セラー F-1）→ result='OK' の run のみに限定
- 終了済みメンバーの再判定で listing_ended 重複＋streak 水増し
  （セラー F-2）→ mark_absent に terminal ガード。部分 present の
  不在カウント水増しも是正
- 鮮度の経路間不一致（bucket_best の NULL/解釈不能 last_seen が
  「新鮮」扱い、/coverage の文字列比較）（セラー F-3）→ 解釈不能は
  鮮度なしに統一、/coverage に ISO 形状 GLOB
- /coverage listings 側の typeof ガード欠如で TEXT 金額が証拠化
  （セラー F-4）→ snapshot 側と対称化
- **取消した締切が同人レーンの再収穫で復活**し、取消返信の
  「今後アラートは届きません」が虚偽だった（卸 中-A）→
  set_schedule に取消 tombstone（自動レーンは同一日時を再登録
  しない。手動 /deadline は明示的な復活として許可）、返信文面を是正
- 移動側チャネルが旧日時を保持していない延期では伝播が発火せず
  静かに競合（卸 中-B）→ 別チャネルの行は自動 supersede せず、
  登録時 system_event＋ダイジェストに「締切の競合」行（番号・
  日付付き）で表面化し、利用者の /deadline_cancel に委ねる
  （最終承認者判断：他チャネルの登録は別の約束でありうる）
- offset 付き deadline_jst が deadline_tz 無しで捨てられる
  （卸 軽微-C）→ offset は自己決定的として変換。旧 Phase-2 テストの
  「no TZ -> no JST, ever」は naive 値に限定して維持（理由明記）
- 装飾語彙に 【早期予約】【先行予約】 追加（卸 軽微-D）
- 橋渡しヒントが承認の実挙動と食い違う（信号が複数／実在しない
  Edition を指す場合に「紐付きます」と断言）（コレクター 中-A）→
  提示規則を解決規則に一致（単一・実在のときだけ断言）
- タイトル長上限の不整合（コレクター 軽微-ア）→ 締切ラベル・
  値下げ・REPRESS ノートも 120 字キャップに統一
- /deadlines 注記「処理すると次が表示されます」が解析不能行では
  不成立（軽微-ウ）→ 取消での前進を明記。確定一覧もタイトル化（軽微-エ）
- サークルなし seed の fold が別サークルの既知候補を巻き込む
  （マニアA 中）→ どちらか一方がサークルを名乗り不一致なら fold しない
- event_start_unparseable の dedupe 欠如（マニアA 軽微）→ 値単位で記録
- match_review の source/url 非スカラ値で probe/consume 共に
  ProgrammingError（マニアB N-1）→ isinstance ガード共有化。
  score=NaN の通過（N-6）→ isfinite 必須化
- 既存 DB の priority 0 行が backfill されない（N-3）→ init_schema で
  冪等 backfill（未解決 match_review の 0 → 45）
- 衝突疑い件数が resolved を数える（N-4）→ 未解決のみに限定
- match_review 提示文面が解決不能行でも承認を約束（N-5）→
  プローブ結果で文面を切替
- コメントと実態のずれ（N-2）→ 優先度 45 は固定レーンに対する
  記述であることを明記（可変優先度候補が先行するのは意図的）
- ハンドラ層のサロゲート束縛死（敵対的 軽微-1）→ _dispatch 入口で
  text/callback data を一度だけ strip_surrogates
- 証拠ボディ上限が「文字数」で実質最大 4MB（敵対的 軽微-2）→
  バイト単位 1MB に是正

### 正直な残件（第5ブロック／全ブロック通算の最終残件）
- 仮想 Windows 検証の限界：この VM に実 Windows / Wine / cmd.exe は
  なく、winsim は Linux 上で静的・再現可能な範囲のみを覆う。
  実機での起動・Task Scheduler・Windows Python の証明は利用者の
  PC でのみ可能（従来どおり）。
- 保持期間（取得記録・イベント 180日／受信記録 90日／証拠 30日）と
  ディスク警告閾値 500MB は暫定デフォルト。変更可能。
- 初日から 0 件のソースは zero-item 異常を発火できず HEALTHY のまま
  （第2ブロックからの残件）。
- discogs_new_release の生産者不在：Discogs Master ポーリングは
  新規機能の判断事項であり修正対象外。REPRESS は announcement +
  variant_stock の 2 系統到達（第4ブロックからの残件）。
- seed に JAN データがないため editions.jan は空で、バーコード
  ゲート／バリアント機構はデータ登録まで休眠（第3ブロック残件）。
  edition_variants の生産者も不在。
- case5 の価格確信度「中」は golden set を回帰オラクルとして維持。
  仕様 C-5.3 の文言との緊張は利用者判断事項として残す。
- 同名タイトル橋渡しの限界（706 グループ／1,946 件＝51.5% は
  一意一致しない限り橋渡ししない）、「・」畳み衝突、同人締切
  00:00、締切 1 スロット モデル、未観測改名の不可視、過去言及の
  event_stage 誤分類（第4ブロック残件を継承）。
- duplicate_suspects の解消 UI は未実装（件数の表面化まで）。
  人間判断を取り消す UI も未実装（設計判断）。
- mall のパス型店舗（item.rakuten.co.jp/<shop> 等）は host 同一視で
  1 店舗に畳まれる。host=運営者プロキシの限界として記録。
- STRUCTURED 抽出の部分失敗（壊れた JSON-LD ブロック）は不在と
  区別できない（セラー F-5、全レーン共通の欠席意味論の範囲内）。
- import 後に昇格した twin の第二 seed が第二の pending 候補を
  生む順序が残る（マニアA 軽微）：二重カタログにはならず（承認時に
  一意タイトルで同一 Edition に合流）、可視の重複判定として記録。
- seed の起点イベント名がカタログと異なる場合、初回目撃が
  「再配布」として記録される（system_events のみの影響）。
- 候補承認 → Edition 化（AR-C・watch_level 0）は R18 の実装判断。
  既存 Edition へ一意に同定できる場合はリンクに refined 済み。
  利用者が veto 可能な明示事項として最終報告に含める。
- 第5ブロックで「修正しない」と記録したもの：卸 中-2（自ドメインと
  mall の同一運営者は複数カウントのまま＝意図的プロキシ）、
  軽微-3（source_urls の減衰なし）、コレクター 軽微-3（24h アラートと
  21:45 スイープの併発は設計通り）、マニアA 軽微-6（サークル・型番
  ともに無い seed は構造的に昇格不能）。
