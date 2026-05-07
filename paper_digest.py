#!/usr/bin/env python3
"""
Daily Europe PMC paper digest.

- Reads keywords from config.json (next to this script).
- Fetches Europe PMC entries indexed on the target date (CREATION_DATE).
- Dedupes across keywords by (source, id), with pmid as secondary tie-break.
- Writes one self-contained interactive HTML file per day to ./digests/.
- Updates ./index.html with links to every daily digest.
- Prints a short Slack-friendly summary (per-source / per-journal counts) to stdout.

Usage:
    python3 paper_digest.py                 # uses yesterday in KST as target date
    python3 paper_digest.py 2026-05-04      # explicit date (YYYY-MM-DD)
    python3 paper_digest.py --today         # use today in KST

Exit codes:
    0  ran fine (digest produced, possibly with 0 papers)
    2  one or more keyword fetches failed AND zero results were collected
"""

from __future__ import annotations

import json
import sys
import os
import html
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from collections import defaultdict, OrderedDict

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DIGESTS_DIR = ROOT / "digests"
DATA_DIR = ROOT / "data"
INDEX_PATH = ROOT / "index.html"

EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_ABSTRACT_TPL = "https://europepmc.org/abstract/{source}/{id}"
USER_AGENT = "research-digest/1.0 (mailto:yoojinha@hanyang.ac.kr)"


# ---------------------------------------------------------------------------
# config & dates
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def kst_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def resolve_target_date(argv: list[str]) -> dt.date:
    """Single-date legacy resolver — used by callers that want exactly one day."""
    if len(argv) >= 2:
        arg = argv[1]
        if arg == "--today":
            return kst_now().date()
        try:
            return dt.date.fromisoformat(arg)
        except ValueError:
            raise SystemExit(f"Invalid date arg: {arg!r} (use YYYY-MM-DD or --today)")
    return (kst_now() - dt.timedelta(days=1)).date()


def resolve_target_dates(argv: list[str]) -> list[dt.date]:
    """Resolve one or more target dates from CLI args.

    No args (default): backfill mode — Mon returns [last Fri, Sat, Sun],
    other weekdays return [yesterday]. (On Sat/Sun the script is normally not
    triggered, but if run manually it returns [yesterday].)

    Args:
      --today                       run for today only
      YYYY-MM-DD [YYYY-MM-DD ...]   run for the listed dates
    """
    if len(argv) >= 2:
        out: list[dt.date] = []
        for arg in argv[1:]:
            if arg == "--today":
                out.append(kst_now().date())
            else:
                try:
                    out.append(dt.date.fromisoformat(arg))
                except ValueError:
                    raise SystemExit(f"Invalid date arg: {arg!r} (use YYYY-MM-DD or --today)")
        return out

    today = kst_now().date()
    yesterday = today - dt.timedelta(days=1)
    # KST weekday(): Mon=0 ... Sun=6
    if today.weekday() == 0:  # Monday → backfill Fri (-3), Sat (-2), Sun (-1)
        return [today - dt.timedelta(days=n) for n in (3, 2, 1)]
    return [yesterday]


# ---------------------------------------------------------------------------
# Europe PMC fetch
# ---------------------------------------------------------------------------

def build_query(keyword: str, target_date: dt.date, override: str | None) -> str:
    date_filter = f"CREATION_DATE:[{target_date.isoformat()} TO {target_date.isoformat()}]"
    if override:
        body = override.strip()
    else:
        # Quote the keyword so multi-word phrases match as a phrase.
        kw_q = keyword.replace('"', '')
        body = f'(TITLE:"{kw_q}" OR ABSTRACT:"{kw_q}")'
    return f"{body} AND {date_filter}"


def _epmc_get(query: str, page_size: int) -> list[dict]:
    params = {
        "query": query,
        "format": "json",
        "pageSize": str(page_size),
        "resultType": "core",
    }
    url = f"{EPMC_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload.get("resultList", {}).get("result", []) or []


def fetch_keyword(keyword: str, target_date: dt.date, page_size: int,
                  override: str | None) -> list[dict]:
    """Fetch all results (up to page_size hard cap) for one keyword."""
    return _epmc_get(build_query(keyword, target_date, override), page_size)


def fetch_journal(journal: str, target_date: dt.date, page_size: int) -> list[dict]:
    """Fetch all results from a specific JOURNAL on the target CREATION_DATE."""
    j = journal.replace('"', '')
    query = (f'JOURNAL:"{j}" '
             f'AND CREATION_DATE:[{target_date.isoformat()} TO {target_date.isoformat()}]')
    return _epmc_get(query, page_size)


def collect(config: dict, target_date: dt.date):
    """
    Returns:
        papers: list of unique paper dicts (each with 'matched_keywords' and 'matched_journals')
        kw_to_keys: map keyword -> list of (source,id) keys it matched
        failed: list of (label, error_string) — labels include "kw:foo" and "journal:Nature"
    """
    keywords = config["keywords"]
    pinned_journals = config.get("always_include_journals", []) or []
    overrides = config.get("keyword_overrides", {}) or {}
    page_size = int(config.get("page_size", 50))

    seen: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    kw_to_keys: dict[str, list[tuple[str, str]]] = {}
    failed: list[tuple[str, str]] = []

    for kw in keywords:
        try:
            results = fetch_keyword(kw, target_date, page_size, overrides.get(kw))
        except Exception as e:
            failed.append((f"kw:{kw}", f"{type(e).__name__}: {e}"))
            kw_to_keys[kw] = []
            continue
        keys: list[tuple[str, str]] = []
        for r in results:
            key = (r.get("source") or "?", r.get("id") or "?")
            if key in seen:
                if kw not in seen[key]["matched_keywords"]:
                    seen[key]["matched_keywords"].append(kw)
            else:
                r = dict(r)
                r["matched_keywords"] = [kw]
                r["matched_journals"] = []
                seen[key] = r
            keys.append(key)
        kw_to_keys[kw] = keys

    # Always-include journal queries — paper is included even with zero keyword hits.
    for j in pinned_journals:
        try:
            results = fetch_journal(j, target_date, page_size)
        except Exception as e:
            failed.append((f"journal:{j}", f"{type(e).__name__}: {e}"))
            continue
        for r in results:
            # EPMC's JOURNAL field can match abbreviations / variants — keep only exact matches.
            jt = (r.get("journalTitle") or "").strip()
            ji = r.get("journalInfo") or {}
            jr = ji.get("journal") or {} if isinstance(ji, dict) else {}
            jt_alt = (jr.get("title") or "").strip() if isinstance(jr, dict) else ""
            if j.lower() not in {jt.lower(), jt_alt.lower()}:
                continue
            key = (r.get("source") or "?", r.get("id") or "?")
            if key in seen:
                if j not in seen[key]["matched_journals"]:
                    seen[key]["matched_journals"].append(j)
            else:
                r = dict(r)
                r["matched_keywords"] = []
                r["matched_journals"] = [j]
                seen[key] = r

    return list(seen.values()), kw_to_keys, failed


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def fmt_authors(paper: dict, max_n: int = 3) -> str:
    raw = paper.get("authorString") or ""
    if not raw:
        al = paper.get("authorList", {}).get("author", []) if isinstance(paper.get("authorList"), dict) else []
        names = [a.get("fullName") or a.get("lastName") or "" for a in al]
    else:
        names = [s.strip() for s in raw.split(",") if s.strip()]
    if not names:
        return ""
    if len(names) <= max_n:
        return ", ".join(names)
    return ", ".join(names[:max_n]) + ", et al."


def journal_of(p: dict) -> str:
    j = p.get("journalTitle")
    if j:
        return j
    ji = p.get("journalInfo") or {}
    if isinstance(ji, dict):
        jr = ji.get("journal") or {}
        if isinstance(jr, dict):
            return jr.get("title") or ji.get("title") or ""
        return ji.get("title") or ""
    return ""


def abstract_excerpt(p: dict, n: int = 250) -> str:
    a = p.get("abstractText") or ""
    if not a:
        return ""
    a = a.strip().replace("\n", " ")
    if len(a) <= n:
        return a
    return a[:n].rsplit(" ", 1)[0] + "…"


def epmc_url(p: dict) -> str:
    src = p.get("source") or "MED"
    pid = p.get("id") or ""
    return EPMC_ABSTRACT_TPL.format(source=src, id=pid)


def doi_url(p: dict) -> str | None:
    d = p.get("doi")
    if not d:
        return None
    return f"https://doi.org/{d}"


def source_label(s: str) -> str:
    return {
        "MED": "PubMed",
        "PMC": "PMC",
        "PPR": "Preprint",
        "AGR": "Agricola",
        "CBA": "Chinese Bio Abstr.",
        "CTX": "CiteXplore",
        "ETH": "EThOS",
        "HIR": "NICE",
        "PAT": "Patents",
    }.get(s, s)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_HEAD = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Digest — {date}</title>
<style>
:root {{
  --bg: #fafaf7;
  --fg: #1c1c1a;
  --muted: #6b6b66;
  --accent: #b85c00;
  --card: #ffffff;
  --border: #e5e3dc;
  --chip: #f0ede4;
  --chip-active: #b85c00;
  --chip-active-fg: #ffffff;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  font-size: 15px; line-height: 1.55; }}
.wrap {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 80px; }}
header h1 {{ margin: 0 0 4px; font-size: 26px; letter-spacing: -0.01em; }}
header .meta {{ color: var(--muted); font-size: 13px; }}
.summary {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin: 22px 0; }}
.summary h2 {{ margin: 0 0 10px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
.summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 14px 28px; }}
.summary-grid .col h3 {{ margin: 0 0 6px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
.summary-grid .col ul {{ margin: 0; padding: 0; list-style: none; font-size: 14px; }}
.summary-grid .col li {{ display: flex; justify-content: space-between; gap: 12px; padding: 2px 0; border-bottom: 1px dashed var(--border); }}
.summary-grid .col li:last-child {{ border-bottom: 0; }}
.summary-grid .col li b {{ font-weight: 600; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 18px; }}
.chip {{ display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 999px; background: var(--chip); border: 1px solid var(--border);
  font-size: 13px; cursor: pointer; user-select: none; transition: all 0.12s; }}
.chip:hover {{ border-color: var(--accent); }}
.chip.active {{ background: var(--chip-active); color: var(--chip-active-fg); border-color: var(--chip-active); }}
.chip .count {{ opacity: 0.6; margin-left: 6px; font-variant-numeric: tabular-nums; }}
.chip.active .count {{ opacity: 0.85; }}
.filter-row {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; }}
.filter-row .lbl {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; min-width: 70px; }}
.search-box {{ width: 100%; margin: 14px 0 22px; }}
.search-box input {{ width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; background: var(--card); }}
.papers {{ display: flex; flex-direction: column; gap: 14px; }}
.paper {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; transition: border-color 0.12s; }}
.paper:hover {{ border-color: var(--accent); }}
.paper.hidden {{ display: none; }}
.paper h3 {{ margin: 0 0 6px; font-size: 17px; line-height: 1.35; font-weight: 600; }}
.paper h3 a {{ color: var(--fg); text-decoration: none; }}
.paper h3 a:hover {{ color: var(--accent); text-decoration: underline; }}
.paper .by {{ font-size: 13px; color: var(--muted); margin: 0 0 6px; }}
.paper .meta {{ font-size: 12px; color: var(--muted); margin: 0 0 8px; display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; }}
.paper .meta .src {{ display: inline-block; padding: 1px 7px; border-radius: 4px; background: var(--chip); font-weight: 600; letter-spacing: 0.02em; font-size: 11px; }}
.paper .meta .src.PPR {{ background: #fff3df; color: #8a4b00; }}
.paper .meta .src.MED {{ background: #e3eef6; color: #1f4e75; }}
.paper .meta .src.PMC {{ background: #e3f4e6; color: #2a6b35; }}
.paper .meta .kw {{ display: inline-block; padding: 0 6px; background: var(--chip); border-radius: 4px; font-size: 11px; }}
.paper .meta .pinned-tag {{ background: #fff0d6; color: #8a4b00; font-weight: 600; }}
.chip.pinned {{ background: #fff0d6; border-color: #f0c388; color: #8a4b00; }}
.chip.pinned.active {{ background: #b85c00; color: #ffffff; border-color: #b85c00; }}
.paper .ab {{ font-size: 14px; color: #2c2c28; margin: 6px 0 0; }}
.paper .doi a {{ color: var(--muted); font-size: 12px; text-decoration: none; }}
.paper .doi a:hover {{ color: var(--accent); }}
.empty {{ background: var(--card); border: 1px dashed var(--border); border-radius: 10px; padding: 30px; text-align: center; color: var(--muted); }}
.failures {{ background: #fff8e9; border: 1px solid #f0d99a; border-radius: 8px; padding: 10px 14px; margin: 18px 0; font-size: 13px; color: #6b4a00; }}
footer {{ margin-top: 60px; padding-top: 18px; border-top: 1px solid var(--border); font-size: 12px; color: var(--muted); }}
footer a {{ color: var(--muted); }}
.nav {{ display: flex; gap: 14px; margin-bottom: 18px; font-size: 13px; }}
.nav a {{ color: var(--accent); text-decoration: none; }}
.nav a:hover {{ text-decoration: underline; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #181815; --fg: #e8e6df; --muted: #8a8780; --card: #232320; --border: #353330; --chip: #2a2825; --accent: #f29349; }}
  .paper .meta .src.PPR {{ background: #3a2a13; color: #f0c388; }}
  .paper .meta .src.MED {{ background: #1a2a3a; color: #93c0e8; }}
  .paper .meta .src.PMC {{ background: #1d3322; color: #8fd29a; }}
  .summary, .paper {{ box-shadow: 0 1px 0 rgba(0,0,0,0.2); }}
  .failures {{ background: #2e2615; border-color: #5a4818; color: #e8c478; }}
  .paper .meta .pinned-tag {{ background: #3a2a13; color: #f0c388; }}
  .chip.pinned {{ background: #3a2a13; border-color: #5a4818; color: #f0c388; }}
}}
</style>
</head>
<body>
<div class="wrap">
<div class="nav"><a href="../index.html">← All digests</a></div>
<header>
  <h1>Paper Digest</h1>
  <div class="meta">{date_long} · Europe PMC (PubMed + bioRxiv + medRxiv + others) · <b>{total}</b> unique paper(s)</div>
</header>
"""

HTML_TAIL = """
<footer>
  Generated {generated_at} KST · Source: <a href="https://europepmc.org" target="_blank">Europe PMC</a> ·
  Window: <code>CREATION_DATE = {date}</code> ·
  <a href="https://europepmc.org/Help#searchcommands" target="_blank">EPMC search syntax</a>
</footer>
</div>
<script>
(function() {{
  var state = {{ keyword: 'all', source: 'all', q: '' }};
  function apply() {{
    var q = state.q.toLowerCase();
    document.querySelectorAll('.paper').forEach(function(el) {{
      var kws = (el.dataset.keywords || '').split('|');
      var src = el.dataset.source || '';
      var hay = (el.dataset.search || '').toLowerCase();
      var ok = true;
      if (state.keyword !== 'all' && kws.indexOf(state.keyword) === -1) ok = false;
      if (state.source !== 'all' && src !== state.source) ok = false;
      if (q && hay.indexOf(q) === -1) ok = false;
      el.classList.toggle('hidden', !ok);
    }});
    var visible = document.querySelectorAll('.paper:not(.hidden)').length;
    document.getElementById('empty').style.display = visible ? 'none' : 'block';
    document.getElementById('visibleCount').textContent = visible;
  }}
  document.querySelectorAll('.chip[data-filter]').forEach(function(chip) {{
    chip.addEventListener('click', function() {{
      var dim = chip.dataset.filter;
      var val = chip.dataset.value;
      state[dim] = val;
      document.querySelectorAll('.chip[data-filter="' + dim + '"]').forEach(function(c) {{ c.classList.remove('active'); }});
      chip.classList.add('active');
      apply();
    }});
  }});
  var search = document.getElementById('search');
  if (search) search.addEventListener('input', function(e) {{ state.q = e.target.value; apply(); }});
  apply();
}})();
</script>
</body>
</html>
"""


def render_html(target_date: dt.date, papers: list[dict], failed: list[tuple[str, str]],
                keywords: list[str], pinned_journals: list[str]) -> str:
    total = len(papers)

    # counts
    by_src: dict[str, int] = defaultdict(int)
    by_journal: dict[str, int] = defaultdict(int)
    by_kw: dict[str, int] = defaultdict(int)
    by_pinned: dict[str, int] = defaultdict(int)
    pinned_count = 0
    for p in papers:
        by_src[p.get("source") or "?"] += 1
        j = journal_of(p) or "(unspecified)"
        by_journal[j] += 1
        for k in p.get("matched_keywords", []):
            by_kw[k] += 1
        if p.get("matched_journals"):
            pinned_count += 1
            for pj in p["matched_journals"]:
                by_pinned[pj] += 1

    # build summary blocks
    src_lis = "".join(
        f'<li><span>{html.escape(source_label(s))}</span><b>{n}</b></li>'
        for s, n in sorted(by_src.items(), key=lambda kv: -kv[1])
    )
    journal_top = sorted(by_journal.items(), key=lambda kv: -kv[1])[:8]
    journal_lis = "".join(
        f'<li><span>{html.escape(j)}</span><b>{n}</b></li>'
        for j, n in journal_top
    ) or '<li><span style="color:var(--muted)">(none)</span><b></b></li>'
    kw_lis = "".join(
        f'<li><span>{html.escape(k)}</span><b>{by_kw.get(k, 0)}</b></li>'
        for k in keywords
    )
    pinned_lis = "".join(
        f'<li><span>📌 {html.escape(j)}</span><b>{by_pinned.get(j, 0)}</b></li>'
        for j in pinned_journals
    )

    # filter chips
    kw_chips = ['<span class="chip active" data-filter="keyword" data-value="all">All <span class="count" id="visibleCount">{}</span></span>'.format(total)]
    for k in keywords:
        n = by_kw.get(k, 0)
        kw_chips.append(
            f'<span class="chip" data-filter="keyword" data-value="{html.escape(k, quote=True)}">{html.escape(k)}<span class="count">{n}</span></span>'
        )
    if pinned_count:
        kw_chips.append(
            f'<span class="chip pinned" data-filter="keyword" data-value="__pinned__">📌 Pinned journals<span class="count">{pinned_count}</span></span>'
        )
    kw_chip_html = "".join(kw_chips)

    src_chips = ['<span class="chip active" data-filter="source" data-value="all">All sources</span>']
    for s, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        src_chips.append(
            f'<span class="chip" data-filter="source" data-value="{html.escape(s, quote=True)}">{html.escape(source_label(s))}<span class="count">{n}</span></span>'
        )
    src_chip_html = "".join(src_chips)

    # papers
    paper_blocks = []
    for p in papers:
        title = html.escape(p.get("title") or "(no title)")
        url = epmc_url(p)
        authors = html.escape(fmt_authors(p))
        journal = html.escape(journal_of(p))
        pubdate = html.escape(p.get("firstPublicationDate") or "")
        src = p.get("source") or "?"
        kws = p.get("matched_keywords", [])
        ab = abstract_excerpt(p)
        ab_html = f'<div class="ab">{html.escape(ab)}</div>' if ab else ""
        doi_link = doi_url(p)
        doi_html = (f'<div class="doi"><a href="{html.escape(doi_link)}" target="_blank" rel="noopener">{html.escape(p.get("doi"))}</a></div>'
                    if doi_link else "")

        pinned_for_paper = p.get("matched_journals") or []

        meta_parts = [f'<span class="src {html.escape(src)}">{html.escape(source_label(src))}</span>']
        if journal:
            meta_parts.append(f'<span>{journal}</span>')
        if pubdate:
            meta_parts.append(f'<span>{pubdate}</span>')
        for k in kws:
            meta_parts.append(f'<span class="kw">{html.escape(k)}</span>')
        for pj in pinned_for_paper:
            meta_parts.append(f'<span class="kw pinned-tag">📌 {html.escape(pj)}</span>')
        meta_html = "".join(meta_parts)

        # search index text
        search_text = " ".join([
            p.get("title") or "",
            p.get("abstractText") or "",
            p.get("authorString") or "",
            journal_of(p) or "",
            " ".join(kws),
            " ".join(pinned_for_paper),
        ])
        kw_values = list(kws)
        if pinned_for_paper:
            kw_values.append("__pinned__")
        data_kw = "|".join(html.escape(v, quote=True) for v in kw_values)

        paper_blocks.append(
            f'''<article class="paper" data-keywords="{data_kw}" data-source="{html.escape(src, quote=True)}" data-search="{html.escape(search_text, quote=True)}">
  <h3><a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a></h3>
  {f'<div class="by">{authors}</div>' if authors else ''}
  <div class="meta">{meta_html}</div>
  {ab_html}
  {doi_html}
</article>'''
        )

    failures_html = ""
    if failed:
        items = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(e)}</li>" for k, e in failed)
        failures_html = f'<div class="failures"><b>Some keyword fetches failed:</b><ul>{items}</ul></div>'

    body = []
    body.append(HTML_HEAD.format(
        date=target_date.isoformat(),
        date_long=target_date.strftime("%A, %B %d, %Y"),
        total=total,
    ))
    pinned_block = (
        f'<div class="col"><h3>📌 Pinned journals</h3><ul>{pinned_lis}</ul></div>'
        if pinned_journals else ""
    )
    body.append(f'''
<section class="summary">
  <h2>Summary</h2>
  <div class="summary-grid">
    <div class="col"><h3>By source</h3><ul>{src_lis or '<li><span style="color:var(--muted)">(none)</span><b></b></li>'}</ul></div>
    <div class="col"><h3>Top journals</h3><ul>{journal_lis}</ul></div>
    <div class="col"><h3>By keyword</h3><ul>{kw_lis}</ul></div>
    {pinned_block}
  </div>
</section>
''')
    body.append(failures_html)

    if total == 0:
        body.append(f'<div class="empty">No new Europe PMC entries matched the watched keywords for {target_date.isoformat()}.</div>')
    else:
        body.append(f'''
<div class="filter-row"><div class="lbl">Keyword</div><div class="filters">{kw_chip_html}</div></div>
<div class="filter-row"><div class="lbl">Source</div><div class="filters">{src_chip_html}</div></div>
<div class="search-box"><input id="search" type="search" placeholder="Filter by title, author, journal, abstract…"></div>
<div class="papers">
{''.join(paper_blocks)}
</div>
<div id="empty" class="empty" style="display:none">No papers match the current filters.</div>
''')

    body.append(HTML_TAIL.format(
        date=target_date.isoformat(),
        generated_at=kst_now().strftime("%Y-%m-%d %H:%M"),
    ))
    return "".join(body)


# ---------------------------------------------------------------------------
# index.html (list of all daily digests)
# ---------------------------------------------------------------------------

def update_index(latest_summary: dict) -> None:
    """Rewrites index.html. Reads ./data/*.json for per-day metadata."""
    rows = []
    if DATA_DIR.exists():
        files = sorted(DATA_DIR.glob("*.json"), reverse=True)
        for f in files:
            if f.name.startswith("_"):
                continue  # auxiliary chunk/summary files
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(meta, dict):
                continue
            d = meta.get("date") or f.stem
            n = meta.get("total", 0)
            top_src = meta.get("by_source", {})
            top_kw = meta.get("by_keyword", {})
            pinned_n = meta.get("pinned_paper_count", 0)
            src_str = ", ".join(f"{source_label(s)} {c}" for s, c in sorted(top_src.items(), key=lambda kv: -kv[1])[:3]) or "—"
            kw_str = ", ".join(f"{k} {c}" for k, c in sorted(top_kw.items(), key=lambda kv: -kv[1])[:3] if c) or "—"
            pinned_str = f" · 📌 {pinned_n}" if pinned_n else ""
            rows.append(f'''<a class="row" href="digests/{d}.html">
  <div class="d">{d}</div>
  <div class="n">{n} <span style="color:var(--muted);font-size:13px">paper(s){html.escape(pinned_str)}</span></div>
  <div class="info"><span>{html.escape(src_str)}</span><span>{html.escape(kw_str)}</span></div>
</a>''')

    rows_html = "\n".join(rows) if rows else '<div class="empty">No digests yet.</div>'

    INDEX_PATH.write_text(f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Research Digest — Index</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{
  --bg: #fafaf7; --fg: #1c1c1a; --muted: #6b6b66; --accent: #b85c00;
  --card: #ffffff; --border: #e5e3dc;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }}
.wrap {{ max-width: 720px; margin: 0 auto; padding: 40px 20px 80px; }}
h1 {{ font-size: 28px; letter-spacing: -0.01em; margin: 0 0 6px; }}
.sub {{ color: var(--muted); font-size: 14px; margin-bottom: 28px; }}
.row {{ display: grid; grid-template-columns: 110px 110px 1fr; gap: 16px; align-items: baseline;
  padding: 14px 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--card);
  text-decoration: none; color: var(--fg); margin-bottom: 8px; transition: border-color 0.12s; }}
.row:hover {{ border-color: var(--accent); }}
.d {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
.n {{ font-variant-numeric: tabular-nums; }}
.info {{ display: flex; flex-direction: column; gap: 4px; color: var(--muted); font-size: 13px; }}
.empty {{ padding: 40px; text-align: center; color: var(--muted); border: 1px dashed var(--border); border-radius: 10px; background: var(--card); }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #181815; --fg: #e8e6df; --muted: #8a8780; --card: #232320; --border: #353330; --accent: #f29349; }}
}}
</style>
</head>
<body>
<div class="wrap">
<h1>Research Digest</h1>
<div class="sub">Daily Europe PMC paper digest. Most recent first.</div>
{rows_html}
</div>
</body>
</html>
""", encoding="utf-8")


# ---------------------------------------------------------------------------
# Slack-friendly summary string
# ---------------------------------------------------------------------------

def format_slack_summary(target_date: dt.date, papers: list[dict], failed: list[tuple[str, str]],
                          keywords: list[str], pinned_journals: list[str], html_path: Path) -> str:
    total = len(papers)

    by_src: dict[str, int] = defaultdict(int)
    by_journal: dict[str, int] = defaultdict(int)
    by_kw: dict[str, int] = defaultdict(int)
    by_pinned: dict[str, int] = defaultdict(int)
    pinned_count = 0
    for p in papers:
        by_src[p.get("source") or "?"] += 1
        j = journal_of(p) or "(unspecified)"
        by_journal[j] += 1
        for k in p.get("matched_keywords", []):
            by_kw[k] += 1
        if p.get("matched_journals"):
            pinned_count += 1
            for pj in p["matched_journals"]:
                by_pinned[pj] += 1

    if total == 0:
        body = [f"*Paper Digest — {target_date.isoformat()}* · *0* new",
                "_Europe PMC (PubMed + preprints)_",
                "",
                "No new entries matched the watched keywords today."]
    else:
        src_str = ", ".join(f"{source_label(s)} {c}" for s, c in sorted(by_src.items(), key=lambda kv: -kv[1])) or "—"
        top_journals = sorted(by_journal.items(), key=lambda kv: -kv[1])[:5]
        journals_str = ", ".join(f"{j} ({c})" for j, c in top_journals) or "—"
        kw_str = ", ".join(f"{k} {by_kw.get(k, 0)}" for k in keywords)

        body = [
            f"*Paper Digest — {target_date.isoformat()}* · *{total}* new",
            f"• *Source:* {src_str}",
            f"• *Top journals:* {journals_str}",
            f"• *Keyword hits:* {kw_str}",
        ]
        if pinned_journals:
            pinned_str = ", ".join(f"{j} {by_pinned.get(j, 0)}" for j in pinned_journals)
            body.append(f"• 📌 *Pinned journals:* {pinned_str}  _({pinned_count} unique paper(s))_")
    body.append("")
    body.append(f"📄 Full digest: `{html_path}`")
    if failed:
        body.append(f"⚠️ Failed fetches: {', '.join(k for k, _ in failed)}")
    return "\n".join(body)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _process_one_day(target_date: dt.date, config: dict, keywords: list[str],
                     pinned_journals: list[str]) -> tuple[Path, list[dict], list[tuple[str, str]]]:
    """Run the digest pipeline for a single date — write HTML + JSON. Return (html_path, papers, failed)."""
    papers, kw_to_keys, failed = collect(config, target_date)
    html_text = render_html(target_date, papers, failed, keywords, pinned_journals)
    html_path = DIGESTS_DIR / f"{target_date.isoformat()}.html"
    html_path.write_text(html_text, encoding="utf-8")

    by_src: dict[str, int] = defaultdict(int)
    by_kw: dict[str, int] = defaultdict(int)
    by_pinned: dict[str, int] = defaultdict(int)
    pinned_count = 0
    for p in papers:
        by_src[p.get("source") or "?"] += 1
        for k in p.get("matched_keywords", []):
            by_kw[k] += 1
        if p.get("matched_journals"):
            pinned_count += 1
            for pj in p["matched_journals"]:
                by_pinned[pj] += 1
    meta = {
        "date": target_date.isoformat(),
        "total": len(papers),
        "by_source": dict(by_src),
        "by_keyword": {k: by_kw.get(k, 0) for k in keywords},
        "by_pinned_journal": {j: by_pinned.get(j, 0) for j in pinned_journals},
        "pinned_paper_count": pinned_count,
        "failed": [k for k, _ in failed],
        "generated_at": kst_now().isoformat(),
    }
    (DATA_DIR / f"{target_date.isoformat()}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return html_path, papers, failed


def main(argv: list[str]) -> int:
    config = load_config()
    keywords = config["keywords"]
    pinned_journals = config.get("always_include_journals", []) or []

    DIGESTS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    target_dates = resolve_target_dates(argv)

    results = []  # (date, html_path, papers, failed)
    last_meta = None
    for target_date in target_dates:
        html_path, papers, failed = _process_one_day(target_date, config, keywords, pinned_journals)
        results.append((target_date, html_path, papers, failed))
        last_meta = json.loads((DATA_DIR / f"{target_date.isoformat()}.json").read_text(encoding="utf-8"))

    # update index once with last day's meta (any meta works — update_index reads all data/*.json)
    if last_meta is not None:
        update_index(last_meta)

    # Print Slack-friendly summary
    if len(results) == 1:
        d, html_path, papers, failed = results[0]
        print(format_slack_summary(d, papers, failed, keywords, pinned_journals, html_path))
    else:
        # Multi-day backfill — print one combined summary
        all_papers: list[dict] = []
        all_failed: list[tuple[str, str]] = []
        for _, _, papers, failed in results:
            all_papers.extend(papers)
            all_failed.extend(failed)
        first, last = results[0][0], results[-1][0]
        title = (f"*Paper Digest — {first.isoformat()} ~ {last.isoformat()}*"
                 f" · *{len(all_papers)}* new across {len(results)} days")
        print(title)
        for d, html_path, papers, failed in results:
            print(f"• `{d.isoformat()}` · {len(papers)}편 → `{html_path}`")
        if all_failed:
            print(f"⚠️ Failed fetches: {', '.join(k for k, _ in all_failed)}")

    # Exit code: 2 only if every requested date had complete failure
    any_success = any(len(papers) > 0 or not failed for _, _, papers, failed in results)
    return 0 if any_success else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
