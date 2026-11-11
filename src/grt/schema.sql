-- Game Record Tool schema (design v3 C-15 final form, per implementation
-- prompt section 5). All statements are idempotent (IF NOT EXISTS).
-- QC round-4 fix: primary-key / unique identity columns carry explicit
-- NOT NULL. In SQLite a TEXT PRIMARY KEY does NOT imply NOT NULL (rowid
-- table quirk), so without it NULL identity rows were silently accepted
-- and could accumulate invisibly to id lookups. No REFERENCES clauses are
-- declared, matching the C-15 model form; referential integrity between
-- tables is enforced at the application layer by design.

CREATE TABLE IF NOT EXISTS editions(
  id TEXT NOT NULL PRIMARY KEY,
  title TEXT, catalog TEXT,
  catalog_norm TEXT, label TEXT, year INTEGER, format TEXT, country TEXT,
  game TEXT, rarity TEXT,
  discogs_id INTEGER, vgmdb_id INTEGER,
  jan TEXT, jan_status TEXT,
  music_type TEXT,
  distribution_type TEXT,
  distribution_type_source_url TEXT,
  distribution_channel TEXT,
  channel_source_url TEXT,
  variant_completeness TEXT,
  watch_level INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edition_variants(
  id INTEGER PRIMARY KEY, edition_id TEXT,
  discogs_id INTEGER, variant_kind TEXT,
  obi TEXT, sticker TEXT, disc_color TEXT, variant_color TEXT,
  numbering TEXT, limited_qty_text TEXT, retailer_exclusive TEXT,
  pressing_note TEXT, barcode TEXT, barcode_status TEXT,
  distribution_type TEXT,
  distinguishing_marks TEXT, evidence_url TEXT, status TEXT
);

CREATE TABLE IF NOT EXISTS candidate_editions(
  id INTEGER PRIMARY KEY, title TEXT, catalog_norm TEXT,
  raw_title TEXT, raw_description TEXT,
  source_url TEXT, source_urls_json TEXT, fingerprint TEXT UNIQUE,
  distribution_type TEXT, distribution_channel TEXT, channel_source_url TEXT,
  barcode TEXT, barcode_status TEXT,
  existence_confidence TEXT, price_confidence TEXT,
  found_at TEXT, first_seen TEXT, last_seen TEXT, found_via TEXT,
  variant_hints_json TEXT,
  status TEXT,
  reject_reason TEXT,
  promoted_edition_id TEXT
);

CREATE TABLE IF NOT EXISTS listings(
  id INTEGER PRIMARY KEY, edition_id TEXT, candidate_id INTEGER, variant_id INTEGER,
  physical_copy_id INTEGER,
  source TEXT, url TEXT, price REAL, currency TEXT,
  total_jpy REAL, ships_to_jp TEXT,
  media_cond TEXT, sleeve_cond TEXT, cond_rank INTEGER, obi_flag INTEGER,
  barcode_source_tier TEXT,
  status TEXT,
  absent_streak INTEGER DEFAULT 0,
  oos_declared INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, raw_title TEXT
);

CREATE TABLE IF NOT EXISTS listing_copy_groups(
  id INTEGER PRIMARY KEY, edition_id TEXT, variant_id INTEGER NULL,
  seller_group TEXT, first_seen TEXT, cond_rank INTEGER
);

CREATE TABLE IF NOT EXISTS seller_groups(
  seller_group TEXT, source TEXT, storefront TEXT, confirmed_url TEXT,
  policy_version INTEGER
);

CREATE TABLE IF NOT EXISTS price_snapshots(
  edition_id TEXT, variant_id INTEGER, source TEXT, taken_at TEXT,
  prev_success_at TEXT,
  num_for_sale INTEGER, lowest_price REAL, currency TEXT,
  shipping_jpy REAL, fx_rate REAL, total_jpy REAL
);

CREATE TABLE IF NOT EXISTS variant_stock(
  variant_id INTEGER, source TEXT, storefront TEXT, state TEXT,
  changed_at TEXT, absent_streak INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS announcements(
  id INTEGER PRIMARY KEY, source TEXT, url TEXT, title_raw TEXT, label TEXT,
  event_stage TEXT,
  found_at TEXT, candidate_id INTEGER
);

CREATE TABLE IF NOT EXISTS release_schedule(
  id INTEGER PRIMARY KEY, edition_id TEXT, candidate_id INTEGER, channel TEXT,
  deadline_raw TEXT, deadline_tz TEXT, deadline_jst TEXT,
  deadline_confidence TEXT,
  deadline_kind TEXT,
  deadline_kind_source_url TEXT,
  superseded_by INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS deadline_history(
  id INTEGER PRIMARY KEY, schedule_id INTEGER, old_value_json TEXT,
  changed_at TEXT, changed_via TEXT
);

CREATE TABLE IF NOT EXISTS parent_products(
  id INTEGER PRIMARY KEY, parent_title TEXT, parent_barcode TEXT,
  parent_source_url TEXT, edition_id TEXT, variant_id INTEGER,
  link_type TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS wantlist(
  id INTEGER PRIMARY KEY, edition_id TEXT, variant_id INTEGER,
  want_obi TEXT,
  want_condition_min TEXT,
  want_sealed INTEGER,
  max_price_jpy REAL, priority INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS catalog_prefix_ledger(
  prefix TEXT NOT NULL, number INTEGER NOT NULL, state TEXT,
  checked_at TEXT, suffix_rule_version INTEGER, note TEXT,
  suffix_probe_json TEXT,
  PRIMARY KEY (prefix, number)
);

CREATE TABLE IF NOT EXISTS catalog_suffix_rules(
  id INTEGER PRIMARY KEY, suffix TEXT, base_edition_id TEXT,
  evidence_url TEXT, approved INTEGER, policy_version INTEGER
);

CREATE TABLE IF NOT EXISTS paper_coverage(
  id INTEGER PRIMARY KEY, medium_kind TEXT, medium_name TEXT, issue_label TEXT,
  checked_at TEXT, check_route TEXT, result TEXT, found_candidate_ids TEXT
);

CREATE TABLE IF NOT EXISTS watch_rules(
  id INTEGER PRIMARY KEY, source TEXT, query TEXT, fetch_mode TEXT,
  interval_min INTEGER, enabled INTEGER, last_run TEXT,
  last_status TEXT
);

CREATE TABLE IF NOT EXISTS fetch_runs(
  id INTEGER PRIMARY KEY, source_id INTEGER, watch_rule_id INTEGER,
  started_at TEXT, finished_at TEXT, http_status INTEGER,
  items_extracted INTEGER, result TEXT,
  error_detail TEXT, raw_hash TEXT, fetcher_kind TEXT,
  normalized_hash TEXT, body_path TEXT
);

CREATE TABLE IF NOT EXISTS source_health(
  source_id INTEGER PRIMARY KEY, state TEXT,
  consecutive_failures INTEGER, last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS blocked_sources(
  source TEXT, first_blocked_at TEXT, last_blocked_at TEXT,
  consecutive_count INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS observation_gaps(
  id INTEGER PRIMARY KEY, scope TEXT, gap_start TEXT, gap_end TEXT, declared_at TEXT
);

CREATE TABLE IF NOT EXISTS heartbeat(
  id INTEGER PRIMARY KEY CHECK(id=1), alive_at TEXT, last_job_at TEXT, boot_at TEXT
);

CREATE TABLE IF NOT EXISTS system_events(
  id INTEGER PRIMARY KEY, kind TEXT, detail_json TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_outbox(
  id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  edition_id TEXT, event_type TEXT, body TEXT,
  priority_score INTEGER, floor_applied INTEGER DEFAULT 0,
  not_before TEXT, state TEXT,
  attempts INTEGER DEFAULT 0, next_attempt_at TEXT,
  quiet_override INTEGER DEFAULT 0, quiet_override_reason TEXT,
  last_notified_price REAL, policy_version INTEGER,
  sent_at TEXT, last_error TEXT, reply_markup_json TEXT
);

CREATE TABLE IF NOT EXISTS telegram_updates(
  update_id INTEGER PRIMARY KEY, received_at TEXT, handled_at TEXT, action TEXT
);

CREATE TABLE IF NOT EXISTS review_queue(
  id INTEGER PRIMARY KEY, kind TEXT, payload_json TEXT, fingerprint TEXT,
  priority INTEGER, created_at TEXT, resolved_at TEXT, resolution TEXT
);

CREATE TABLE IF NOT EXISTS duplicate_suspects(
  id INTEGER PRIMARY KEY, kind TEXT, key TEXT, payload_json TEXT,
  created_at TEXT, resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS match_decisions(
  id INTEGER PRIMARY KEY, listing_id INTEGER, edition_id TEXT,
  variant_id INTEGER,
  score INTEGER, score_breakdown_json TEXT, veto TEXT,
  existence_confidence TEXT, price_confidence TEXT, barcode_only INTEGER DEFAULT 0,
  barcode_source_tier TEXT,
  matcher_version TEXT, policy_version INTEGER, decided_at TEXT
);

CREATE TABLE IF NOT EXISTS policy_versions(
  id INTEGER PRIMARY KEY, version_no INTEGER, changed_at TEXT,
  changed_via TEXT, diff_json TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT NOT NULL PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, name TEXT, url TEXT, country TEXT,
  kind TEXT, platform TEXT,
  catalog_search INTEGER, has_ingest_list TEXT, fetch_mode TEXT,
  barcode_field_tier INTEGER, end_time_reliable INTEGER DEFAULT 0,
  expected_cadence_days REAL, status TEXT, discovered_via TEXT,
  found_at TEXT, last_checked TEXT,
  yield_listings INTEGER, yield_candidates INTEGER, yield_prices INTEGER
);

-- Supplementary dictionary tables (section 5 notes).
CREATE TABLE IF NOT EXISTS catalog_prefix_dict(
  prefix TEXT NOT NULL PRIMARY KEY, label TEXT
);

CREATE TABLE IF NOT EXISTS priority_catalog_prefixes(
  prefix TEXT NOT NULL PRIMARY KEY
);

-- Condition dictionary: per-source raw wording -> normalized condition.
-- cond_rank mapping is defined by the spec (M=5, EX=4, VG+=3, VG=2, G=1).
CREATE TABLE IF NOT EXISTS condition_dict(
  source TEXT NOT NULL, raw_pattern TEXT NOT NULL, normalized TEXT,
  field TEXT NOT NULL,
  PRIMARY KEY (source, raw_pattern, field)
);

-- Promo clue dictionary (spec section 4, agreement-10 lists the clue words;
-- vocabulary marked as spec-derived seed, extendable after first-hand
-- confirmation per section 12).
CREATE TABLE IF NOT EXISTS promo_clues(
  clue TEXT NOT NULL PRIMARY KEY, clue_code TEXT, source_note TEXT
);

-- Deadline-kind dictionary: left EMPTY on purpose. The concrete vocabulary
-- must be confirmed from real pages at implementation time (section 12);
-- unmatched text stays deadline_kind='unknown' for human assignment.
CREATE TABLE IF NOT EXISTS deadline_kind_dict(
  pattern TEXT NOT NULL PRIMARY KEY, kind TEXT, source_url TEXT
);

-- Announcement stage keyword dictionary (spec agreement-13 lists the
-- keyword examples; seeded from the spec text).
CREATE TABLE IF NOT EXISTS stage_keywords(
  keyword TEXT NOT NULL PRIMARY KEY, stage TEXT
);

-- Physical copy groups require listing membership; kept explicit.
CREATE TABLE IF NOT EXISTS listing_copy_members(
  group_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
  PRIMARY KEY (group_id, listing_id)
);

-- Phase 2/3 operational tables (added v2.0; CREATE IF NOT EXISTS so
-- existing installs gain them on the next migrate).
CREATE TABLE IF NOT EXISTS event_catalog(
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT,
  starts_on TEXT, ends_on TEXT, venue TEXT, url TEXT,
  harvest_from TEXT, harvest_to TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS repress_signals(
  id INTEGER PRIMARY KEY, edition_id TEXT, candidate_id INTEGER,
  system TEXT, detail TEXT, at TEXT,
  UNIQUE(edition_id, candidate_id, system, at)
);
CREATE TABLE IF NOT EXISTS store_page_state(
  source_id INTEGER PRIMARY KEY, last_hash TEXT,
  last_checked TEXT, last_changed TEXT
);
CREATE TABLE IF NOT EXISTS freq_state(
  source_id INTEGER PRIMARY KEY, tier_min INTEGER,
  consec_fail INTEGER DEFAULT 0, consec_ok INTEGER DEFAULT 0,
  demoted INTEGER DEFAULT 0, updated_at TEXT
);
