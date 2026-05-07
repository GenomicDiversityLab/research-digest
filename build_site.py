#!/usr/bin/env python3
"""
Site renderer for the Research Digest GitHub Pages site.

Reads every per-day raw paper file in ./data/ (and the legacy
data/_week_raw.json snapshot) plus the Korean summary files, and rewrites:
  - index.html              landing page (daily list + keyword section)
  - digests/<date>.html     per-day detail
  - keywords/<slug>.html    per-keyword aggregate (newest first)

Cross-links between pages are relative so the site works locally and on
GitHub Pages without configuration.

Run:
    python3 build_site.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DIGESTS = ROOT / "digests"
KEYWORDS_DIR = ROOT / "keywords"

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
KEYWORDS: list[str] = CONFIG["keywords"]
PINNED_JOURNALS: list[str] = CONFIG.get("always_include_journals", []) or []

IF_LOOKUP_RAW = json.loads((ROOT / "journal_if.json").read_text(encoding="utf-8"))["if"]
IF_LOOKUP = {k.lower(): v for k, v in IF_LOOKUP_RAW.items()}

SUMS_HIGH = json.loads((DATA / "_summaries_high.json").read_text(encoding="utf-8")) \
    if (DATA / "_summaries_high.json").exists() else {}
SUMS_LOW = json.loads((DATA / "_summaries_low.json").read_text(encoding="utf-8")) \
    if (DATA / "_summaries_low.json").exists() else {}


# ---------------------------------------------------------------------------
# theme
# ---------------------------------------------------------------------------

# Per-keyword color palette: (light bg, light fg)
KEYWORD_PALETTE: dict[str, tuple[str, str]] = {
    "somatic mosaicism": ("#ede9fe", "#5b21b6"),  # violet
    "mosaic":            ("#ffedd5", "#9a3412"),  # orange
    "mosaic variant":    ("#fce7f3", "#9d174d"),  # pink
    "methylation":       ("#dbeafe", "#1e3a8a"),  # blue
    "pacbio":            ("#d1fae5", "#065f46"),  # green
}
KEYWORD_PALETTE_DARK: dict[str, tuple[str, str]] = {
    "somatic mosaicism": ("#3b1d6b", "#c4b5fd"),
    "mosaic":            ("#3a2210", "#fdba74"),
    "mosaic variant":    ("#3a1626", "#f9a8d4"),
    "methylation":       ("#10204a", "#93c5fd"),
    "pacbio":            ("#0c2a23", "#86efac"),
}
PINNED_LIGHT = ("#fef3c7", "#854d0e")
PINNED_DARK = ("#3a2d0d", "#fde68a")


def slug_for_keyword(kw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")
    return s or "keyword"


def kw_palette(kw: str, dark: bool = False) -> tuple[str, str]:
    pool = KEYWORD_PALETTE_DARK if dark else KEYWORD_PALETTE
    return pool.get(kw.lower(), ("#e5e7eb", "#374151") if not dark else ("#2a2825", "#cccdd1"))


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(s: str) -> str:
    if not s:
        return ""
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", s)).strip()


def journal_of(p: dict) -> str:
    j = (p.get("journalTitle") or "").strip() if isinstance(p.get("journalTitle"), str) else ""
    if not j:
        ji = p.get("journalInfo") or {}
        if isinstance(ji, dict):
            jr = ji.get("journal") or {}
            if isinstance(jr, dict):
                j = (jr.get("title") or "").strip()
    return j


def fmt_authors(p: dict, max_n: int = 3) -> str:
    raw = p.get("authorString") or ""
    names = [s.strip() for s in raw.split(",") if s.strip()]
    if not names:
        return ""
    if len(names) <= max_n:
        return ", ".join(names)
    return ", ".join(names[:max_n]) + ", et al."


def epmc_url(p: dict) -> str:
    return f"https://europepmc.org/abstract/{p.get('source','MED')}/{p.get('id','')}"


def doi_url(p: dict) -> str | None:
    d = p.get("doi")
    return f"https://doi.org/{d}" if d else None


def source_label(s: str) -> str:
    return {"MED": "PubMed", "PMC": "PMC", "PPR": "Preprint"}.get(s, s)


def short_journal(j: str) -> str:
    """Shorten verbose journal names for chip display."""
    j = j.split(":")[0]  # drop subtitle
    j = j.replace("Proceedings of the National Academy of Sciences of the United States of America", "PNAS")
    j = j.replace("Journal of the American Chemical Society", "JACS")
    j = j.replace("Alzheimer's & dementia", "Alz Dem")
    return j


def kst_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------

def load_all_papers() -> dict[str, dict]:
    """Returns uid -> enriched paper record (with `_dates`, `_if`, `_journal`)."""
    by_uid: dict[str, dict] = {}

    def upsert(p: dict, date_str: str) -> None:
        uid = f"{p.get('source','?')}-{p.get('id','?')}"
        if uid in by_uid:
            by_uid[uid].setdefault("_dates", set()).add(date_str)
            # merge matched_keywords / matched_journals
            for k in p.get("matched_keywords", []) or []:
                if k not in by_uid[uid].get("matched_keywords", []):
                    by_uid[uid].setdefault("matched_keywords", []).append(k)
            for j in p.get("matched_journals", []) or []:
                if j not in by_uid[uid].get("matched_journals", []):
                    by_uid[uid].setdefault("matched_journals", []).append(j)
            return
        rec = dict(p)
        rec["_dates"] = {date_str}
        rec.setdefault("matched_keywords", [])
        rec.setdefault("matched_journals", [])
        j = journal_of(rec)
        rec["_journal"] = j
        rec["_if"] = IF_LOOKUP.get(j.lower()) if j else None
        by_uid[uid] = rec

    # Per-day raw files (preferred — produced by paper_digest.py)
    for f in sorted(DATA.glob("????-??-??_papers.json")):
        date_str = f.name.replace("_papers.json", "")
        try:
            arr = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in arr:
            upsert(p, date_str)

    # Legacy weekly snapshot (used to backfill old days that pre-date the per-day files)
    legacy = DATA / "_week_raw.json"
    if legacy.exists():
        try:
            week = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            week = {}
        for date_str, arr in week.items():
            # Only use legacy entries when no per-day file exists for that date
            if (DATA / f"{date_str}_papers.json").exists():
                continue
            for p in arr:
                upsert(p, date_str)

    # finalize: convert _dates set to sorted list
    for uid, rec in by_uid.items():
        rec["_dates"] = sorted(rec["_dates"], reverse=True)
        rec["_first_seen"] = rec["_dates"][-1]
        rec["_latest"] = rec["_dates"][0]

    return by_uid


def load_per_day_meta() -> dict[str, dict]:
    """Returns date_str -> meta dict from data/<date>.json."""
    out: dict[str, dict] = {}
    for f in sorted(DATA.glob("????-??-??.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[m.get("date", f.stem)] = m
    return out


# ---------------------------------------------------------------------------
# CSS (shared across all pages)
# ---------------------------------------------------------------------------

def render_css() -> str:
    parts = ["""
:root {
  color-scheme: light dark;
  --bg: #fafaf7; --fg: #1c1c1a; --muted: #6b6b66; --soft: #4a4a45;
  --accent: #b85c00; --accent-soft: #fef3e6;
  --card: #ffffff; --border: #e5e3dc; --chip: #f0ede4;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.04);
  --radius: 12px;
}
* { box-sizing: border-box; }
html, body { margin:0; padding:0; background:var(--bg); color:var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
    "Apple SD Gothic Neo", "Malgun Gothic", "Pretendard", sans-serif;
  font-size: 15px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 920px; margin: 0 auto; padding: 28px 20px 80px; }
a { color: var(--accent); }
header.hero { display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
  padding: 12px 0 8px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
header.hero h1 { margin: 0 0 2px; font-size: 26px; letter-spacing: -0.02em; font-weight: 700; }
header.hero .meta { color: var(--muted); font-size: 13px; }
header.hero .home-link { font-size: 13px; color: var(--muted); text-decoration: none; }
header.hero .home-link:hover { color: var(--accent); }

/* Day tabs (daily detail pages) */
.daynav { display:flex; align-items:center; flex-wrap: wrap; gap: 10px;
  background: var(--card); border:1px solid var(--border); border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 18px; font-size: 13px; box-shadow: var(--shadow); }
.daynav .arrow, .daynav .home { color: var(--muted); text-decoration:none; padding: 4px 8px;
  border-radius: 6px; }
.daynav .arrow:hover, .daynav .home:hover { background: var(--chip); color: var(--accent); }
.daynav .arrow.disabled { opacity: 0.35; cursor: default; }
.daynav .center { display:flex; gap:4px; flex-wrap:wrap; }
.daynav .center a, .daynav .center span { color: var(--fg); text-decoration:none;
  padding: 4px 9px; border-radius: 6px; font-variant-numeric: tabular-nums; }
.daynav .center a:hover { background: var(--chip); }
.daynav .center .today { background: var(--accent); color:#fff; font-weight:600; }
.daynav .center .empty { color: var(--muted); }

/* Summary card */
.summary { background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
  padding: 16px 20px; margin: 0 0 22px; box-shadow: var(--shadow); }
.summary h2 { margin:0 0 10px; font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); font-weight:600; }
.summary-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px 28px; }
.summary-grid .col h3 { margin:0 0 6px; font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
.summary-grid .col ul { margin:0; padding:0; list-style:none; font-size:13px; }
.summary-grid .col li { display:flex; justify-content:space-between; gap:8px; padding:2px 0;
  border-bottom: 1px dashed var(--border); }
.summary-grid .col li:last-child { border-bottom: 0; }
.summary-grid .col li b { font-weight:600; font-variant-numeric: tabular-nums; }

/* Filter chips */
.filter-row { display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; margin-bottom: 8px; }
.filter-row .lbl { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em;
  font-weight:600; min-width:64px; padding-top: 4px; }
.filters { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-flex; align-items:center; padding: 4px 11px; border-radius: 999px;
  background: var(--chip); border:1px solid var(--border); font-size:12px;
  cursor:pointer; user-select:none; transition: all 0.12s; text-decoration:none; color: inherit; }
.chip:hover { border-color: var(--accent); }
.chip.active { background: var(--accent); color:#fff; border-color: var(--accent); }
.chip .count { opacity: 0.65; margin-left: 6px; font-variant-numeric: tabular-nums; font-size: 11px; }
.chip.active .count { opacity: 0.95; }

/* Per-keyword color chips (kw-* class injected per page) */
""".strip()]

    # Per-keyword colored chips
    for kw in KEYWORDS:
        slug = slug_for_keyword(kw)
        bg, fg = kw_palette(kw)
        bgd, fgd = kw_palette(kw, dark=True)
        parts.append(f"""
.chip.kw-{slug}, .tag.kw-{slug} {{ background: {bg}; color: {fg}; border-color: {bg}; }}
.chip.kw-{slug}:hover {{ filter: brightness(0.95); border-color: {fg}; }}
.chip.kw-{slug}.active {{ background: {fg}; color: #fff; border-color: {fg}; }}
@media (prefers-color-scheme: dark) {{
  .chip.kw-{slug}, .tag.kw-{slug} {{ background: {bgd}; color: {fgd}; border-color: {bgd}; }}
  .chip.kw-{slug}.active {{ background: {fgd}; color: #181815; border-color: {fgd}; }}
}}""".strip())

    bg, fg = PINNED_LIGHT
    bgd, fgd = PINNED_DARK
    parts.append(f"""
.chip.pinned, .tag.pinned {{ background: {bg}; color: {fg}; border-color: {bg}; font-weight: 600; }}
.chip.pinned:hover {{ filter: brightness(0.95); border-color: {fg}; }}
.chip.pinned.active {{ background: {fg}; color: #fff; border-color: {fg}; }}
@media (prefers-color-scheme: dark) {{
  .chip.pinned, .tag.pinned {{ background: {bgd}; color: {fgd}; border-color: {bgd}; }}
  .chip.pinned.active {{ background: {fgd}; color: #181815; border-color: {fgd}; }}
}}""".strip())

    parts.append("""
.search-box { width:100%; margin: 8px 0 18px; }
.search-box input { width:100%; padding: 10px 14px; border:1px solid var(--border); border-radius: 8px;
  font-size: 14px; background: var(--card); color: var(--fg); transition: border-color 0.12s; }
.search-box input:focus { outline: none; border-color: var(--accent); }

.papers { display:flex; flex-direction:column; gap: 12px; }
.paper { background:var(--card); border:1px solid var(--border); border-radius: var(--radius);
  padding: 16px 20px; transition: border-color 0.12s, box-shadow 0.12s; box-shadow: var(--shadow); }
.paper:hover { border-color: var(--accent); box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 6px 18px rgba(184,92,0,0.10); }
.paper.hidden { display:none; }
.paper.hi { border-color: #f0c388; background: linear-gradient(0deg, var(--accent-soft), var(--card) 70%); }
.paper h3 { margin: 0 0 6px; font-size: 16px; line-height: 1.4; font-weight: 600; }
.paper h3 a { color: var(--fg); text-decoration: none; }
.paper h3 a:hover { color: var(--accent); }
.paper .if-tag { display:inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 4px;
  background: #fff3df; color: #8a4b00; font-size: 11px; font-weight: 700; vertical-align: 2px;
  letter-spacing: 0.02em; }
.paper.hi .if-tag { background: var(--accent); color: #fff; }
.paper .by { font-size: 12px; color: var(--muted); margin: 0 0 6px; }
.paper .meta { font-size: 11px; color: var(--muted); margin: 0 0 8px; display: flex;
  flex-wrap: wrap; gap: 4px 10px; align-items: center; }
.paper .meta .src { display:inline-block; padding: 1px 7px; border-radius: 4px; font-weight: 700;
  letter-spacing: 0.02em; font-size: 10px; background: var(--chip); }
.paper .meta .src.PPR { background:#fff3df; color:#8a4b00; }
.paper .meta .src.MED { background:#e3eef6; color:#1f4e75; }
.paper .meta .src.PMC { background:#e3f4e6; color:#2a6b35; }
.paper .meta .date { font-variant-numeric: tabular-nums; }
.tag { display:inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px;
  background: var(--chip); border: 1px solid var(--border); text-decoration: none;
  font-weight: 500; transition: all 0.12s; }
.tag:hover { filter: brightness(0.95); }
.paper .ksum { background: #fffefa; border:1px solid #efe9d8; border-radius: 8px; padding: 10px 14px;
  margin: 8px 0 4px; font-size: 14px; line-height: 1.7; color: #2c2c28; }
.paper.hi .ksum { background: #fffaf2; border-color: #efd9b8; }
.paper .ksum .label { display:inline-block; font-size: 10px; font-weight: 700; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.06em; margin-right: 6px; vertical-align: 2px; }
.paper .ksum-struct { display: flex; flex-direction: column; gap: 8px; }
.paper .ksum-struct .line { display: flex; gap: 10px; }
.paper .ksum-struct .lbl { flex: 0 0 56px; font-size: 11px; font-weight: 700; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.04em; padding-top: 3px; }
.paper .ksum-struct .body { flex: 1 1 auto; }
.paper .ksum-struct ul { margin: 2px 0 0; padding-left: 18px; }
.paper .doi a { color: var(--muted); font-size: 11px; text-decoration: none; }
.paper .doi a:hover { color: var(--accent); }
.paper .ab { font-size: 13px; color: var(--soft); margin: 6px 0 0; }

/* date headers used on per-keyword pages */
.date-head { display:flex; align-items:baseline; gap: 12px; margin: 26px 0 8px;
  padding-bottom: 6px; border-bottom: 1px dashed var(--border); }
.date-head h2 { margin: 0; font-size: 14px; font-weight: 700; letter-spacing: -0.01em; }
.date-head .day-meta { font-size: 12px; color: var(--muted); }

/* index page */
.kw-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px; margin: 14px 0 28px; }
.kw-card { display:block; padding: 14px 16px; border-radius: var(--radius); background: var(--card);
  border:1px solid var(--border); text-decoration: none; color: var(--fg); transition: all 0.12s;
  box-shadow: var(--shadow); }
.kw-card:hover { border-color: var(--accent); transform: translateY(-1px); box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 8px 22px rgba(184,92,0,0.10); }
.kw-card .name { display:flex; align-items:center; gap: 8px; font-weight: 700; font-size: 15px;
  margin-bottom: 4px; }
.kw-card .swatch { width: 10px; height: 10px; border-radius: 999px; }
.kw-card .stats { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.kw-card .recent { margin-top: 6px; font-size: 11px; color: var(--muted); }

/* day list */
.daylist { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
.daylist .row { display:grid; grid-template-columns: 110px 200px 1fr; gap: 14px;
  align-items: baseline; padding: 12px 16px; border:1px solid var(--border); border-radius: var(--radius);
  background: var(--card); text-decoration:none; color: var(--fg); transition: border-color 0.12s; }
.daylist .row:hover { border-color: var(--accent); }
.daylist .d { font-variant-numeric: tabular-nums; font-weight: 600; }
.daylist .n { font-variant-numeric: tabular-nums; font-size: 14px; }
.daylist .hi-pill { display:inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
  background:#fff0d6; color:#8a4b00; font-size: 11px; font-weight: 600; }
.daylist .info { display:flex; flex-direction: column; gap: 2px; color: var(--muted); font-size: 12px; }

.empty { background: var(--card); border:1px dashed var(--border); border-radius: var(--radius);
  padding: 30px; text-align: center; color: var(--muted); }
.section-h { margin: 30px 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); font-weight: 700; }
footer { margin-top: 60px; padding-top: 16px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--muted); }
footer a { color: var(--muted); }

@media (prefers-color-scheme: dark) {
  :root { --bg:#181815; --fg:#e8e6df; --muted:#8a8780; --soft:#bbbab2; --card:#232320;
    --border:#353330; --chip:#2a2825; --accent:#f29349; --accent-soft:#2a2014;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 4px 12px rgba(0,0,0,0.3); }
  .paper .meta .src.PPR { background:#3a2a13; color:#f0c388; }
  .paper .meta .src.MED { background:#1a2a3a; color:#93c0e8; }
  .paper .meta .src.PMC { background:#1d3322; color:#8fd29a; }
  .paper .ksum { background:#1f1f1c; border-color:#3a3833; color: #e0ddd2; }
  .paper.hi .ksum { background:#2a2419; border-color:#4a3e22; }
  .paper.hi { background: linear-gradient(0deg, #2a2014, var(--card) 70%); }
  .daylist .hi-pill { background:#3a2a13; color:#f0c388; }
}
""".rstrip())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------

def render_paper_card(p: dict, *, base: str, with_date: bool = False) -> str:
    """`base` is the relative path prefix to keyword pages, e.g. '../keywords/' or 'keywords/'."""
    uid = f"{p.get('source','?')}-{p.get('id','?')}"
    title = html.escape(p.get("title") or "(제목 없음)")
    url = epmc_url(p)
    authors = html.escape(fmt_authors(p))
    journal = html.escape(p.get("_journal") or journal_of(p))
    pubdate = html.escape(p.get("firstPublicationDate") or "")
    src = p.get("source") or "?"
    kws: list[str] = p.get("matched_keywords") or []
    pinned: list[str] = p.get("matched_journals") or []
    ifv = p.get("_if")
    if ifv is None:
        j = p.get("_journal") or journal_of(p)
        ifv = IF_LOOKUP.get(j.lower()) if j else None
    is_hi = ifv is not None and ifv >= 10
    if_tag = f'<span class="if-tag">IF={ifv:.1f}</span>' if ifv is not None else ""

    # Korean summary
    ksum_html = ""
    if uid in SUMS_HIGH and is_hi:
        s = SUMS_HIGH[uid]
        res_lis = "".join(f"<li>{html.escape(r)}</li>" for r in s.get("results", []))
        ksum_html = f'''<div class="ksum"><div class="ksum-struct">
  <div class="line"><div class="lbl">배경</div><div class="body">{html.escape(s.get("background",""))}</div></div>
  <div class="line"><div class="lbl">결과</div><div class="body"><ul>{res_lis}</ul></div></div>
  <div class="line"><div class="lbl">결론</div><div class="body">{html.escape(s.get("conclusion",""))}</div></div>
  <div class="line"><div class="lbl">의의</div><div class="body">{html.escape(s.get("significance",""))}</div></div>
</div></div>'''
    elif uid in SUMS_LOW:
        ksum_html = (f'<div class="ksum"><span class="label">한글 요약</span>'
                     f'{html.escape(SUMS_LOW[uid].get("summary",""))}</div>')
    else:
        # fallback: cleaned abstract excerpt
        ab = strip_tags(p.get("abstractText") or "")[:280]
        if ab:
            ksum_html = f'<div class="ab">{html.escape(ab)}…</div>'

    # meta line
    meta_parts = [f'<span class="src {html.escape(src)}">{html.escape(source_label(src))}</span>']
    if journal:
        meta_parts.append(f'<span>{journal}</span>')
    if pubdate:
        meta_parts.append(f'<span class="date">{pubdate}</span>')
    if with_date and p.get("_latest"):
        meta_parts.append(f'<span class="date">EPMC: {html.escape(p["_latest"])}</span>')
    for k in kws:
        slug = slug_for_keyword(k)
        meta_parts.append(
            f'<a class="tag kw-{slug}" href="{base}{slug}.html">{html.escape(k)}</a>'
        )
    for pj in pinned:
        meta_parts.append(f'<span class="tag pinned">📌 {html.escape(pj)}</span>')
    meta_html = "".join(meta_parts)

    doi_link = doi_url(p)
    doi_html = (f'<div class="doi"><a href="{html.escape(doi_link)}" target="_blank" rel="noopener">{html.escape(p.get("doi"))}</a></div>'
                if doi_link else "")

    # data attributes for filtering
    kw_values = [slug_for_keyword(k) for k in kws]
    if pinned:
        kw_values.append("__pinned__")
    if is_hi:
        kw_values.append("__hi__")
    data_kw = "|".join(html.escape(v, quote=True) for v in kw_values)
    search_text = " ".join([
        p.get("title") or "",
        strip_tags(p.get("abstractText") or "")[:300],
        p.get("authorString") or "",
        p.get("_journal") or journal_of(p),
        " ".join(kws),
        " ".join(pinned),
    ])

    cls = "paper hi" if is_hi else "paper"
    return f'''<article class="{cls}" data-keywords="{data_kw}" data-source="{html.escape(src, quote=True)}" data-search="{html.escape(search_text, quote=True)}">
  <h3><a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>{if_tag}</h3>
  {f'<div class="by">{authors}</div>' if authors else ''}
  <div class="meta">{meta_html}</div>
  {ksum_html}
  {doi_html}
</article>'''


# ---------------------------------------------------------------------------
# daily detail pages
# ---------------------------------------------------------------------------

def render_day_html(date: dt.date, papers: list[dict], all_dates: list[dt.date]) -> str:
    total = len(papers)
    by_src: dict[str, int] = defaultdict(int)
    by_journal: dict[str, int] = defaultdict(int)
    by_kw: dict[str, int] = defaultdict(int)
    by_pinned: dict[str, int] = defaultdict(int)
    pinned_count = 0
    hi_count = 0
    for p in papers:
        by_src[p.get("source") or "?"] += 1
        by_journal[p.get("_journal") or journal_of(p) or "(unspecified)"] += 1
        for k in p.get("matched_keywords", []) or []:
            by_kw[k] += 1
        if p.get("matched_journals"):
            pinned_count += 1
            for pj in p["matched_journals"]:
                by_pinned[pj] += 1
        if (p.get("_if") or 0) >= 10:
            hi_count += 1

    src_lis = "".join(
        f'<li><span>{html.escape(source_label(s))}</span><b>{n}</b></li>'
        for s, n in sorted(by_src.items(), key=lambda kv: -kv[1])
    ) or '<li><span style="color:var(--muted)">(없음)</span><b></b></li>'
    journal_top = sorted(by_journal.items(), key=lambda kv: -kv[1])[:8]
    journal_lis = "".join(
        f'<li><span>{html.escape(short_journal(j))}</span><b>{n}</b></li>'
        for j, n in journal_top
    ) or '<li><span style="color:var(--muted)">(없음)</span><b></b></li>'
    kw_lis = "".join(
        f'<li><span>{html.escape(k)}</span><b>{by_kw.get(k, 0)}</b></li>' for k in KEYWORDS
    )
    pinned_lis = "".join(
        f'<li><span>📌 {html.escape(j)}</span><b>{by_pinned.get(j, 0)}</b></li>'
        for j in PINNED_JOURNALS
    )

    # filter chips
    kw_chips = [f'<span class="chip active" data-filter="keyword" data-value="all">전체 <span class="count" id="visibleCount">{total}</span></span>']
    for k in KEYWORDS:
        slug = slug_for_keyword(k)
        kw_chips.append(
            f'<span class="chip kw-{slug}" data-filter="keyword" data-value="{slug}">{html.escape(k)}<span class="count">{by_kw.get(k, 0)}</span></span>'
        )
    if pinned_count:
        kw_chips.append(
            f'<span class="chip pinned" data-filter="keyword" data-value="__pinned__">📌 Pinned<span class="count">{pinned_count}</span></span>'
        )
    if hi_count:
        kw_chips.append(
            f'<span class="chip" data-filter="keyword" data-value="__hi__">⭐ IF≥10<span class="count">{hi_count}</span></span>'
        )

    src_chips = ['<span class="chip active" data-filter="source" data-value="all">전체</span>']
    for s, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        src_chips.append(
            f'<span class="chip" data-filter="source" data-value="{html.escape(s, quote=True)}">{html.escape(source_label(s))}<span class="count">{n}</span></span>'
        )

    # papers — sort high-IF first, then pinned, then title
    def sort_key(p: dict) -> tuple:
        ifv = p.get("_if") or 0
        return (-int(ifv >= 10) * 1000 - ifv, -int(bool(p.get("matched_journals"))),
                p.get("title") or "")

    papers_sorted = sorted(papers, key=sort_key)
    blocks = "".join(render_paper_card(p, base="../keywords/") for p in papers_sorted)

    # day navigation
    idx = all_dates.index(date) if date in all_dates else -1
    prev_d = all_dates[idx-1] if idx > 0 else None
    next_d = all_dates[idx+1] if 0 <= idx < len(all_dates)-1 else None
    prev_html = (f'<a class="arrow" href="{prev_d.isoformat()}.html">← {prev_d.isoformat()}</a>'
                 if prev_d else '<span class="arrow disabled">← 이전</span>')
    next_html = (f'<a class="arrow" href="{next_d.isoformat()}.html">{next_d.isoformat()} →</a>'
                 if next_d else '<span class="arrow disabled">다음 →</span>')
    center_parts = []
    for d in all_dates:
        cls = "today" if d == date else ""
        center_parts.append(
            f'<a class="{cls}" href="{d.isoformat()}.html">{d.month}/{d.day}</a>'
        )
    daynav = (f'<nav class="daynav">{prev_html}'
              f'<a class="home" href="../index.html">📚 인덱스</a>'
              f'<div class="center">{"".join(center_parts)}</div>'
              f'{next_html}</nav>')

    body_html = ""
    if total == 0:
        body_html = f'<div class="empty">{date.isoformat()}에는 새 논문이 없습니다.</div>'
    else:
        body_html = f'''
<div class="filter-row"><div class="lbl">Keyword</div><div class="filters">{"".join(kw_chips)}</div></div>
<div class="filter-row"><div class="lbl">Source</div><div class="filters">{"".join(src_chips)}</div></div>
<div class="search-box"><input id="search" type="search" placeholder="제목·저자·저널·초록·요약 검색…"></div>
<div class="papers">{blocks}</div>
<div id="empty" class="empty" style="display:none">조건에 맞는 논문이 없습니다.</div>'''

    pinned_block = (f'<div class="col"><h3>📌 Pinned</h3><ul>{pinned_lis}</ul></div>'
                    if PINNED_JOURNALS else '')

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Digest — {date.isoformat()}</title>
<style>{render_css()}</style>
</head>
<body>
<div class="wrap">
{daynav}
<header class="hero">
  <div>
    <h1>📄 Paper Digest</h1>
    <div class="meta">{date.strftime("%Y년 %m월 %d일 (%a)")} · Europe PMC · <b>{total}</b>편 · IF≥10 <b>{hi_count}</b>편</div>
  </div>
</header>
<section class="summary">
  <h2>Summary</h2>
  <div class="summary-grid">
    <div class="col"><h3>Source</h3><ul>{src_lis}</ul></div>
    <div class="col"><h3>Top journals</h3><ul>{journal_lis}</ul></div>
    <div class="col"><h3>Keyword</h3><ul>{kw_lis}</ul></div>
    {pinned_block}
  </div>
</section>
{body_html}
<footer>
  Generated {kst_now().strftime("%Y-%m-%d %H:%M")} KST · <a href="https://europepmc.org" target="_blank">Europe PMC</a> · IF: JCR 2024 (curated)
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
    var em = document.getElementById('empty'); if (em) em.style.display = visible ? 'none' : 'block';
    var vc = document.getElementById('visibleCount'); if (vc) vc.textContent = visible;
  }}
  document.querySelectorAll('.chip[data-filter]').forEach(function(chip) {{
    chip.addEventListener('click', function() {{
      var dim = chip.dataset.filter;
      state[dim] = chip.dataset.value;
      document.querySelectorAll('.chip[data-filter="' + dim + '"]').forEach(function(c) {{ c.classList.remove('active'); }});
      chip.classList.add('active'); apply();
    }});
  }});
  var search = document.getElementById('search');
  if (search) search.addEventListener('input', function(e) {{ state.q = e.target.value; apply(); }});
  apply();
  document.addEventListener('keydown', function(e) {{
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
    if (e.key === 'ArrowLeft') {{ var p = document.querySelector('.daynav .arrow:not(.disabled):first-of-type'); if (p && p.href) location.href = p.href; }}
    else if (e.key === 'ArrowRight') {{ var nodes = document.querySelectorAll('.daynav .arrow:not(.disabled)'); var n = nodes[nodes.length-1]; if (n && n.href) location.href = n.href; }}
  }});
}})();
</script>
</body></html>'''


# ---------------------------------------------------------------------------
# per-keyword aggregate pages
# ---------------------------------------------------------------------------

def render_keyword_page(keyword: str, papers: list[dict], all_keywords: list[str]) -> str:
    slug = slug_for_keyword(keyword)
    bg, fg = kw_palette(keyword)
    total = len(papers)
    hi_count = sum(1 for p in papers if (p.get("_if") or 0) >= 10)

    # Group by date (use _latest as the date for this paper on this listing)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for p in papers:
        by_date[p.get("_latest") or p.get("_first_seen") or "0000-00-00"].append(p)

    sections = []
    for d in sorted(by_date.keys(), reverse=True):
        plist = by_date[d]
        n = len(plist)
        # each card; use ../keywords/ since the page lives at keywords/<slug>.html and
        # internal kw chips link to other keyword pages in the same dir
        cards = "".join(render_paper_card(p, base="") for p in plist)
        try:
            dt_obj = dt.date.fromisoformat(d)
            label = dt_obj.strftime("%Y년 %m월 %d일 (%a)")
        except Exception:
            label = d
        sections.append(
            f'<div class="date-head"><h2>{label}</h2>'
            f'<span class="day-meta">{n}편 · <a href="../digests/{d}.html">하루 전체 보기 →</a></span></div>'
            f'<div class="papers">{cards}</div>'
        )

    # Other keyword chips (sidebar / top nav)
    kw_chips = []
    for k in all_keywords:
        s = slug_for_keyword(k)
        cls = "chip kw-" + s + (" active" if k == keyword else "")
        kw_chips.append(f'<a class="{cls}" href="{s}.html">{html.escape(k)}</a>')
    other_chips = "".join(kw_chips)

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(keyword)} — Research Digest</title>
<style>{render_css()}</style>
</head>
<body>
<div class="wrap">
<nav class="daynav">
  <a class="home" href="../index.html">📚 인덱스</a>
  <div class="center">{other_chips}</div>
</nav>
<header class="hero">
  <div>
    <h1><span class="tag kw-{slug}" style="font-size:18px;padding:4px 12px;border-radius:8px;font-weight:700">{html.escape(keyword)}</span> 키워드</h1>
    <div class="meta">총 <b>{total}</b>편 · IF≥10 <b>{hi_count}</b>편 · 최신순</div>
  </div>
</header>
<div class="search-box"><input id="search" type="search" placeholder="제목·저자·저널·초록·요약 검색…"></div>
{"".join(sections) if sections else '<div class="empty">아직 이 키워드에 매치된 논문이 없습니다.</div>'}
<footer>
  Generated {kst_now().strftime("%Y-%m-%d %H:%M")} KST · <a href="https://europepmc.org" target="_blank">Europe PMC</a> · IF: JCR 2024 (curated)
</footer>
</div>
<script>
(function() {{
  var search = document.getElementById('search');
  if (!search) return;
  search.addEventListener('input', function(e) {{
    var q = e.target.value.toLowerCase();
    document.querySelectorAll('.paper').forEach(function(el) {{
      var hay = (el.dataset.search || '').toLowerCase();
      el.classList.toggle('hidden', q && hay.indexOf(q) === -1);
    }});
  }});
}})();
</script>
</body></html>'''


# ---------------------------------------------------------------------------
# index page
# ---------------------------------------------------------------------------

def render_index_html(papers_by_uid: dict[str, dict], per_day_meta: dict[str, dict]) -> str:
    # Aggregate per-keyword counts across all papers
    per_kw_count: dict[str, int] = defaultdict(int)
    per_kw_hi: dict[str, int] = defaultdict(int)
    per_kw_recent: dict[str, str] = {}
    for uid, p in papers_by_uid.items():
        for k in p.get("matched_keywords", []) or []:
            per_kw_count[k] += 1
            if (p.get("_if") or 0) >= 10:
                per_kw_hi[k] += 1
            d = p.get("_latest") or ""
            if d > per_kw_recent.get(k, ""):
                per_kw_recent[k] = d

    kw_cards = []
    for k in KEYWORDS:
        slug = slug_for_keyword(k)
        bg, fg = kw_palette(k)
        n = per_kw_count.get(k, 0)
        hi = per_kw_hi.get(k, 0)
        recent = per_kw_recent.get(k, "")
        recent_disp = f"최근: {recent}" if recent else "기록 없음"
        hi_html = f' · ⭐ IF≥10 {hi}편' if hi else ''
        kw_cards.append(f'''<a class="kw-card" href="keywords/{slug}.html">
  <div class="name"><span class="swatch" style="background:{fg}"></span>{html.escape(k)}</div>
  <div class="stats"><b>{n}</b>편{hi_html}</div>
  <div class="recent">{recent_disp}</div>
</a>''')

    # Pinned-journal aggregated card
    pinned_total = sum(1 for p in papers_by_uid.values() if p.get("matched_journals"))
    if PINNED_JOURNALS:
        pbg, pfg = PINNED_LIGHT
        kw_cards.append(f'''<a class="kw-card" href="keywords/_pinned.html">
  <div class="name"><span class="swatch" style="background:{pfg}"></span>📌 Pinned 저널</div>
  <div class="stats"><b>{pinned_total}</b>편 · {", ".join(PINNED_JOURNALS)}</div>
  <div class="recent">Cell · Nature · Science 등</div>
</a>''')

    # Daily list (most recent first, all dates we have)
    rows = []
    for d in sorted(per_day_meta.keys(), reverse=True):
        m = per_day_meta[d]
        n = m.get("total", 0)
        hi_n = sum(1 for p in papers_by_uid.values()
                   if d in p.get("_dates", []) and (p.get("_if") or 0) >= 10)
        kws = m.get("by_keyword", {})
        srcs = m.get("by_source", {})
        kw_str = ", ".join(f"{k} {v}" for k, v in sorted(kws.items(), key=lambda kv: -kv[1])[:3] if v) or "—"
        src_str = ", ".join(f"{source_label(s)} {v}" for s, v in sorted(srcs.items(), key=lambda kv: -kv[1])[:3]) or "—"
        hi_html = f'<span class="hi-pill">⭐ IF≥10 · {hi_n}</span>' if hi_n else ""
        rows.append(f'''<a class="row" href="digests/{d}.html">
  <div class="d">{d}</div>
  <div class="n">{n}편 {hi_html}</div>
  <div class="info"><span>{html.escape(src_str)}</span><span>{html.escape(kw_str)}</span></div>
</a>''')

    rows_html = "\n".join(rows) if rows else '<div class="empty">아직 디지스트가 없습니다.</div>'

    total_papers = len(papers_by_uid)
    total_days = len(per_day_meta)
    total_hi = sum(1 for p in papers_by_uid.values() if (p.get("_if") or 0) >= 10)

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research Digest</title>
<style>{render_css()}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div>
    <h1>📚 Research Digest</h1>
    <div class="meta">매일 아침 8:00 KST (월–금) · Europe PMC · 누적 <b>{total_papers}</b>편 / <b>{total_days}</b>일 · IF≥10 <b>{total_hi}</b>편</div>
  </div>
  <a class="home-link" href="https://github.com/GenomicDiversityLab/research-digest" target="_blank">GitHub ↗</a>
</header>

<div class="section-h">키워드별 보기</div>
<div class="kw-grid">
{"".join(kw_cards)}
</div>

<div class="section-h">날짜별</div>
<div class="daylist">
{rows_html}
</div>

<footer>
  Generated {kst_now().strftime("%Y-%m-%d %H:%M")} KST · <a href="https://europepmc.org" target="_blank">Europe PMC</a> ·
  IF: JCR 2024 (curated) ·
  <a href="https://github.com/GenomicDiversityLab/research-digest" target="_blank">repo</a>
</footer>
</div>
</body></html>'''


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def render_pinned_aggregate(papers_by_uid: dict[str, dict]) -> str:
    """A pseudo-keyword page for pinned-journal hits."""
    plist = [p for p in papers_by_uid.values() if p.get("matched_journals")]
    plist.sort(key=lambda p: p.get("_latest", ""), reverse=True)

    # use a synthetic 'keyword' so the rendering reuses the keyword page layout
    fake_kw = "📌 Pinned"
    by_date: dict[str, list[dict]] = defaultdict(list)
    for p in plist:
        by_date[p.get("_latest", "")].append(p)

    sections = []
    for d in sorted(by_date.keys(), reverse=True):
        cards = "".join(render_paper_card(p, base="") for p in by_date[d])
        try:
            label = dt.date.fromisoformat(d).strftime("%Y년 %m월 %d일 (%a)")
        except Exception:
            label = d
        sections.append(
            f'<div class="date-head"><h2>{label}</h2>'
            f'<span class="day-meta">{len(by_date[d])}편 · <a href="../digests/{d}.html">하루 전체 보기 →</a></span></div>'
            f'<div class="papers">{cards}</div>'
        )

    other_chips = "".join(
        f'<a class="chip kw-{slug_for_keyword(k)}" href="{slug_for_keyword(k)}.html">{html.escape(k)}</a>'
        for k in KEYWORDS
    )

    pbg, pfg = PINNED_LIGHT

    total = len(plist)
    hi_count = sum(1 for p in plist if (p.get("_if") or 0) >= 10)

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📌 Pinned 저널 — Research Digest</title>
<style>{render_css()}</style>
</head>
<body>
<div class="wrap">
<nav class="daynav">
  <a class="home" href="../index.html">📚 인덱스</a>
  <div class="center">{other_chips}<a class="chip pinned active" href="_pinned.html">📌 Pinned</a></div>
</nav>
<header class="hero">
  <div>
    <h1><span class="tag pinned" style="font-size:18px;padding:4px 12px;border-radius:8px;font-weight:700">📌 Pinned 저널</span></h1>
    <div class="meta">{", ".join(PINNED_JOURNALS)} · 총 <b>{total}</b>편 · IF≥10 <b>{hi_count}</b>편 · 최신순</div>
  </div>
</header>
<div class="search-box"><input id="search" type="search" placeholder="제목·저자·저널·초록·요약 검색…"></div>
{"".join(sections) if sections else '<div class="empty">아직 매치된 논문이 없습니다.</div>'}
<footer>Generated {kst_now().strftime("%Y-%m-%d %H:%M")} KST</footer>
</div>
<script>
(function() {{
  var s = document.getElementById('search');
  if (!s) return;
  s.addEventListener('input', function(e) {{
    var q = e.target.value.toLowerCase();
    document.querySelectorAll('.paper').forEach(function(el) {{
      var hay = (el.dataset.search || '').toLowerCase();
      el.classList.toggle('hidden', q && hay.indexOf(q) === -1);
    }});
  }});
}})();
</script>
</body></html>'''


def main() -> None:
    DIGESTS.mkdir(exist_ok=True)
    KEYWORDS_DIR.mkdir(exist_ok=True)

    papers_by_uid = load_all_papers()
    per_day_meta = load_per_day_meta()
    print(f"loaded {len(papers_by_uid)} unique papers across {len(per_day_meta)} days")

    # Date set: any day we processed (even 0-paper days) gets a daily HTML
    all_dates_set: set[str] = set(per_day_meta.keys())
    for p in papers_by_uid.values():
        all_dates_set.update(p.get("_dates", []))
    all_dates = sorted(dt.date.fromisoformat(d) for d in all_dates_set if re.match(r"^\d{4}-\d{2}-\d{2}$", d))

    # --- daily detail pages ---
    by_date_papers: dict[str, list[dict]] = defaultdict(list)
    for p in papers_by_uid.values():
        for d in p.get("_dates", []):
            by_date_papers[d].append(p)
    for d in all_dates:
        plist = by_date_papers.get(d.isoformat(), [])
        (DIGESTS / f"{d.isoformat()}.html").write_text(
            render_day_html(d, plist, all_dates), encoding="utf-8"
        )
    print(f"wrote {len(all_dates)} daily HTML(s) to digests/")

    # --- per-keyword pages ---
    by_kw: dict[str, list[dict]] = defaultdict(list)
    for p in papers_by_uid.values():
        for k in p.get("matched_keywords", []) or []:
            by_kw[k].append(p)
    for k in KEYWORDS:
        plist = by_kw.get(k, [])
        plist.sort(key=lambda p: p.get("_latest", ""), reverse=True)
        slug = slug_for_keyword(k)
        (KEYWORDS_DIR / f"{slug}.html").write_text(
            render_keyword_page(k, plist, KEYWORDS), encoding="utf-8"
        )
    print(f"wrote {len(KEYWORDS)} keyword HTML(s) to keywords/")

    # --- pinned aggregate page ---
    if PINNED_JOURNALS:
        (KEYWORDS_DIR / "_pinned.html").write_text(
            render_pinned_aggregate(papers_by_uid), encoding="utf-8"
        )
        print("wrote keywords/_pinned.html")

    # --- index ---
    (ROOT / "index.html").write_text(
        render_index_html(papers_by_uid, per_day_meta), encoding="utf-8"
    )
    print("wrote index.html")


if __name__ == "__main__":
    main()
