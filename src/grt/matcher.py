"""Integrated matcher (agreements 02 / 03 / 10 / 20).

Pure core: :func:`evaluate` judges one (listing, edition[, variant]) pair
with the fixed score table, veto set, route caps and the barcode unified
gate (P1/P2/E1/E2). DB wrapper: :func:`match_listing` does blocking,
tie handling, audit logging (match_decisions), duplicate suspects and
review-queue routing. No automatic merging anywhere.
"""
from __future__ import annotations

import json

from . import clock
from .migrate import current_policy_version
from .normalize import (barcode_check_digit_ok, normalize_barcode,
                        normalize_catalog, normalize_title, title_similarity)

MATCHER_VERSION = "1.0.0"

HIGH, MID, LOW = "高", "中", "低"

NONRETAIL_TYPES = {
    "sample_mihon", "promo_not_for_sale", "bundle_bonus", "prize_campaign",
    "store_demo", "magazine_premium", "test_press", "doujin", "event_only",
    "unknown_nonretail",
}
PROMO_VARIANT_KINDS = {"promo_sample", "promo_not_for_sale", "radio_promo"}
PROMO_DIST_TYPES = {"sample_mihon", "promo_not_for_sale"}

SCORE_TABLE = {
    "barcode": 100, "catalog_exact": 100, "catalog_numeric": 40,
    "title_exact": 30, "title_similar": 15, "label": 10, "year": 10,
    "media": 10,
}

# --------------------------------------------------------------------------
# variant_completeness: prior definition (agreement-23, Phase 1)
#
# Agreement-23 requires the grant/revoke rules to be defined in advance in
# Phase 1 (the column is the premise of the P2 barcode gate), while the
# inventory job itself is Phase 2. The fragments below are exactly what
# spec v3 C-6.3 / prompt agreement-23 fix in writing; anything beyond them
# is listed under "undetermined" and must NOT be invented by code.
VARIANT_COMPLETENESS_VALUES = ("unknown", "single_confirmed", "multiple")
VARIANT_COMPLETENESS_DEFAULT = "unknown"
VARIANT_COMPLETENESS_RULES = {
    # An edition that merely *looks* single-variant is still unconfirmed.
    "default": "unknown (initial value; migration backfill without an "
               "evidence URL stays unknown)",
    # The one grant condition the spec fixes verbatim: exactly one variant
    # kind, confirmed through at least two independent source systems.
    "single_confirmed": "grant only when a single variant kind has been "
                        "confirmed via two or more independent systems; "
                        "never by inference from a single source",
    "no_speculative_grant": "speculative / bulk assignment is prohibited",
    # Spec v3 C-5.4 safety valve presupposes revocation: if a variant is
    # later separated and single_confirmed is revoked, decisions grounded
    # in gate (b) are deterministically re-judged, prices detached, and
    # the count reported in the daily digest. That consequence machinery
    # is specified; the revocation trigger itself is not (see below).
    "revocation_consequence": "re-judge (b)-route match_decisions, detach "
                              "prices, report the count in the digest",
    # NOT fixed by v3 / prompt v2 (round-9 item 9-A5) -- decisions needed:
    "undetermined": [
        "exact grant condition for 'multiple' (variant rows >= 2 alone, "
        "or human confirmation per variant)",
        "revocation trigger and target state (back to 'unknown' vs "
        "forward to 'multiple') when a second variant surfaces",
        "how independent 'systems' are counted for the two-system rule",
        "audit representation of a grant/revocation (no column for it "
        "exists in the section-5 schema)",
    ],
}


# --------------------------------------------------------------------------
# small helpers

def medium_of(fmt: str | None, media: str | None = None) -> str | None:
    for v in (media, fmt):
        if not v:
            continue
        f = str(v).upper()
        # W-check B3 (maniac B D-6): a single string naming BOTH a CD
        # and a vinyl (an "LP+CD" set) has no single primary medium;
        # returning whichever substring came first manufactured a
        # media_mismatch veto against plain-LP editions. Mixed sets
        # report None (unknown) and take neither points nor a veto.
        has_cd = "CD" in f
        has_vinyl = "VINYL" in f or "LP" in f or "インチ" in f \
            or "INCH" in f or "ピクチャー" in f
        if has_cd and has_vinyl:
            return None
        if has_cd:
            return "CD"
        if "CASSETTE" in f or "カセット" in f:
            return "CASSETTE"
        if has_vinyl:
            return "VINYL"
    return None


def format_class(fmt: str | None) -> str | None:
    """Fine format class, disc-count aware for LP (2xLP != LP)."""
    if not fmt:
        return None
    import re as _re
    f = str(fmt).upper()
    # R14 (AI-engineer F8): a mixed set ('LP+CD') classified as CD
    # here while medium_of deliberately returns None for the same
    # string, so the format veto fired on exactly the pairs the
    # medium axis had decided to leave alone. Mixed media has no
    # fine class either.
    if "CD" in f and any(m in f for m in ("LP", "VINYL", "レコード",
                                          "アナログ")):
        return None
    if "CD" in f:
        return "CD"
    # W-check B3 (AI-engineer 3 / maniac B D-6): the inch classes
    # used to trigger on a bare digit substring, so any format text
    # containing a year ('Vinyl, LP, Album (2017 Reissue)',
    # 'LP 1977') classified as 7INCH and vetoed an otherwise exact
    # match. An inch class now requires the digit to carry an inch
    # marker (", ″, INCH, インチ) — the forms real listings use.
    m = _re.search(r"(12|10|7)\s*(?:\"|″|”|INCH|インチ)", f)
    if m:
        return m.group(1) + "INCH"
    if "LP" in f or "ピクチャー" in f:
        m = _re.search(r"(\d+)\s*[X×]?\s*LP", f)
        return f"{m.group(1)}LP" if m else "1LP"
    return None


def detect_promo_clues(db, *texts: str | None) -> list[str]:
    """Return promo clue codes hit in the given texts (agreement-10)."""
    if db is None:
        clues = ["見本", "非売品", "白ラベル", "PROMO", "NOT FOR SALE", "店頭用"]
        codes = {"見本": "mihon", "非売品": "not_for_sale", "白ラベル": "white_label",
                 "PROMO": "promo", "NOT FOR SALE": "not_for_sale", "店頭用": "store_demo"}
        hay = " ".join(str(t) if t is not None else ""
                       for t in texts).upper()
        return sorted({codes[c] for c in clues if c.upper() in hay})
    rows = db.execute("SELECT clue, clue_code FROM promo_clues").fetchall()
    # R14 (adversarial): a JSON-LD "name": 123 reaches here as an
    # int; str() coercion keeps the clue scan from dying on it.
    hay = " ".join(str(t) if t is not None else ""
                   for t in texts).upper()
    return sorted({r[1] for r in rows if r[0].upper() in hay})


def _norm_label(v) -> str:
    return normalize_title(v)


def _int_or_none(v) -> int | None:
    """Parse an integer identifier (Discogs / VGMdb ID) or None.

    W-check B3 (AI-engineer 5 / adversarial 3 / maniac B D-13):
    scraped values arrive as unchecked strings ('12-345', a release
    URL, '1994年'); a bare int() raised ValueError out of evaluate /
    find_candidate_editions, and because register_ingested had no
    per-item containment, one dirty value silently killed the whole
    page batch at the same position on every run. Unparseable means
    "no identifier" — the signal is absent, never an exception.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    s = str(v).strip()
    # R14 (adversarial 中-5): '²'.isdigit() is True (Unicode "Number,
    # Other") but int('²') raises — the B3 invariant "unparseable
    # means no identifier, never an exception" had a surviving input
    # class. isdecimal() is exactly the set int() accepts.
    if s.isdecimal():
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _year_or_none(v) -> int | None:
    """Parse a year, accepting the Japanese '1994年' writing form."""
    n = _int_or_none(v)
    if n is not None:
        return n
    if isinstance(v, str):
        import re as _re
        m = _re.search(r"(19|20)\d{2}", v)
        if m:
            return int(m.group(0))
    return None


def _catalog_parts(norm: str | None):
    """Split a normalized catalog string into (prefix, digits,
    suffix) — 'KICA123B' -> ('KICA', '123', 'B'). None when the
    shape is not prefix+digits+optional suffix."""
    import re as _re
    if not norm:
        return None
    m = _re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", str(norm))
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# --------------------------------------------------------------------------
# pure evaluation core

def evaluate(listing: dict, edition: dict, variant: dict | None = None,
             promo_clue_codes: list[str] | None = None) -> dict:
    """Judge one pair. Returns score, breakdown, vetoes, the two-layer
    confidence and a disposition in {auto, review, candidate}."""
    L, E, V = listing, edition, variant or {}
    breakdown: dict[str, int] = {}
    vetoes: list[str] = []

    # --- normalization -----------------------------------------------------
    cat = normalize_catalog(L.get("catalog_raw") or L.get("catalog"))
    e_cat_norm = (E.get("catalog_norm") or normalize_catalog(E.get("catalog"))["norm"])
    catalog_exact = bool(cat["norm"]) and cat["norm"] == e_cat_norm
    catalog_numeric = (
        not catalog_exact and bool(cat["numeric"]) and len(cat["numeric"]) >= 3
        and cat["numeric"] == normalize_catalog(E.get("catalog"))["numeric"]
        if E.get("catalog") else False
    )
    if not catalog_exact and not catalog_numeric and cat["numeric"] and E.get("catalog_norm"):
        e_num = "".join(ch for ch in e_cat_norm if ch.isdigit())
        e_num = e_num.lstrip("0") or e_num  # same zero-strip as normalize
        catalog_numeric = len(cat["numeric"]) >= 3 and cat["numeric"] == e_num

    l_bar = normalize_barcode(L.get("barcode_raw") or L.get("barcode"))
    # W-check B3 (maniac B D-3 / AI-engineer 2b): the edition-side
    # barcode falls back to editions.jan. The editions table has no
    # 'barcode' column — its JAN lives in `jan` — and no code read
    # it, so an edition whose JAN was registered only at edition
    # level could never barcode-match at all.
    e_bar = normalize_barcode(V.get("barcode") or E.get("barcode")
                              or E.get("jan"))
    barcode_valid = bool(l_bar) and barcode_check_digit_ok(l_bar)
    barcode_match = barcode_valid and bool(e_bar) and l_bar == e_bar
    barcode_only = barcode_match and not catalog_exact

    title_exact = bool(normalize_title(L.get("raw_title") or L.get("title"))) and \
        normalize_title(L.get("raw_title") or L.get("title")) == normalize_title(E.get("title"))
    sim = title_similarity(L.get("raw_title") or L.get("title"), E.get("title"))

    # --- promo determination (bidirectional veto, agreement-10) ------------
    clue_codes = promo_clue_codes
    if clue_codes is None:
        clue_codes = detect_promo_clues(None, L.get("raw_title"), L.get("description"))
    listing_promo = bool(clue_codes) or cat["is_promo"] or \
        (L.get("distribution_type") in PROMO_DIST_TYPES)
    edition_promo_known = (
        V.get("variant_kind") in PROMO_VARIANT_KINDS
        or E.get("distribution_type") in PROMO_DIST_TYPES
        or E.get("distribution_type") == "retail"
        or V.get("variant_kind") == "retail"
    )
    edition_promo = (
        V.get("variant_kind") in PROMO_VARIANT_KINDS
        or E.get("distribution_type") in PROMO_DIST_TYPES
    )

    # --- score --------------------------------------------------------------
    if barcode_match:
        breakdown["barcode"] = SCORE_TABLE["barcode"]
    if catalog_exact:
        breakdown["catalog_exact"] = SCORE_TABLE["catalog_exact"]
    elif catalog_numeric:
        breakdown["catalog_numeric"] = SCORE_TABLE["catalog_numeric"]
    if title_exact:
        breakdown["title_exact"] = SCORE_TABLE["title_exact"]
    elif sim >= 0.90:
        breakdown["title_similar"] = SCORE_TABLE["title_similar"]
    if L.get("label") and E.get("label") and _norm_label(L["label"]) == _norm_label(E["label"]):
        breakdown["label"] = SCORE_TABLE["label"]
    if _year_or_none(L.get("year")) is not None \
            and _year_or_none(L.get("year")) == _year_or_none(E.get("year")):
        breakdown["year"] = SCORE_TABLE["year"]
    lf, ef = format_class(L.get("format")), format_class(E.get("format"))
    if lf and ef and lf == ef:
        breakdown["media"] = SCORE_TABLE["media"]
    # Spec C-12.2 (W-check B3, maniac B D-4): numeric part equal but
    # the catalog SUFFIX differs (KICA-123B against KICA-123A) is NOT
    # a veto — it is a review-bound DEMERIT. A suffix difference is
    # typically a different pressing, so the pair must not sail into
    # automatic price attachment on the strength of the other
    # signals; the demerit is sized like the other partial-match
    # signal (title_similar, 15) and is recorded in the breakdown so
    # the audit shows why the pair was held back.
    suffix_mismatch = False
    if not catalog_exact and catalog_numeric:
        lp = _catalog_parts(cat["norm"])
        ep = _catalog_parts(e_cat_norm)
        if lp and ep and lp[0] == ep[0] and lp[2] != ep[2]:
            breakdown["catalog_suffix_mismatch"] = -15
            suffix_mismatch = True
    score = sum(breakdown.values())

    # --- vetoes ---------------------------------------------------------------
    lm = medium_of(L.get("format"), L.get("media"))
    em = medium_of(E.get("format"), E.get("media"))
    if lm and em and lm != em:
        vetoes.append("media_mismatch")
    elif lf and ef and lf != ef:
        vetoes.append("format_mismatch")
    # An explicit format_detail contradiction (e.g. Mono vs Stereo,
    # 2LP set vs 1LP) is a format veto when both sides state it.
    ld, ed_ = L.get("format_detail"), E.get("format_detail")
    if ld and ed_ and str(ld).upper().replace(" ", "") != \
            str(ed_).upper().replace(" ", "") \
            and "format_mismatch" not in vetoes and "media_mismatch" not in vetoes:
        vetoes.append("format_mismatch")
    if L.get("country") and E.get("country") and \
            str(L["country"]).upper() != str(E["country"]).upper():
        vetoes.append("country_mismatch")
    if edition_promo_known and listing_promo != edition_promo:
        vetoes.append("promo_mismatch")
    if _int_or_none(L.get("discogs_id")) is not None \
            and _int_or_none(E.get("discogs_id")) is not None \
            and _int_or_none(L["discogs_id"]) != _int_or_none(E["discogs_id"]):
        vetoes.append("discogs_id_mismatch")
    # Title veto fires only on positive evidence: a missing listing title
    # is absence of evidence, not a contradiction.
    l_title_present = bool(normalize_title(L.get("raw_title") or L.get("title")))
    if l_title_present and not title_exact and sim < 0.35 \
            and normalize_title(E.get("title")):
        vetoes.append("title_mismatch")
    if L.get("parent_barcode_match"):
        vetoes.append("parent_product")

    # --- variant gate ---------------------------------------------------------
    disc_fields = ["variant_color", "retailer_exclusive", "pressing_note"]
    gate_a = False
    disc_conflict = False
    for f in disc_fields:
        lv, ev = L.get(f), V.get(f)
        if lv and ev:
            if str(lv).upper() == str(ev).upper():
                gate_a = True
            else:
                disc_conflict = True
    if _int_or_none(L.get("discogs_id")) is not None \
            and _int_or_none(V.get("discogs_id")) is not None:
        if _int_or_none(L["discogs_id"]) == _int_or_none(V["discogs_id"]):
            gate_a = True
        else:
            disc_conflict = True
    if disc_conflict:
        gate_a = False
    gate_b = E.get("variant_completeness") == "single_confirmed"
    variant_gate = (gate_a or gate_b) and not disc_conflict

    route = L.get("route") or "retail"
    dist_type = L.get("distribution_type") or E.get("distribution_type")
    retail_route = route in ("retail", "used", "auction", "discogs")
    morphology_ok = dist_type not in NONRETAIL_TYPES

    existence, price, disposition = LOW, LOW, "candidate"
    gate_case = None

    # --- doujin composite key (agreement-03) --------------------------------
    if L.get("circle") and E.get("circle"):
        circle_ok = normalize_title(L["circle"]) == normalize_title(E["circle"])
        extra = (
            (L.get("event") and E.get("event")
             and normalize_title(L["event"]) == normalize_title(E["event"]))
            or (_year_or_none(L.get("year")) is not None
                and _year_or_none(L.get("year")) == _year_or_none(E.get("year")))
            or (lf and ef and lf == ef)
        )
        # Spec C-5.6 (W-check B3, maniac B D-5): when BOTH sides name
        # an event and the events DIFFER, the two are different
        # distribution events of the same work — existence may still
        # aggregate, but the price must stay a separate series:
        # never price-高, never automatic; integration only via the
        # review queue. The branch used to not look at a conflicting
        # event at all when year or media supplied `extra`.
        event_conflict = bool(
            L.get("event") and E.get("event")
            and normalize_title(L["event"]) != normalize_title(E["event"]))
        if circle_ok and title_exact and extra and not vetoes:
            existence = HIGH
            if L.get("shop") and E.get("price_shop") and L["shop"] == E["price_shop"]:
                price = HIGH
                disposition = "auto"
            else:
                price = MID
                disposition = "review"
        elif circle_ok and title_exact:
            existence, price, disposition = MID, MID, "review"
        if event_conflict:
            if price == HIGH:
                price = MID
            disposition = "review"
        # W-check B3 (AI-engineer 6): this branch used to return
        # WITHOUT the route caps, so a GOODS-route or promo-clue
        # doujin pair kept 高/高/auto although C-5.3 caps those
        # routes for every other judgement path. The caps now apply
        # here exactly as on the gate and score paths.
        existence, price, disposition = _apply_caps(
            existence, price, disposition, route, clue_codes, dist_type)
        return _result(score, breakdown, vetoes, existence, price, disposition,
                       None, barcode_only, barcode_match,
                       variant_gate=variant_gate)

    # --- barcode unified gate (agreement-20) --------------------------------
    if barcode_match:
        if not morphology_ok:
            # Case 5: barcode is blocking-only for non-retail forms and
            # never advances the pair. Promo forms (sample / not-for-sale)
            # are forced into review per the promo rule (agreement-10);
            # other non-retail forms stop at candidate registration.
            # W-check B3 (AI-engineer 8) flagged MID here as an
            # inversion against E2's LOW, citing the C-5.3 cap
            # table ("non-retail barcode route must not advance
            # price judgement"). The counter-evidence is stronger:
            # the golden set — which C-5.4 itself names as this
            # gate's regression oracle — fixes case5 at MID in nine
            # cases (G071–G075, G092–G095), and MID never feeds
            # price evidence anywhere (pricedrop requires HIGH).
            # The golden set wins; the C-5.3 wording tension is
            # recorded in WCHECK_REPORT.md as an open item for the
            # user rather than silently resolved either way.
            existence, price = MID, MID
            disposition = "review" if dist_type in PROMO_DIST_TYPES else "candidate"
            gate_case = "case5"
        elif vetoes:
            existence, price, disposition = MID, MID, "review"
            gate_case = "case4"
        elif catalog_exact and variant_gate and retail_route:
            existence, price, disposition = HIGH, HIGH, "auto"
            gate_case = "P1"
        elif not catalog_exact and (L.get("barcode_tier") == 1) \
                and gate_b and not disc_conflict \
                and not suffix_mismatch and retail_route:
            # P2: the sole surviving solo-confirmation exception. Title
            # exactness neither promotes nor demotes here; E1 is defined
            # by variant_gate being false.
            # R14 (AI-engineer F2 / collector C): P2's premise is the
            # ABSENCE of corroborating material. Two contradictions
            # used to slip past it: a Discogs-ID conflict against the
            # variant (disc_conflict — P1's variant_gate excludes it,
            # so a pair with MORE material judged lower than a pair
            # with less), and a catalog suffix mismatch (C-12.2 —
            # material exists and disagrees). Either one demotes the
            # pair to the E1/E2 path below.
            existence, price, disposition = HIGH, HIGH, "auto"
            gate_case = "P2"
        elif catalog_exact or title_exact:
            existence, price, disposition = HIGH, MID, "review"
            gate_case = "E1"
        else:
            # E2: candidate registration only; no price confidence at all.
            existence, price, disposition = MID, LOW, "candidate"
            gate_case = "E2"
        existence, price, disposition = _apply_caps(
            existence, price, disposition, route, clue_codes, dist_type)
        return _result(score, breakdown, vetoes, existence, price, disposition,
                       gate_case, barcode_only, barcode_match,
                       variant_gate=variant_gate)

    # --- score path -----------------------------------------------------------
    if score < 60:
        # Below threshold: a discovery record only. Vetoes are recorded
        # but cannot promote a weak pair into the review band.
        existence, price, disposition = LOW, LOW, "candidate"
    elif vetoes:
        existence, price, disposition = MID, MID, "review"
    elif score >= 100:
        existence = HIGH
        if variant_gate and retail_route and morphology_ok and not clue_codes \
                and dist_type != "unknown":
            price, disposition = HIGH, "auto"
        else:
            price, disposition = MID, "review"
    else:  # 60..99
        existence, price, disposition = MID, MID, "review"

    existence, price, disposition = _apply_caps(
        existence, price, disposition, route, clue_codes, dist_type)

    return _result(score, breakdown, vetoes, existence, price, disposition,
                   gate_case, barcode_only, barcode_match,
                   variant_gate=variant_gate)


def _apply_caps(existence, price, disposition, route, clue_codes, dist_type):
    """Route caps (agreement-03), applied on every judgement path:
    GOODS route caps both axes at 'mid' and never auto-links a price;
    a promo clue stops price auto-attach and forces review;
    distribution_type='unknown' caps price at 'mid' until confirmed."""
    if route == "goods":
        if existence == HIGH:
            existence = MID
        if price == HIGH:
            price = MID
        if disposition == "auto":
            disposition = "review"
    if clue_codes:
        if price == HIGH:
            price = MID
        if disposition == "auto":
            disposition = "review"
    if dist_type == "unknown":
        if price == HIGH:
            price = MID
        if disposition == "auto":
            disposition = "review"
    return existence, price, disposition


def _result(score, breakdown, vetoes, existence, price, disposition,
            gate_case, barcode_only, barcode_match, variant_gate=False):
    return {
        "score": score,
        "breakdown": breakdown,
        "vetoes": vetoes,
        "existence_confidence": existence,
        "price_confidence": price,
        "disposition": disposition,
        "gate_case": gate_case,
        "barcode_only": 1 if barcode_only else 0,
        "barcode_match": barcode_match,
        # Whether the variant was actually DISCRIMINATED for this
        # judgement (gate_a/gate_b passed). Consumers that attribute
        # a listing to a variant (ingest write-back) must require
        # this — see the R14 collector finding in ingest.py.
        "variant_gate": bool(variant_gate),
    }


# --------------------------------------------------------------------------
# DB wrapper: blocking + tie handling + audit

def find_candidate_editions(db, listing: dict) -> list[dict]:
    """Blocking: collect edition rows sharing any blocking key.

    Keys (agreement-02): catalog full key, catalog numeric-part key,
    (normalized title + year), barcode, Discogs Release ID, VGMdb ID,
    and the doujin composite key (circle + title). Blocking only widens
    the funnel; every collected pair is still judged by evaluate().
    """
    seen: dict[str, dict] = {}

    def add(rows):
        for r in rows:
            d = dict(r)
            seen[d["id"]] = d

    cat = normalize_catalog(listing.get("catalog_raw") or listing.get("catalog"))
    if cat["norm"]:
        add(db.execute("SELECT * FROM editions WHERE catalog_norm=?",
                       (cat["norm"],)).fetchall())
    # Catalog numeric-part key (agreement-02): same digit run under a
    # different label prefix must still reach the evaluation core (the
    # core awards it catalog_numeric +40). The >=3 length floor mirrors
    # evaluate()'s own catalog_numeric guard so very short digit runs
    # (e.g. "12") cannot flood the candidate set.
    if cat["numeric"] and len(cat["numeric"]) >= 3:
        target = cat["numeric"]
        for r in db.execute("SELECT * FROM editions").fetchall():
            d = dict(r)
            if d["id"] in seen:
                continue
            e_num = "".join(ch for ch in (d.get("catalog_norm") or "")
                            if ch.isdigit())
            if not e_num and d.get("catalog"):
                e_num = normalize_catalog(d["catalog"])["numeric"]
            # R14 (AI-engineer F3): the edition side's raw digit run
            # kept its zero padding here while the listing side is
            # stripped by normalize_catalog — a padded edition
            # (KICA0123) never blocked for an unpadded listing even
            # though evaluate awards the pair catalog_numeric.
            e_num = e_num.lstrip("0") or e_num
            if e_num == target:
                seen[d["id"]] = d
    bar = normalize_barcode(listing.get("barcode_raw") or listing.get("barcode"))
    if bar:
        add(db.execute(
            "SELECT e.* FROM editions e JOIN edition_variants v"
            " ON v.edition_id=e.id WHERE v.barcode=?", (bar,)).fetchall())
        # Edition-level JAN (W-check B3, maniac B D-3): editions.jan
        # is a blocking key in its own right; stored values are
        # compared normalized, like the parent-product veto.
        for r in db.execute(
                "SELECT * FROM editions WHERE jan IS NOT NULL").fetchall():
            if normalize_barcode(r["jan"]) == bar:
                add([r])
    if _int_or_none(listing.get("discogs_id")) is not None:
        add(db.execute("SELECT * FROM editions WHERE discogs_id=?",
                       (_int_or_none(listing["discogs_id"]),)).fetchall())
    # VGMdb ID (agreement-02 blocking key; editions.vgmdb_id column).
    if _int_or_none(listing.get("vgmdb_id")) is not None:
        add(db.execute("SELECT * FROM editions WHERE vgmdb_id=?",
                       (_int_or_none(listing["vgmdb_id"]),)).fetchall())
    if listing.get("title") or listing.get("raw_title"):
        title = listing.get("title") or listing.get("raw_title")
        # W-check B3 (AI-engineer 2d): title blocking used to REQUIRE
        # a year, so an ingest item with a literally identical title
        # but no year field collected ZERO candidates and could never
        # match. An exact normalized-title equality is a legitimate
        # blocking key on its own; the year stays a scoring signal
        # inside evaluate().
        for r in db.execute("SELECT * FROM editions").fetchall():
            if r["id"] not in seen and \
                    normalize_title(r["title"]) == normalize_title(title):
                add([r])
    # Doujin composite key (circle + title, agreements-02/03): doujin
    # discs routinely carry no catalog number, so a circle-bearing
    # listing blocks on its normalized title without requiring a year.
    # NOTE: the section-5 editions schema has no circle/event column, so
    # the circle element cannot be verified against stored editions; the
    # title element is what blocking can collect here, and evaluate()'s
    # composite judgement applies whenever an edition dict carries the
    # circle (e.g. candidate-side records). Recorded as a schema limit,
    # not silently widened: title equality is exact after normalization.
    if listing.get("circle"):
        title = listing.get("raw_title") or listing.get("title")
        if title:
            for r in db.execute("SELECT * FROM editions").fetchall():
                if r["id"] not in seen and \
                        normalize_title(r["title"]) == normalize_title(title):
                    add([r])
    return list(seen.values())


# --------------------------------------------------------------------------
# duplicate suspects (agreement-02: collisions are never auto-merged;
# every collision is stacked into duplicate_suspects for human review)
#
# NOTE on kind values: the value domain of duplicate_suspects.kind is
# listed as undefined in prompt section 12 (round-9 item 9-D3). The three
# kinds below are a provisional prior use -- one per collision tier the
# spec itself defines -- pending ratification, in the same position as
# review_queue's 'match_review'. No other kinds may be added by guesswork.
DUPLICATE_KIND_EDITION = "edition_key_collision"
DUPLICATE_KIND_VARIANT = "variant_key_collision"
DUPLICATE_KIND_DISCOGS = "discogs_id_conflict"


def _record_duplicate_suspect(db, kind: str, key: str, payload: dict) -> bool:
    """Insert one unresolved suspect row; False if the same (kind, key)
    is already awaiting review (a suspect is a state, not an event log;
    the per-judgement event log is match_decisions)."""
    row = db.execute(
        "SELECT 1 FROM duplicate_suspects WHERE kind=? AND key=?"
        " AND resolved_at IS NULL", (kind, key)).fetchone()
    if row:
        return False
    db.execute(
        "INSERT INTO duplicate_suspects(kind, key, payload_json, created_at)"
        " VALUES(?, ?, ?, ?)",
        (kind, key, json.dumps(payload, ensure_ascii=False, default=str),
         clock.now_iso(db)))
    return True


def _stack_duplicate_suspects(db, listing: dict, listing_id,
                              candidates: list[dict],
                              results: list[dict]) -> None:
    """Detect the collision cases agreement-02 names and stack them.

    Edition tier: (catalog_norm, format, country) shared by >= 2 distinct
    editions among the blocked candidates. Variant tier:
    (edition_id, variant_kind, discogs_id) shared by >= 2 variant rows.
    Discogs tier: the evaluation core returned discogs_id_mismatch --
    differing Discogs Release IDs are never auto-merged.
    """
    by_key: dict[tuple, list[str]] = {}
    for ed in candidates:
        if not ed.get("catalog_norm"):
            continue
        k = (ed["catalog_norm"], ed.get("format"), ed.get("country"))
        by_key.setdefault(k, []).append(ed["id"])
    for (cn, fmt, ctry), ids in by_key.items():
        uniq = sorted(set(ids))
        if len(uniq) > 1:
            _record_duplicate_suspect(
                db, DUPLICATE_KIND_EDITION, f"{cn}|{fmt or ''}|{ctry or ''}",
                {"edition_ids": uniq, "listing_id": listing_id,
                 "catalog_norm": cn, "format": fmt, "country": ctry,
                 "matcher_version": MATCHER_VERSION})
    for ed in candidates:
        vrows = db.execute(
            "SELECT id, variant_kind, discogs_id FROM edition_variants"
            " WHERE edition_id=? AND discogs_id IS NOT NULL",
            (ed["id"],)).fetchall()
        vgroups: dict[tuple, list[int]] = {}
        for vr in vrows:
            vgroups.setdefault((vr["variant_kind"], vr["discogs_id"]),
                               []).append(vr["id"])
        for (vk, did), vids in vgroups.items():
            if len(vids) > 1:
                _record_duplicate_suspect(
                    db, DUPLICATE_KIND_VARIANT, f"{ed['id']}|{vk}|{did}",
                    {"edition_id": ed["id"], "variant_ids": sorted(vids),
                     "variant_kind": vk, "discogs_id": did,
                     "listing_id": listing_id,
                     "matcher_version": MATCHER_VERSION})
    editions_by_id = {ed["id"]: ed for ed in candidates}
    for r in results:
        if "discogs_id_mismatch" in r["vetoes"]:
            ed = editions_by_id.get(r["edition_id"], {})
            _record_duplicate_suspect(
                db, DUPLICATE_KIND_DISCOGS,
                f"{listing.get('discogs_id')}|{r['edition_id']}",
                {"edition_id": r["edition_id"], "listing_id": listing_id,
                 "listing_discogs_id": listing.get("discogs_id"),
                 "edition_discogs_id": ed.get("discogs_id"),
                 "matcher_version": MATCHER_VERSION})


def match_listing(db, listing: dict, listing_id: int | None = None) -> list[dict]:
    """Evaluate a listing against all blocking candidates; log decisions.

    Ties at the top score are never auto-decided: every tied candidate is
    routed to the review queue (deterministic 'judgement withheld').

    Transaction contract (R14, adversarial QA): this function commits
    its own decisions at its end (and on the parent-veto early return)
    but does NOT roll back on an internal failure — a caller wrapping
    several operations in one unit of work must roll back itself when
    match_listing raises, or partial decisions can be persisted by a
    later commit. register_ingested (the only production caller) does
    exactly that per item.
    """
    # Agreement-26 (Phase 2 acceptance #7): a listing whose barcode
    # belongs to a PARENT product (the game box, a bundle, ...) is
    # vetoed outright — it never reaches scoring, so a parent
    # product's price can never be recorded as the disc's price.
    # Review fix (collector): the veto must compare NORMALIZED
    # barcodes and accept the barcode_raw key like every other
    # matcher path — a hyphenated JAN or a raw-key listing used to
    # slip straight past the veto into scoring.
    barcode = normalize_barcode(
        listing.get("barcode_raw") or listing.get("barcode"))
    if barcode:
        parent = None
        for prow in db.execute(
                "SELECT id, parent_barcode FROM parent_products"
        ).fetchall():
            if normalize_barcode(prow[1]) == barcode:
                parent = prow
                break
        if parent is not None:
            pv0 = current_policy_version(db)
            db.execute(
                """INSERT INTO match_decisions(
                     listing_id, edition_id, score, score_breakdown_json,
                     veto, existence_confidence, price_confidence,
                     barcode_only, barcode_source_tier, matcher_version,
                     policy_version, decided_at)
                   VALUES(?, NULL, 0, '{}', 'parent_product', '低', '低',
                          0, ?, ?, ?, ?)""",
                (listing_id, listing.get("barcode_source_tier"),
                 MATCHER_VERSION, pv0, clock.now_iso(db)))
            db.commit()
            return [{"edition_id": None, "score": 0, "breakdown": {},
                     "vetoes": ["parent_product"],
                     "existence_confidence": "低",
                     "price_confidence": "低",
                     "disposition": "veto", "barcode_only": 0}]

    # W-check B3 (AI-engineer 2c): the barcode gate's P2 tier reads
    # listing["barcode_tier"] as an int, but production sources speak
    # barcode_source_tier ('structured' / 'free_text' / 'unknown').
    # Derive the tier here, once: structured data (a GTIN published
    # in the page's own structured markup / an API field) is tier 1;
    # a barcode recovered from free text is tier 2. Without this
    # mapping P2 was reachable only in golden fixtures.
    listing = dict(listing)
    if listing.get("barcode_tier") is None:
        _tier = {"structured": 1, "free_text": 2}.get(
            str(listing.get("barcode_source_tier") or ""))
        if _tier is not None:
            listing["barcode_tier"] = _tier

    candidates = find_candidate_editions(db, listing)
    clue_codes = detect_promo_clues(
        db, listing.get("raw_title"), listing.get("description"))
    results = []
    for ed in candidates:
        # W-check B3 (C1: seller D9 / maniac B D-1 / AI-engineer 1):
        # the pair used to be judged against ONE variant row taken
        # with fetchone() and no ORDER BY — the verdict (score,
        # vetoes, gate) depended on the physical insertion order of
        # the variant rows. Every variant is now evaluated and the
        # best judgement wins deterministically: highest score, then
        # a barcode match, then fewest vetoes, then lowest variant
        # id. The winning variant is recorded on the result (and in
        # match_decisions.variant_id) so the audit can show WHICH
        # variant the judgement is about.
        vrows = db.execute(
            "SELECT * FROM edition_variants WHERE edition_id=?"
            " ORDER BY id", (ed["id"],)).fetchall()
        variants = [dict(v) for v in vrows] or [None]
        best = None
        for variant in variants:
            res = evaluate(listing, ed, variant,
                           promo_clue_codes=clue_codes)
            res["edition_id"] = ed["id"]
            res["variant_id"] = variant["id"] if variant else None
            # R14 (AI-engineer F9): the winner key used to stop at
            # (score, barcode, vetoes), so two variants whose
            # judgements differed in CONFIDENCE (P1 vs E1 at the same
            # score) were decided by smallest row id — re-registering
            # the variants flipped whether price 高 was granted on
            # identical data. The better judgement now wins before
            # the id tiebreak is ever reached.
            _conf = {"高": 2, "中": 1, "低": 0}
            _disp = {"auto": 2, "review": 1, "candidate": 0}
            key = (res["score"], 1 if res["barcode_match"] else 0,
                   -len(res["vetoes"]),
                   _conf.get(res["price_confidence"], 0),
                   _conf.get(res["existence_confidence"], 0),
                   _disp.get(res["disposition"], 0),
                   -(variant["id"] if variant else 0))
            if best is None or key > best[0]:
                best = (key, res)
        results.append(best[1])

    if results:
        top = max(r["score"] for r in results)
        tied = [r for r in results if r["score"] == top]
        if len(tied) > 1 and top >= 60:
            # Tie at the top: judgement is withheld for every tied
            # candidate (never auto-decided); all go to the review queue
            # at 'mid' confidence (agreement-02).
            for r in tied:
                r["disposition"] = "review"
                if r["existence_confidence"] == "高":
                    r["existence_confidence"] = "中"
                if r["price_confidence"] == "高":
                    r["price_confidence"] = "中"
                r["vetoes"] = r["vetoes"] + ["tie_withheld"]

    # Collisions surfaced by this judgement are stacked for human review
    # (agreement-02); nothing is auto-merged here or anywhere else.
    _stack_duplicate_suspects(db, listing, listing_id, candidates, results)

    # R19 (maniac B / AI engineer): a HUMAN adjudication
    # (matcher_version='human-review-1') is the latest word on a
    # listing until its identity actually changes. Re-matching used
    # to insert a fresh decision every cycle — demoting an
    # approved price-高 back to the matcher's 中 within one fetch
    # cycle (the B4 consumer's whole effect evaporated) — and to
    # re-queue APPROVED items forever, while REJECTED items were
    # suppressed: one stray tap on the re-asked question silently
    # revoked the approval.
    # W-check B5 (maniac B F-6a/F-6b): the R19 guard keyed on the
    # listing's latest decision OVERALL, which both froze every
    # OTHER edition evaluated in the same run (the early return
    # discarded their decisions and queue rows too — an indefinite
    # cohabitation freeze) and failed to protect the human pair
    # whenever an intervening run's machine decision for a
    # different edition became the listing's latest (the approval
    # was silently demoted on the next co-evaluation). Human
    # adjudication is per (listing, edition) PAIR: exactly the
    # pairs whose OWN latest decision is human are held — no new
    # decision, no new queue row for that pair — and every other
    # pair proceeds normally.
    human_held: set = set()
    if listing_id is not None and results:
        for r in results:
            row = db.execute(
                "SELECT matcher_version FROM match_decisions"
                " WHERE listing_id=? AND edition_id=?"
                " ORDER BY id DESC LIMIT 1",
                (listing_id, r["edition_id"])).fetchone()
            if row is not None and row[0] == "human-review-1":
                human_held.add(r["edition_id"])

    pv = current_policy_version(db)
    for r in results:
        if r["edition_id"] in human_held:
            continue
        cur = db.execute(
            """INSERT INTO match_decisions(
                 listing_id, edition_id, variant_id, score,
                 score_breakdown_json, veto,
                 existence_confidence, price_confidence, barcode_only,
                 barcode_source_tier, matcher_version, policy_version, decided_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, r["edition_id"], r.get("variant_id"), r["score"],
             json.dumps(r["breakdown"], ensure_ascii=False),
             ",".join(r["vetoes"]) or None,
             r["existence_confidence"], r["price_confidence"], r["barcode_only"],
             listing.get("barcode_source_tier"), MATCHER_VERSION, pv,
             clock.now_iso(db)),
        )
        r["decision_id"] = cur.lastrowid
        if r["disposition"] in ("review", "candidate"):
            fingerprint = f"{listing.get('url', '')}|{r['edition_id']}"
            # W-check B2 (seller D-2): re-matching the same listing
            # stacked an identical UNRESOLVED review row every time —
            # the human queue filled with copies of itself on days
            # with no new arrivals at all. duplicate_suspects already
            # deduped this way; match_review now does too.
            # W-check B4 (maniac B): a row the human REJECTED also
            # suppresses re-queueing for the same fingerprint — the
            # listing's data has not changed (same fingerprint), so
            # re-asking is pure noise. (R19: APPROVED rows are now
            # covered by the human-decision guard above — a
            # re-match that still evaluates the approved edition
            # never reaches this loop at all.)
            already = db.execute(
                "SELECT 1 FROM review_queue WHERE kind='match_review'"
                " AND fingerprint=? AND (resolved_at IS NULL"
                " OR resolution='rejected')",
                (fingerprint,)).fetchone()
            if already is None:
                # W-check B5 (maniac B F-2): match_review rows used
                # to enter at priority 0 — below every candidate
                # lane (fixed 30-40, JP-boosted higher) — and this
                # kind has NO outbox notification path, so under
                # continuous candidate inflow the price-evidence
                # gate sank to the bottom of /queue indefinitely.
                # 45 puts a listing review above the FIXED
                # candidate lanes (30-40): it gates money decisions
                # on items already in the catalogue. (R24, maniac B
                # N-2: variable-priority candidates are
                # policy-scored up to 90 and may still precede it —
                # that ordering is deliberate: a 90-point candidate
                # is a strong new-discovery signal, and the fixed
                # lanes' unbounded inflow was the starvation being
                # fixed, not candidate priority as such.)
                db.execute(
                    """INSERT INTO review_queue(kind, payload_json, fingerprint,
                         priority, created_at)
                       VALUES('match_review', ?, ?, ?, ?)""",
                    (json.dumps({"listing": listing, "result": r},
                                ensure_ascii=False, default=str),
                     fingerprint, 45, clock.now_iso(db)),
                )
    db.commit()
    return results
