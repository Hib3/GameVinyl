"""Deterministic normalization and comparison keys (agreement-02).

The step order is fixed by the spec:
  1. Unicode NFKC
  2. Uppercase, full-width -> half-width
  3. Hyphen variants unified to '-', plus a hyphen-stripped comparison key
  4. Spaces / periods removed comparison key
  5. Label-prefix stripping only for prefixes registered in
     ``catalog_prefix_dict`` (both stripped and unstripped keys are produced).
     The dual-key mechanism lives in :func:`normalize_catalog_keys`; it is
     inert until the dictionary is seeded. The dictionary's contents are
     NOT yet determined (no prefix->label list or derivation rule is fixed
     by the spec / prompt, and the seed data does not determine one), so
     ``catalog_prefix_dict`` stays empty and production call sites keep
     using :func:`normalize_catalog` (round-9 fix note, 9-A4(b)(d)).
  6. Promo wording is separated out of the catalog string into a flag
  7. Barcodes are digits-only; only check-digit-valid values may feed
     ``barcode_match``

Implementation note (W-check B3, AI-engineer item 9): the promo
separation (step 6) is applied to the RAW string at the START of
:func:`normalize_catalog`, before hyphen unification and space
removal — after those steps, multi-word promo phrases such as
'NOT FOR SALE' could no longer be matched at all. The numbered list
above is the logical pipeline; the physical order inside
``normalize_catalog`` (promo first) is the functionally correct one
and must not be "fixed" back to the documented order.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_HYPHENS = "-‐−–—〜~－"
_PROMO_WORDS = ("見本盤", "見本", "非売品", "プロモ", "PROMO", "NOT FOR SALE",
                "NOTFOR SALE", "白ラベル", "店頭用", "SAMPLE", "サンプル")


def nfkc_upper(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    return t.upper()


def _unify_hyphens(t: str) -> str:
    for h in _HYPHENS:
        t = t.replace(h, "-")
    return t


def split_promo(text: str | None) -> tuple[str, bool]:
    """Remove promo wording from a catalog string; report whether it hit."""
    t = nfkc_upper(text)
    hit = False
    for w in _PROMO_WORDS:
        if w in t:
            hit = True
            t = t.replace(w, " ")
    return t, hit


def normalize_catalog(raw: str | None) -> dict:
    """Return comparison keys for a catalog string.

    Keys: ``norm`` (hyphens unified, spaces/periods removed),
    ``numeric`` (digit-run only), ``is_promo`` flag.
    """
    t, is_promo = split_promo(raw)
    t = _unify_hyphens(t)
    norm = re.sub(r"[\s.\-]", "", t)
    digits = re.sub(r"\D", "", t)
    # W-check B3 (maniac B D-15): zero-padding is a writing variant,
    # not a different number — KICA-0123 and KICA-123 are the same
    # catalog numeric part. Leading zeros are stripped so both the
    # exact-numeric comparison and the blocking key see one value.
    digits = digits.lstrip("0") or digits
    return {"norm": norm, "numeric": digits, "is_promo": is_promo, "raw": raw or ""}


def catalog_prefix_of(catalog_norm: str | None,
                      prefixes) -> str | None:
    """Longest registered label prefix that ``catalog_norm`` starts with.

    ``prefixes`` is any iterable of registered prefixes (the rows of
    ``catalog_prefix_dict``). Returns None when nothing matches. Pure and
    deterministic; with an empty registry it always returns None, which
    is exactly why step 5 cannot fire while the dictionary is unseeded.
    """
    if not catalog_norm:
        return None
    best: str | None = None
    for p in prefixes or ():
        p = str(p).upper()
        if p and catalog_norm.startswith(p) and len(catalog_norm) > len(p):
            if best is None or len(p) > len(best):
                best = p
    return best


def normalize_catalog_keys(raw: str | None, prefixes=()) -> dict:
    """Step-5 dual keys: everything :func:`normalize_catalog` returns,
    plus ``stripped`` (the key with a registered label prefix removed)
    and ``matched_prefix``. ``stripped`` is None when no registered
    prefix matches, so callers must fall back to ``norm`` alone."""
    base = normalize_catalog(raw)
    matched = catalog_prefix_of(base["norm"], prefixes)
    stripped = base["norm"][len(matched):] if matched else None
    return {**base, "stripped": stripped, "matched_prefix": matched}


def normalize_title(raw: str | None) -> str:
    t = _unify_hyphens(nfkc_upper(raw))
    return re.sub(r"[\s.\-]", "", t)


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def normalize_barcode(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"\D", "", nfkc_upper(raw))


def barcode_check_digit_ok(barcode: str | None) -> bool:
    """JAN/EAN-13 modulo-10 check. UPC-12 is also accepted (same scheme)."""
    b = normalize_barcode(barcode)
    if len(b) not in (12, 13) or not b.isdigit():
        return False
    digits = [int(c) for c in b]
    check = digits[-1]
    body = digits[:-1]
    # EAN/UPC: positions counted from the right of the full code; the digit
    # immediately left of the check digit has weight 3.
    total = 0
    for i, d in enumerate(reversed(body)):
        total += d * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10 == check


def fingerprint_for(title: str | None, catalog_norm: str | None,
                    circle: str | None = None) -> str:
    """Stable fingerprint used to aggregate the same candidate across sources."""
    # W-check B4 (wholesaler): the middle dot is the most common
    # Japanese title variant of all (オリジナル・サウンドトラック
    # vs オリジナルサウンドトラック) and normalize_title does not
    # fold it — two spellings of one work became two candidates
    # with split sources. The fold lives HERE, not in
    # normalize_title: the title normalisation is shared with the
    # matcher (agreement-02) and must not move under it.
    base = normalize_title(title).replace("・", "")
    # W-check B5 (wholesaler): pure sales-state decorations at the
    # HEAD of a shop headline (【予約】, [予約受付中], 【送料無料】
    # ...) split one product into multiple candidates across
    # lanes, defeating source aggregation. Only an unambiguous
    # decoration vocabulary is stripped, and only at the head —
    # edition-bearing words （限定/特典盤/再プレス盤) are never
    # touched. Like the ・ fold, this lives in the fingerprint,
    # not in normalize_title.
    import re as _re
    _deco = _re.compile(
        r"^[\s　]*[\[【［](予約受付中|予約受付|早期予約|先行予約|"
        r"予約|送料無料|"
        r"ポイントアップ|クーポン対象)[\]】］][\s　]*")
    while True:
        stripped = _deco.sub("", base)
        if stripped == base:
            break
        base = stripped
    if base.startswith("予約受付中"):
        base = base[len("予約受付中"):].lstrip(" 　")
    cat = (catalog_norm or "").strip()
    cir = normalize_title(circle).replace("・", "") if circle else ""
    return f"{base}|{cat}|{cir}"


def one_line(text) -> str:
    """Collapse an externally-derived string to a single safe line.

    W-check B3 (adversarial QA item 6): titles come from feeds and
    shop pages — third-party-controlled text. A title containing
    newlines used to be pasted verbatim into Telegram notification
    bodies, where a crafted title could insert what looks like an
    independent alert line (a fake deadline, a fake price, a
    phishing URL). Control characters are stripped and every line
    break becomes a space before a title is composed into a body.
    """
    if text is None:
        return ""
    t = str(text)
    # Line breaks become spaces (not deletions) so a two-line title
    # stays readable once flattened; every other control character
    # is dropped.
    t = t.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    t = "".join(ch for ch in t if ch in "\t" or (ord(ch) >= 0x20
                                                 and ch != "\x7f"))
    # R14 (maniac B N-1 / adversarial 3): Unicode format characters
    # (category Cf — U+202E right-to-left override, U+2066 isolate,
    # U+200B zero-width space, U+FEFF BOM) survived the control-char
    # filter and can visually reorder or disguise a notification
    # line even though they cannot inject a new one. They carry no
    # legitimate meaning in a title; drop them too.
    # W-check B5 (adversarial QA): unpaired surrogates (category
    # Cs) also survived — ord >= 0x20 and category Cs, not Cf. A
    # lone surrogate in a notification body makes the production
    # transport (httpx json=) raise UnicodeEncodeError, silently
    # swallowing the reply; drop Cs as well. (A valid astral
    # character is a single code point in Python, never Cs.)
    import unicodedata
    t = "".join(ch for ch in t
                if unicodedata.category(ch) not in ("Cf", "Cs"))
    return " ".join(t.split())


def strip_surrogates(text):
    """Remove unpaired surrogates from an outbound string (B5).

    The send boundary's last line of defence: any text composed
    WITHOUT one_line (command echoes, stored bodies) must still
    not be able to kill the transport's JSON encoding."""
    if not isinstance(text, str):
        return text
    return "".join(ch for ch in text
                   if not (0xD800 <= ord(ch) <= 0xDFFF))


def normalize_currency(raw) -> str | None:
    """Canonical currency code (R14, maniac A): 'jpy' / 'JPY ' used
    to be stored verbatim, after which every strict == 'JPY'
    comparison silently failed and the price fell out of every
    bucket, baseline and /price series. Codes are case-insensitive
    three-letter tokens in every producer; normalise at the
    boundaries and inside yen_value."""
    if raw is None:
        return None
    c = str(raw).strip().upper()
    return c or None


_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign",
                    "utm_term", "utm_content", "from", "ref",
                    "fbclid", "gclid"}


def canonical_url(u: str) -> str:
    """Listing identity URL, shared by ingest and the absence
    machinery (moved here in R14 so evidence.py compares the same
    form register writes).

    Tracking/navigation parameters, the fragment, and parameter
    ORDER are not part of a product's identity (R14, seller:
    ?id=7&color=red and ?color=red&id=7 are one listing);
    substantive parameters are kept, sorted."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(u or "")
    q = sorted((k, v) for k, v in parse_qsl(parts.query)
               if k not in _TRACKING_PARAMS)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(q), ""))


def page_identity_url(u: str) -> str:
    """Page identity for sitemap-discovered product pages (W-check
    B4, wholesaler): the same page reached as http/https, with or
    without 'www.', or with a trailing slash produced FOUR
    candidates, because sitemap items key on their URL string.
    Scheme, www-prefix and a trailing slash are not part of a
    page's identity; path case and substantive query parameters
    are (canonical_url rules apply on top)."""
    from urllib.parse import urlsplit, urlunsplit
    base = canonical_url(u)
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # R19 (wholesaler): an explicit DEFAULT port (:443 on https,
    # :80 on http) split one page into two identities — the port
    # is part of the identity only when it is not the scheme's
    # default. The base URL is already canonicalised (its scheme
    # may be http), so both defaults are dropped here; the result
    # is always https.
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit(("https", netloc, path, parts.query, ""))
