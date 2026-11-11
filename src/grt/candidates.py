"""Candidate-editions shared bookkeeping.

Kept separate from the fetchers so every discovery lane records
re-discoveries the same way.

W-check B4 (wholesaler / maniac B): aggregation used to be the end
of the story — corroboration was recorded but never USED. Three
consumers now hang off the same aggregation point:

- the queue priority of an unresolved candidate_review row is
  refreshed when the candidate gains independent sources (the
  multi_source component of review_priority finally has a
  producer);
- a REJECTED candidate reopens when a NEW independent host
  corroborates it — the rejection was made on thinner evidence,
  and the system's whole purpose is not missing the item; any
  same-host re-sighting changes nothing;
- stored URLs are canonicalised, so the host arithmetic above
  counts real independence instead of utm/trailing-slash noise.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from . import clock
from .normalize import canonical_url


# Multi-tenant suffixes where the SUBDOMAIN is the operator (two
# sellers on one platform are two sources, not one).
# W-check B5 (wholesaler 中-1): booth.pm — the core doujin
# distribution platform, where every circle is its own subdomain
# — thebase.in (BASE's current domain; base.shop is its old one)
# and makeshop.jp were missing, so every pair of sellers on
# those platforms collapsed into ONE 'operator' and legitimate
# corroboration never counted. Amazon/Rakuten mall storefronts
# are PATH-based (item.rakuten.co.jp/<shop>) and still collapse
# to the mall — a recorded limitation, not fixable at host level.
_TENANT_SUFFIXES = ("myshopify.com", "base.shop", "thebase.in",
                    "makeshop.jp", "booth.pm", "fc2.com",
                    "amebaownd.com", "stores.jp", "shop-pro.jp")


def _host_of(url: str | None) -> str:
    """The OPERATOR key of a URL — the independence unit.

    R19 (wholesaler): the raw hostname was the unit, so one
    label's shop + blog subdomains counted as two 'independent'
    sources (enough to resurrect a rejected candidate and to earn
    the multi-source bonus), and a trailing-dot host
    ('example.com.') counted as a different host from itself.
    The unit is now the registrable domain (eTLD+1 heuristic:
    last two labels, three under co.jp-style second levels),
    except on multi-tenant platform suffixes where the subdomain
    IS the operator. Trailing dots are stripped."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    # W-check B5 (wholesaler 軽微-2): the Unicode and punycode
    # spellings of one IDN domain counted as two operators.
    # Canonicalise to punycode (IDNA) before keying.
    if any(ord(c) > 127 for c in host):
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass
    for suffix in _TENANT_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return host
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 2:
        return host
    # W-check B5 (wholesaler 軽微-1): 'com' (and net/org/co) were
    # missing from the second-level list, so a.com.au and
    # b.com.au BOTH keyed to 'com.au' — two operators collapsed
    # into a domain neither of them owns.
    if len(labels[-1]) == 2 and labels[-2] in (
            "co", "or", "ne", "go", "ac", "ed", "lg", "ad",
            "com", "net", "org"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _stored_urls(row) -> list[str]:
    """All URLs recorded for a candidate row (primary + aggregated),
    tolerating the legacy scalar/dict JSON forms."""
    urls: list[str] = []
    raw = row["source_urls_json"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                urls = [str(u) for u in parsed if u]
            elif isinstance(parsed, str) and parsed:
                urls = [parsed]
            elif isinstance(parsed, dict):
                urls = [str(v) for v in parsed.values()
                        if isinstance(v, str) and v]
        except (ValueError, TypeError):
            urls = []
    if row["source_url"] and row["source_url"] not in urls:
        urls.insert(0, row["source_url"])
    return urls


def independent_source_count(db, candidate_id: int) -> int:
    """Distinct source HOSTS recorded for one candidate — the
    deterministic proxy for independent corroboration (two pages
    of one shop are one source; a label feed plus a shop are two).
    """
    row = db.execute(
        "SELECT source_url, source_urls_json FROM candidate_editions"
        " WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        return 0
    return len({_host_of(u) for u in _stored_urls(row)} - {""})


def _priority_for(db, row, sources: int) -> int:
    from . import scoring
    cand = {
        "title": row["raw_title"] or row["title"],
        "catalog_norm": row["catalog_norm"],
        "existence_confidence": row["existence_confidence"],
        "distribution_type": row["distribution_type"],
        "country": None,
        "independent_sources": sources,
    }
    return scoring.review_priority(db, cand)


def _react_to_corroboration(db, row, hosts_before: set,
                            hosts_after: set) -> None:
    """Priority refresh / rejected-reopen for one candidate whose
    recorded source set just changed."""
    cid = row["id"]
    n_after = len(hosts_after - {""})
    if n_after < 2:
        return
    if row["status"] == "rejected" and hosts_after - hosts_before:
        # A NEW independent host corroborated a rejected candidate:
        # reopen it as a fresh pending review (new queue row + new
        # button message under a reopen idempotency suffix).
        now = clock.now_iso(db)
        fresh = db.execute(
            "SELECT * FROM candidate_editions WHERE id=?",
            (cid,)).fetchone()
        prio = _priority_for(db, fresh, n_after)
        db.execute(
            "UPDATE candidate_editions SET status='pending',"
            " reject_reason=NULL WHERE id=?", (cid,))
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, fingerprint,"
            " priority, created_at) VALUES('candidate_review', ?, ?,"
            " ?, ?)",
            (json.dumps({"candidate_id": cid,
                         "source": "rediscovery"}),
             row["fingerprint"], prio, now))
        from . import outbox as _outbox
        # R19 (wholesaler): the reopen idempotency suffix was the
        # FIXED string '|reopen', so the SECOND reopening (a third
        # independent host) updated the candidate and wrote its
        # event but its button message collided with the first
        # reopening's key and was never queued. The suffix names
        # the corroborating host that caused this reopening.
        new_hosts = sorted(hosts_after - hosts_before - {""})
        _outbox.enqueue_candidate_review(
            db, cid,
            idem_suffix="|reopen:" + "+".join(new_hosts)[:48])
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('candidate_reopened', ?, ?)",
            (json.dumps({"candidate_id": cid,
                         "independent_sources": n_after}), now))
        return
    if row["status"] == "pending":
        # Refresh the unresolved queue row's priority upward when
        # corroboration raised the computed priority.
        q = db.execute(
            "SELECT id, priority, payload_json FROM review_queue"
            " WHERE kind='candidate_review' AND resolved_at IS NULL"
            ).fetchall()
        # R19 (wholesaler): EVERY unresolved row for the candidate
        # is refreshed (the old loop stopped at the first match,
        # leaving a duplicate row at the stale priority), and a
        # string-form candidate_id in a legacy payload is coerced
        # instead of silently skipped.
        for qrow in q:
            try:
                payload = json.loads(qrow["payload_json"] or "")
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            pcid = payload.get("candidate_id")
            if isinstance(pcid, str) and pcid.isdigit():
                pcid = int(pcid)
            if pcid == cid:
                prio = _priority_for(db, row, n_after)
                if prio > (qrow["priority"] or 0):
                    db.execute(
                        "UPDATE review_queue SET priority=? WHERE id=?",
                        (prio, qrow["id"]))


def note_candidate_source(db, fingerprint: str,
                          source_url: str | None) -> bool:
    """Aggregate a re-discovery's source URL onto its candidate.

    Spec C-5.2: discoveries of the same work from multiple sources
    aggregate their source URLs onto ONE candidate. W-check B3
    (maniac B D-7): every register path used INSERT OR IGNORE and
    nothing anywhere wrote source_urls_json, so a second source's
    URL — an independent corroboration — was silently discarded
    and last_seen never moved. Returns True when the candidate was
    updated (a new URL was added or last_seen refreshed).

    W-check B4: URLs are stored canonicalised (utm parameters and
    trailing slashes made one page count as several 'sources'),
    and the aggregation now feeds the corroboration consumers in
    _react_to_corroboration.
    """
    if not fingerprint or not source_url:
        return False
    row = db.execute(
        "SELECT * FROM candidate_editions WHERE fingerprint=?",
        (fingerprint,)).fetchone()
    if row is None:
        return False
    before = _stored_urls(row)
    hosts_before = {_host_of(u) for u in before}
    canon = canonical_url(source_url)
    urls = []
    for u in before:
        cu = canonical_url(u)
        if cu not in urls:
            urls.append(cu)
    if canon and canon not in urls:
        urls.append(canon)
    db.execute(
        "UPDATE candidate_editions SET source_urls_json=?,"
        " last_seen=? WHERE id=?",
        (json.dumps(urls, ensure_ascii=False), clock.now_iso(db),
         row["id"]))
    hosts_after = {_host_of(u) for u in urls}
    if hosts_after != hosts_before:
        _react_to_corroboration(db, row, hosts_before, hosts_after)
    return True
