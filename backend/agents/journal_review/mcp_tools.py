#!/usr/bin/env python3
"""Journal Review tools: query OpenAlex and CrossRef to find, profile, compare, and rank
journals by topical fit and citation impact, feeding find_matching_journals,
get_journal_profile, compare_journals, analyze_paper_fit, and get_field_landscape.
"""
import os
import sys
import json
import time
import logging
import hashlib
from collections import Counter
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import (
    Text, Card, Table, Alert, MetricCard, Grids,
    BarChart, PieChart, create_ui_response,
)
from shared import external_http

logger = logging.getLogger("JournalReviewTools")

OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_JOURNALS_URL = "https://api.crossref.org/journals"

OPENALEX_MAILTO = "astraldeep@example.com"
HEADERS = {"User-Agent": f"AstralDeep/1.0 (mailto:{OPENALEX_MAILTO})"}

MAX_RESPONSE_BYTES = 5 * 1024 * 1024

CITEDNESS_LABEL = "2-yr Mean Citedness (OpenAlex)"

_CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL = 600


def _cache_key(*args: Any) -> str:
    raw = json.dumps(args, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def _get_cached(key: str) -> Optional[Any]:
    if key in _CACHE:
        ts, val = _CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _CACHE[key]
    return None


def _set_cached(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


def _safe_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    key = _cache_key(url, params)
    cached = _get_cached(key)
    if cached is not None:
        return cached
    try:
        resp = external_http.request(
            "GET", url,
            api_key="",
            params=params,
            timeout=timeout,
            max_response_bytes=MAX_RESPONSE_BYTES,
            extra_headers=HEADERS,
        )
        data = resp.json()
        _set_cached(key, data)
        return data
    except Exception as e:
        logger.warning(f"API request failed: {url} — {e}")
        return None


def _fmt_number(n: Optional[int]) -> str:
    if n is None:
        return "N/A"
    return f"{n:,}"


def _extract_issn(issn_raw) -> str:
    if isinstance(issn_raw, list):
        return issn_raw[0] if issn_raw else "N/A"
    return str(issn_raw) if issn_raw else "N/A"


def _fmt_citedness(value: Optional[float]) -> str:
    return "N/A" if value is None else str(value)


def _parse_openalex_source(src: dict) -> dict:
    counts = src.get("counts_by_year", [])
    recent_year = counts[0] if counts else {}

    # Surface OpenAlex's own metric only — no home-grown impact factor
    citedness = (src.get("summary_stats") or {}).get("2yr_mean_citedness")
    citedness = round(citedness, 2) if isinstance(citedness, (int, float)) else None

    topics = src.get("topics", []) or []
    top_topics = [t.get("display_name", "") for t in topics[:8]]

    return {
        "openalex_id": src.get("id", ""),
        "name": src.get("display_name", "Unknown"),
        "issn": _extract_issn(src.get("issn")),
        "issn_l": src.get("issn_l", ""),
        "publisher": src.get("host_organization_name", "N/A"),
        "type": src.get("type", "N/A"),
        "is_oa": src.get("is_oa", False),
        "apc_usd": src.get("apc_usd"),
        "homepage_url": src.get("homepage_url", ""),
        "works_count": src.get("works_count", 0),
        "cited_by_count": src.get("cited_by_count", 0),
        "h_index": src.get("summary_stats", {}).get("h_index"),
        "i10_index": src.get("summary_stats", {}).get("i10_index"),
        "two_year_mean_citedness": citedness,
        "recent_works": recent_year.get("works_count", 0),
        "recent_cited": recent_year.get("cited_by_count", 0),
        "topics": top_topics,
        "country": src.get("country_code", "N/A"),
    }


def _discover_journals_from_works(query: str, per_page: int = 50) -> List[Tuple[str, int]]:
    data = _safe_get(OPENALEX_WORKS_URL, {
        "search": query,
        "per_page": per_page,
        "sort": "relevance_score:desc",
        "mailto": OPENALEX_MAILTO,
    })
    if not data or not data.get("results"):
        return []

    source_ids: List[str] = []
    for work in data["results"]:
        loc = work.get("primary_location") or {}
        src = loc.get("source")
        if src and src.get("type") == "journal" and src.get("id"):
            source_ids.append(src["id"])

    return Counter(source_ids).most_common()


def _fetch_sources_by_ids(openalex_ids: List[str]) -> List[dict]:
    if not openalex_ids:
        return []
    pipe_ids = "|".join(openalex_ids)
    data = _safe_get(OPENALEX_SOURCES_URL, {
        "filter": f"openalex:{pipe_ids}",
        "per_page": len(openalex_ids),
        "mailto": OPENALEX_MAILTO,
    })
    if not data or not data.get("results"):
        return []
    return data["results"]


def _compute_fit_score(paper_keywords: List[str], journal: dict,
                       paper_count: int = 0) -> dict:
    journal_topics = [t.lower() for t in journal.get("topics", [])]
    journal_name_lower = journal.get("name", "").lower()
    kw_lower = [k.lower().strip() for k in paper_keywords if k.strip()]

    topic_hits = 0
    for kw in kw_lower:
        for topic in journal_topics:
            if kw in topic or topic in kw:
                topic_hits += 1
                break
        else:
            if kw in journal_name_lower:
                topic_hits += 1
    topic_score = min(topic_hits / max(len(kw_lower), 1), 1.0)

    h = journal.get("h_index") or 0
    impact_score = min(h / 500.0, 1.0)

    recent = journal.get("recent_works", 0)
    volume_score = min(recent / 2000.0, 1.0)

    freq_score = min(paper_count / 5.0, 1.0) if paper_count > 0 else 0.0

    weighted = (
        (topic_score * 0.35)
        + (freq_score * 0.25)
        + (impact_score * 0.25)
        + (volume_score * 0.10)
        + (0.05 if journal.get("is_oa") else 0.0)
    )
    overall = round(min(weighted, 1.0) * 100, 1)

    return {
        "overall": overall,
        "topic_relevance": round(topic_score * 100, 1),
        "publication_frequency": round(freq_score * 100, 1),
        "impact": round(impact_score * 100, 1),
        "activity": round(volume_score * 100, 1),
        "open_access": journal.get("is_oa", False),
    }


def find_matching_journals(
    query: str,
    keywords: str = "",
    max_results: int = 10,
    open_access_only: bool = False,
    min_h_index: int = 0,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    max_results = min(max(max_results, 1), 25)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else []
    search_terms = kw_list if kw_list else query.split()[:6]

    ranked_sources = _discover_journals_from_works(query, per_page=80)

    if not ranked_sources:
        data = _safe_get(OPENALEX_SOURCES_URL, {
            "search": query,
            "per_page": max_results * 2,
            "filter": "type:journal",
            "mailto": OPENALEX_MAILTO,
        })
        if data and data.get("results"):
            ranked_sources = [(s["id"], 0) for s in data["results"]]

    if not ranked_sources:
        return create_ui_response([
            Alert(message=f"No journals found for query: '{query}'. Try broader terms.",
                  variant="warning")
        ])

    candidate_ids = [sid for sid, _ in ranked_sources[:max_results * 2]]
    paper_counts = {sid: cnt for sid, cnt in ranked_sources}
    sources = _fetch_sources_by_ids(candidate_ids)

    journals = []
    for src in sources:
        j = _parse_openalex_source(src)
        if open_access_only and not j["is_oa"]:
            continue
        if min_h_index and (j.get("h_index") or 0) < min_h_index:
            continue
        pc = paper_counts.get(src.get("id", ""), 0)
        j["paper_count"] = pc
        j["fit"] = _compute_fit_score(search_terms, j, paper_count=pc)
        journals.append(j)

    journals.sort(key=lambda x: x["fit"]["overall"], reverse=True)
    journals = journals[:max_results]

    if not journals:
        return create_ui_response([
            Alert(message="No journals matched your criteria. Try relaxing filters.",
                  variant="warning")
        ])

    components = []

    avg_fit = round(sum(j["fit"]["overall"] for j in journals) / len(journals), 1)
    oa_count = sum(1 for j in journals if j.get("is_oa"))
    top_journal = journals[0]
    components.append(
        Grids(columns=4, id="summary-metrics", children=[
            MetricCard(title="Top Match", value=f"{top_journal['fit']['overall']}%",
                       subtitle=top_journal["name"][:40], id="top-match"),
            MetricCard(title="Avg Fit Score", value=f"{avg_fit}%",
                       subtitle=f"Across {len(journals)} journals", id="avg-fit"),
            MetricCard(title="Open Access", value=str(oa_count),
                       subtitle=f"of {len(journals)} results", id="oa-count"),
            MetricCard(title="Top H-Index", value=str(top_journal.get("h_index", "N/A")),
                       subtitle="Highest in results", id="top-h"),
        ])
    )

    rows = []
    for i, j in enumerate(journals, 1):
        fit = j["fit"]
        oa_label = "Yes" if j.get("is_oa") else "No"
        apc = f"${j['apc_usd']:,}" if j.get("apc_usd") else "N/A"
        rows.append([
            str(i),
            j["name"],
            f"{fit['overall']}%",
            f"{fit['topic_relevance']}%",
            str(j.get("paper_count", 0)),
            str(j.get("h_index", "N/A")),
            _fmt_citedness(j.get("two_year_mean_citedness")),
            oa_label,
            apc,
            j.get("publisher", "N/A"),
        ])

    components.append(
        Card(title=f"Matching Journals for: {query[:60]}", id="results-card", content=[
            Table(
                headers=["#", "Journal", "Fit", "Topic Match", "Papers Found",
                          "H-Index", CITEDNESS_LABEL, "OA", "APC", "Publisher"],
                rows=rows,
                id="journals-table"
            ),
        ])
    )

    detail_items = []
    for j in journals[:3]:
        topics_str = ", ".join(j["topics"][:5]) if j["topics"] else "No topics listed"
        jid = j.get("issn_l") or j["name"][:20].replace(" ", "-")
        detail_items.append(
            Card(
                title=f"{j['name']} — Topics & Details",
                content=[
                    Text(content=f"**Publisher:** {j['publisher']}", id=f"pub-{jid}"),
                    Text(content=f"**ISSN:** {j['issn']}", id=f"issn-{jid}"),
                    Text(content=f"**Topics:** {topics_str}", id=f"topics-{jid}"),
                    Text(content=f"**Total Works:** {_fmt_number(j['works_count'])} | "
                         f"**Total Citations:** {_fmt_number(j['cited_by_count'])}",
                         id=f"counts-{jid}"),
                    Text(content=f"**Homepage:** {j.get('homepage_url', 'N/A')}",
                         id=f"url-{jid}"),
                ],
            )
        )
    if detail_items:
        components.append(Card(title="Top Match Details", id="details-card", content=detail_items))

    data_summary = {
        "query": query,
        "keywords": kw_list,
        "total_results": len(journals),
        "journals": [
            {
                "rank": i + 1,
                "name": j["name"],
                "publisher": j["publisher"],
                "h_index": j.get("h_index"),
                "two_year_mean_citedness": j.get("two_year_mean_citedness"),
                "is_oa": j.get("is_oa"),
                "apc_usd": j.get("apc_usd"),
                "fit_score": j["fit"]["overall"],
                "topic_relevance": j["fit"]["topic_relevance"],
                "papers_found": j.get("paper_count", 0),
                "topics": j["topics"][:5],
                "homepage": j.get("homepage_url"),
                "issn_l": j.get("issn_l"),
            }
            for i, j in enumerate(journals)
        ],
    }

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": data_summary,
    }


def get_journal_profile(
    journal_name: str,
    issn: str = "",
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    source = None
    if issn:
        issn_clean = issn.strip().replace(" ", "")
        data = _safe_get(OPENALEX_SOURCES_URL, {
            "filter": f"issn:{issn_clean}",
            "mailto": OPENALEX_MAILTO,
        })
        if data and data.get("results"):
            source = data["results"][0]

    if not source:
        data = _safe_get(OPENALEX_SOURCES_URL, {
            "search": journal_name,
            "per_page": 5,
            "filter": "type:journal",
            "mailto": OPENALEX_MAILTO,
        })
        if data and data.get("results"):
            name_lower = journal_name.lower()
            for r in data["results"]:
                if name_lower in r.get("display_name", "").lower():
                    source = r
                    break
            if not source:
                source = data["results"][0]

    if not source:
        return create_ui_response([
            Alert(message=f"Journal not found: '{journal_name}'. Check the name or ISSN.",
                  variant="error")
        ])

    j = _parse_openalex_source(source)

    crossref_meta = {}
    if j.get("issn_l"):
        cr_data = _safe_get(f"{CROSSREF_JOURNALS_URL}/{j['issn_l']}")
        if cr_data and cr_data.get("status") == "ok":
            msg = cr_data.get("message", {})
            crossref_meta = {
                "subjects": msg.get("subjects", []),
                "total_dois": msg.get("counts", {}).get("total-dois", 0),
                "recent_dois": msg.get("counts", {}).get("current-dois", 0),
            }

    components = []

    oa_status = "Open Access" if j["is_oa"] else "Subscription"
    apc_str = f"${j['apc_usd']:,}" if j.get("apc_usd") else "N/A"

    components.append(
        Grids(columns=4, id="profile-metrics", children=[
            MetricCard(title="H-Index", value=str(j.get("h_index", "N/A")),
                       subtitle="Lifetime", id="h-index"),
            MetricCard(title="2-yr Mean Citedness",
                       value=_fmt_citedness(j.get("two_year_mean_citedness")),
                       subtitle="OpenAlex", id="two-yr-citedness"),
            MetricCard(title="Total Citations", value=_fmt_number(j["cited_by_count"]),
                       subtitle="All time", id="total-cites"),
            MetricCard(title="Access Model", value=oa_status,
                       subtitle=f"APC: {apc_str}", id="access-model"),
        ])
    )

    topics_str = ", ".join(j["topics"]) if j["topics"] else "Not classified"
    subjects_str = ", ".join(
        s.get("name", "") for s in crossref_meta.get("subjects", [])
    ) if crossref_meta.get("subjects") else "N/A"

    info_rows = [
        ["Publisher", j["publisher"]],
        ["ISSN (linking)", j["issn_l"] or "N/A"],
        ["ISSN", j["issn"]],
        ["Type", j["type"]],
        ["Country", j["country"]],
        ["Homepage", j.get("homepage_url", "N/A")],
        ["Topics", topics_str],
        ["CrossRef Subjects", subjects_str],
        ["Total Works", _fmt_number(j["works_count"])],
        ["Total DOIs (CrossRef)", _fmt_number(crossref_meta.get("total_dois"))],
        ["I10-Index", str(j.get("i10_index", "N/A"))],
        [CITEDNESS_LABEL, _fmt_citedness(j.get("two_year_mean_citedness"))],
    ]

    components.append(
        Card(title=j["name"], id="journal-info", content=[
            Table(headers=["Attribute", "Value"], rows=info_rows, id="info-table"),
        ])
    )

    counts_by_year = source.get("counts_by_year", [])[:10]
    if counts_by_year:
        years = [str(c["year"]) for c in reversed(counts_by_year)]
        works_vals = [c.get("works_count", 0) for c in reversed(counts_by_year)]
        cite_vals = [c.get("cited_by_count", 0) for c in reversed(counts_by_year)]

        components.append(
            Card(title="Publication & Citation Trends (Last 10 Years)", id="trends-card", content=[
                BarChart(
                    labels=years,
                    datasets=[{"label": "Papers Published", "data": works_vals}],
                    id="works-trend"
                ),
                BarChart(
                    labels=years,
                    datasets=[{"label": "Citations Received", "data": cite_vals}],
                    id="cites-trend"
                ),
            ])
        )

    profile_data = {
        "name": j["name"],
        "publisher": j["publisher"],
        "issn_l": j["issn_l"],
        "h_index": j.get("h_index"),
        "is_oa": j["is_oa"],
        "apc_usd": j.get("apc_usd"),
        "works_count": j["works_count"],
        "cited_by_count": j["cited_by_count"],
        "topics": j["topics"],
        "crossref_subjects": [s.get("name", "") for s in crossref_meta.get("subjects", [])],
        "homepage": j.get("homepage_url"),
        "recent_works_per_year": j["recent_works"],
        "recent_citations_per_year": j["recent_cited"],
        "i10_index": j.get("i10_index"),
        "two_year_mean_citedness": j.get("two_year_mean_citedness"),
    }

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": profile_data,
    }


def compare_journals(
    journal_names: str,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    names = [n.strip() for n in journal_names.split(";") if n.strip()]
    if len(names) < 2:
        return create_ui_response([
            Alert(message="Please provide at least 2 journal names separated by semicolons.",
                  variant="warning")
        ])
    if len(names) > 5:
        names = names[:5]

    journals = []
    not_found = []
    for name in names:
        data = _safe_get(OPENALEX_SOURCES_URL, {
            "search": name,
            "per_page": 3,
            "filter": "type:journal",
            "mailto": OPENALEX_MAILTO,
        })
        if data and data.get("results"):
            name_lower = name.lower()
            best = None
            for r in data["results"]:
                if name_lower in r.get("display_name", "").lower():
                    best = r
                    break
            if not best:
                best = data["results"][0]
            journals.append(_parse_openalex_source(best))
        else:
            not_found.append(name)

    if not journals:
        return create_ui_response([
            Alert(message="None of the specified journals were found.", variant="error")
        ])

    components = []

    if not_found:
        components.append(
            Alert(message=f"Not found: {', '.join(not_found)}", variant="warning")
        )

    headers = ["Metric"] + [j["name"][:35] for j in journals]
    metrics = [
        ("H-Index", lambda j: str(j.get("h_index", "N/A"))),
        (CITEDNESS_LABEL, lambda j: _fmt_citedness(j.get("two_year_mean_citedness"))),
        ("Total Works", lambda j: _fmt_number(j["works_count"])),
        ("Total Citations", lambda j: _fmt_number(j["cited_by_count"])),
        ("Recent Works/yr", lambda j: _fmt_number(j["recent_works"])),
        ("Recent Cites/yr", lambda j: _fmt_number(j["recent_cited"])),
        ("Open Access", lambda j: "Yes" if j["is_oa"] else "No"),
        ("APC (USD)", lambda j: f"${j['apc_usd']:,}" if j.get("apc_usd") else "N/A"),
        ("Publisher", lambda j: j["publisher"]),
        ("I10-Index", lambda j: str(j.get("i10_index", "N/A"))),
    ]

    rows = []
    for metric_label, fn in metrics:
        row = [metric_label] + [fn(j) for j in journals]
        rows.append(row)

    components.append(
        Card(title="Journal Comparison", id="comparison-card", content=[
            Table(headers=headers, rows=rows, id="comparison-table"),
        ])
    )

    j_labels = [j["name"][:25] for j in journals]

    h_values = [j.get("h_index") or 0 for j in journals]
    components.append(
        BarChart(labels=j_labels,
                 datasets=[{"label": "H-Index", "data": h_values}],
                 id="h-index-chart")
    )

    cite_values = [j["cited_by_count"] for j in journals]
    components.append(
        BarChart(labels=j_labels,
                 datasets=[{"label": "Total Citations", "data": cite_values}],
                 id="citation-chart")
    )

    compare_data = {
        "journals_compared": len(journals),
        "not_found": not_found,
        "journals": [
            {
                "name": j["name"],
                "publisher": j["publisher"],
                "h_index": j.get("h_index"),
                "two_year_mean_citedness": j.get("two_year_mean_citedness"),
                "is_oa": j["is_oa"],
                "apc_usd": j.get("apc_usd"),
                "works_count": j["works_count"],
                "cited_by_count": j["cited_by_count"],
                "recent_works": j["recent_works"],
                "topics": j["topics"][:5],
            }
            for j in journals
        ],
    }

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": compare_data,
    }


def analyze_paper_fit(
    paper_title: str,
    paper_keywords: str,
    paper_abstract: str = "",
    target_journals: str = "",
    max_suggestions: int = 8,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    kw_list = [k.strip() for k in paper_keywords.split(",") if k.strip()]
    if not kw_list:
        return create_ui_response([
            Alert(message="Please provide at least one keyword for the paper.", variant="warning")
        ])

    search_text = f"{paper_title} {' '.join(kw_list)}"
    if paper_abstract:
        search_text += f" {paper_abstract[:200]}"

    journals = []

    if target_journals:
        names = [n.strip() for n in target_journals.split(";") if n.strip()]
        for name in names[:10]:
            data = _safe_get(OPENALEX_SOURCES_URL, {
                "search": name,
                "per_page": 3,
                "filter": "type:journal",
                "mailto": OPENALEX_MAILTO,
            })
            if data and data.get("results"):
                name_lower = name.lower()
                best = None
                for r in data["results"]:
                    if name_lower in r.get("display_name", "").lower():
                        best = r
                        break
                if not best:
                    best = data["results"][0]
                j = _parse_openalex_source(best)
                j["paper_count"] = 0
                j["fit"] = _compute_fit_score(kw_list, j)
                journals.append(j)
    else:
        ranked_sources = _discover_journals_from_works(search_text, per_page=80)
        if ranked_sources:
            candidate_ids = [sid for sid, _ in ranked_sources[:max_suggestions * 2]]
            paper_counts = {sid: cnt for sid, cnt in ranked_sources}
            sources = _fetch_sources_by_ids(candidate_ids)
            for src in sources:
                j = _parse_openalex_source(src)
                pc = paper_counts.get(src.get("id", ""), 0)
                j["paper_count"] = pc
                j["fit"] = _compute_fit_score(kw_list, j, paper_count=pc)
                journals.append(j)

    journals.sort(key=lambda x: x["fit"]["overall"], reverse=True)
    journals = journals[:max_suggestions]

    if not journals:
        return create_ui_response([
            Alert(message="No matching journals found. Try different keywords.", variant="warning")
        ])

    components = []

    components.append(
        Card(title="Paper Under Review", id="paper-info", content=[
            Text(content=f"**Title:** {paper_title}", id="paper-title"),
            Text(content=f"**Keywords:** {', '.join(kw_list)}", id="paper-kw"),
            Text(content=f"**Abstract:** {paper_abstract[:300]}{'...' if len(paper_abstract) > 300 else ''}"
                 if paper_abstract else "**Abstract:** Not provided",
                 id="paper-abstract"),
        ])
    )

    rows = []
    for i, j in enumerate(journals, 1):
        fit = j["fit"]
        tier = ("Excellent" if fit["overall"] >= 70
                else "Good" if fit["overall"] >= 50
                else "Moderate" if fit["overall"] >= 30
                else "Low")
        rows.append([
            str(i),
            j["name"],
            f"{fit['overall']}%",
            tier,
            f"{fit['topic_relevance']}%",
            f"{fit['impact']}%",
            f"{fit['activity']}%",
            "Yes" if j["is_oa"] else "No",
        ])

    components.append(
        Card(title="Journal Fit Analysis", id="fit-card", content=[
            Table(
                headers=["#", "Journal", "Overall Fit", "Tier", "Topic Match",
                          "Impact Score", "Activity", "OA"],
                rows=rows,
                id="fit-table"
            ),
        ])
    )

    top3 = journals[:3]
    top3_labels = [j["name"][:25] for j in top3]
    components.append(
        Card(title="Top 3 — Fit Score Breakdown", id="breakdown-card", content=[
            BarChart(
                labels=top3_labels,
                datasets=[
                    {"label": "Topic Relevance", "data": [j["fit"]["topic_relevance"] for j in top3]},
                    {"label": "Impact Score", "data": [j["fit"]["impact"] for j in top3]},
                    {"label": "Activity Score", "data": [j["fit"]["activity"] for j in top3]},
                ],
                id="fit-breakdown-chart"
            ),
        ])
    )

    best = journals[0]
    rec_text = (
        f"**Top recommendation: {best['name']}** with an overall fit score of "
        f"{best['fit']['overall']}%. "
    )
    if best["fit"]["topic_relevance"] >= 60:
        rec_text += "Strong topical alignment with your paper's keywords. "
    if (best.get("h_index") or 0) > 100:
        rec_text += f"High-impact venue (h-index: {best['h_index']}). "
    if best["is_oa"]:
        apc = f" (APC: ${best['apc_usd']:,})" if best.get("apc_usd") else ""
        rec_text += f"Open access{apc}. "

    components.append(
        Card(title="Recommendation", id="rec-card", content=[
            Text(content=rec_text, id="rec-text"),
        ])
    )

    data_out = {
        "paper_title": paper_title,
        "paper_keywords": kw_list,
        "journals_evaluated": len(journals),
        "recommendation": best["name"],
        "recommendation_score": best["fit"]["overall"],
        "results": [
            {
                "rank": i + 1,
                "name": j["name"],
                "publisher": j["publisher"],
                "overall_fit": j["fit"]["overall"],
                "topic_relevance": j["fit"]["topic_relevance"],
                "impact_score": j["fit"]["impact"],
                "activity_score": j["fit"]["activity"],
                "h_index": j.get("h_index"),
                "is_oa": j["is_oa"],
                "topics": j["topics"][:5],
            }
            for i, j in enumerate(journals)
        ],
    }

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": data_out,
    }


def get_field_landscape(
    field: str,
    top_n: int = 15,
    session_id: str = "default",
    **kwargs
) -> Dict[str, Any]:
    top_n = min(max(top_n, 5), 25)

    ranked_sources = _discover_journals_from_works(field, per_page=100)

    if not ranked_sources:
        data = _safe_get(OPENALEX_SOURCES_URL, {
            "search": field,
            "per_page": top_n * 2,
            "sort": "cited_by_count:desc",
            "filter": "type:journal",
            "mailto": OPENALEX_MAILTO,
        })
        if data and data.get("results"):
            ranked_sources = [(s["id"], 0) for s in data["results"]]

    if not ranked_sources:
        return create_ui_response([
            Alert(message=f"No journals found for field: '{field}'.", variant="warning")
        ])

    candidate_ids = [sid for sid, _ in ranked_sources[:top_n * 2]]
    paper_counts = {sid: cnt for sid, cnt in ranked_sources}
    sources = _fetch_sources_by_ids(candidate_ids)

    journals = []
    for src in sources:
        j = _parse_openalex_source(src)
        j["paper_count"] = paper_counts.get(src.get("id", ""), 0)
        journals.append(j)

    journals.sort(key=lambda x: x["cited_by_count"], reverse=True)
    journals = journals[:top_n]

    if not journals:
        return create_ui_response([
            Alert(message=f"No journals found for field: '{field}'.", variant="warning")
        ])

    components = []

    total_cites = sum(j["cited_by_count"] for j in journals)
    oa_pct = round(sum(1 for j in journals if j["is_oa"]) / len(journals) * 100)
    avg_h = round(sum(j.get("h_index") or 0 for j in journals) / len(journals))

    components.append(
        Grids(columns=4, id="landscape-metrics", children=[
            MetricCard(title="Journals Found", value=str(len(journals)),
                       subtitle=f"Top in {field}", id="count"),
            MetricCard(title="Avg H-Index", value=str(avg_h),
                       subtitle="Across results", id="avg-h"),
            MetricCard(title="Open Access", value=f"{oa_pct}%",
                       subtitle="Of listed journals", id="oa-pct"),
            MetricCard(title="Combined Citations", value=_fmt_number(total_cites),
                       subtitle="All journals", id="total-cites"),
        ])
    )

    rows = []
    for i, j in enumerate(journals, 1):
        rows.append([
            str(i),
            j["name"],
            j["publisher"],
            str(j.get("h_index", "N/A")),
            _fmt_citedness(j.get("two_year_mean_citedness")),
            _fmt_number(j["cited_by_count"]),
            _fmt_number(j["recent_works"]),
            "Yes" if j["is_oa"] else "No",
        ])

    components.append(
        Card(title=f"Top Journals in {field.title()}", id="landscape-card", content=[
            Table(
                headers=["#", "Journal", "Publisher", "H-Index", CITEDNESS_LABEL,
                          "Citations", "Recent/yr", "OA"],
                rows=rows,
                id="landscape-table"
            ),
        ])
    )

    chart_labels = [j["name"][:25] for j in journals[:10]]
    chart_vals = [j.get("h_index") or 0 for j in journals[:10]]
    components.append(
        BarChart(labels=chart_labels,
                 datasets=[{"label": "H-Index", "data": chart_vals}],
                 id="landscape-h-chart")
    )

    pub_counts: Dict[str, int] = {}
    for j in journals:
        pub = j["publisher"] or "Unknown"
        pub_counts[pub] = pub_counts.get(pub, 0) + 1
    if len(pub_counts) > 1:
        components.append(
            PieChart(labels=list(pub_counts.keys()),
                     data=list(pub_counts.values()),
                     id="publisher-pie")
        )

    data_out = {
        "field": field,
        "total_journals": len(journals),
        "avg_h_index": avg_h,
        "oa_percentage": oa_pct,
        "journals": [
            {
                "rank": i + 1,
                "name": j["name"],
                "publisher": j["publisher"],
                "h_index": j.get("h_index"),
                "two_year_mean_citedness": j.get("two_year_mean_citedness"),
                "cited_by_count": j["cited_by_count"],
                "is_oa": j["is_oa"],
                "topics": j["topics"][:5],
                "papers_found": j.get("paper_count", 0),
            }
            for i, j in enumerate(journals)
        ],
    }

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": data_out,
    }


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "find_matching_journals": {
        "function": find_matching_journals,
        "scope": "tools:search",
        "description": (
            "Search for scientific journals that match a research paper's topic, "
            "abstract, or keywords. Finds papers on the topic and identifies which "
            "journals publish them most frequently. Returns a ranked list with impact "
            "metrics (h-index, OpenAlex 2-yr mean citedness), topical fit scores, open "
            "access status, and APC costs. Use this when a researcher wants to know "
            "which journals publish work similar to theirs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Research topic, paper title, or abstract excerpt to search for."
                },
                "keywords": {
                    "type": "string",
                    "description": "Comma-separated keywords for finer matching (e.g., 'deep learning, NLP, transformers')."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of journals to return (1-25, default 10).",
                    "default": 10
                },
                "open_access_only": {
                    "type": "boolean",
                    "description": "If true, only return open-access journals.",
                    "default": False
                },
                "min_h_index": {
                    "type": "integer",
                    "description": "Minimum h-index threshold (0 = no filter).",
                    "default": 0
                },
            },
            "required": ["query"]
        }
    },

    "get_journal_profile": {
        "function": get_journal_profile,
        "scope": "tools:search",
        "description": (
            "Get a comprehensive profile for a specific scientific journal including "
            "h-index, OpenAlex 2-yr mean citedness, citation counts, publication volume, "
            "open access status, APC costs, publisher, topics/scope, and year-over-year "
            "publication and citation trends. Use this to deep-dive into a single journal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "journal_name": {
                    "type": "string",
                    "description": "Name of the journal (e.g., 'Nature Machine Intelligence')."
                },
                "issn": {
                    "type": "string",
                    "description": "Optional ISSN for precise lookup (e.g., '2522-5839')."
                },
            },
            "required": ["journal_name"]
        }
    },

    "compare_journals": {
        "function": compare_journals,
        "scope": "tools:search",
        "description": (
            "Compare 2-5 scientific journals side-by-side on key metrics: h-index, "
            "OpenAlex 2-yr mean citedness, citation count, publication volume, open access status, "
            "APC costs, and publisher. Provide journal names separated by semicolons. "
            "Use this when a researcher is deciding between specific journals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "journal_names": {
                    "type": "string",
                    "description": "Semicolon-separated journal names (e.g., 'Nature;Science;PNAS')."
                },
            },
            "required": ["journal_names"]
        }
    },

    "analyze_paper_fit": {
        "function": analyze_paper_fit,
        "scope": "tools:search",
        "description": (
            "Analyze how well a research paper fits specific journals or automatically "
            "find the best-fit journals. Finds real papers on the same topic to identify "
            "where similar work gets published. Scores topical relevance, impact alignment, "
            "and publishing activity. Provide the paper's title, keywords, and optionally "
            "its abstract and target journals. Use this for a personalized recommendation "
            "of where to submit a specific paper."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paper_title": {
                    "type": "string",
                    "description": "Title of the research paper."
                },
                "paper_keywords": {
                    "type": "string",
                    "description": "Comma-separated keywords describing the paper's content."
                },
                "paper_abstract": {
                    "type": "string",
                    "description": "Abstract or summary of the paper (improves matching accuracy)."
                },
                "target_journals": {
                    "type": "string",
                    "description": "Optional semicolon-separated journal names to evaluate against. Leave empty to auto-find best matches."
                },
                "max_suggestions": {
                    "type": "integer",
                    "description": "Max journals to return when auto-finding (default 8).",
                    "default": 8
                },
            },
            "required": ["paper_title", "paper_keywords"]
        }
    },

    "get_field_landscape": {
        "function": get_field_landscape,
        "scope": "tools:search",
        "description": (
            "Get an overview of the top journals in a research field or discipline, "
            "ranked by citation impact. Discovers journals by finding real papers in "
            "the field and identifying where they are published. Shows h-indices, "
            "OpenAlex 2-yr mean citedness, open access percentages, and publisher "
            "distribution. Use this when a researcher wants to understand the journal "
            "ecosystem in their field before deciding where to submit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "Research field or discipline (e.g., 'machine learning', 'oncology')."
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top journals to return (5-25, default 15).",
                    "default": 15
                },
            },
            "required": ["field"]
        }
    },
}
