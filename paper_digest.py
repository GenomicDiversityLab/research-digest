#!/usr/bin/env python3
"""
Daily Europe PMC paper digest — fetcher.

Fetches new EPMC entries indexed on the target date(s), dedupes them, and
saves the raw paper records + per-day metadata to ./data/. Site rendering
is handled by a separate `build_site.py` (run after this).

Usage:
    python3 paper_digest.py                 # default: yesterday in KST
                                              (Mon: backfill last Fri+Sat+Sun)
    python3 paper_digest.py 2026-05-04      # explicit date(s)
    python3 paper_digest.py --today         # today in KST
    python3 paper_digest.py 2026-05-04 2026-05-05 ...

Exit codes:
    0  ran fine (digest produced, possibly with 0 papers)
    2  one or more keyword fetches failed AND zero results were collected
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"

EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
USER_AGENT = "research-digest/1.0 (mailto:yoojinha@hanyang.ac.kr)"


# ---------------------------------------------------------------------------
# config & dates
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def kst_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def resolve_target_dates(argv: list[str]) -> list[dt.date]:
    """One or more target dates from CLI args.

    No args: Mon → [last Fri, Sat, Sun]; Tue–Fri → [yesterday]; Sat/Sun → [yesterday].
    --today: today.
    YYYY-MM-DD [YYYY-MM-DD ...]: explicit list.
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
                    raise SystemExit(f"Invalid date arg: {arg!r}")
        return out
    today = kst_now().date()
    yesterday = today - dt.timedelta(days=1)
    if today.weekday() == 0:  # Monday → 3-day backfill
        return [today - dt.timedelta(days=n) for n in (3, 2, 1)]
    return [yesterday]


# ---------------------------------------------------------------------------
# Europe PMC fetch
# ---------------------------------------------------------------------------

def _epmc_get(query: str, page_size: int) -> list[dict]:
    params = {"query": query, "format": "json", "pageSize": str(page_size), "resultType": "core"}
    url = f"{EPMC_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload.get("resultList", {}).get("result", []) or []


def _date_range(d: dt.date) -> str:
    return f"CREATION_DATE:[{d.isoformat()} TO {d.isoformat()}]"


def collect_for(target_date: dt.date, config: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """Returns (papers, failed). Papers are deduped by (source, id) and have
    `matched_keywords` and `matched_journals` lists attached."""
    keywords = config["keywords"]
    pinned_journals = config.get("always_include_journals", []) or []
    overrides = config.get("keyword_overrides", {}) or {}
    page_size = int(config.get("page_size", 50))

    seen: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    failed: list[tuple[str, str]] = []
    df = _date_range(target_date)

    for kw in keywords:
        if kw in overrides:
            q = f"{overrides[kw]} AND {df}"
        else:
            kq = kw.replace('"', '')
            q = f'(TITLE:"{kq}" OR ABSTRACT:"{kq}") AND {df}'
        try:
            res = _epmc_get(q, page_size)
        except Exception as e:
            failed.append((f"kw:{kw}", f"{type(e).__name__}: {e}"))
            continue
        for r in res:
            key = (r.get("source") or "?", r.get("id") or "?")
            if key in seen:
                if kw not in seen[key]["matched_keywords"]:
                    seen[key]["matched_keywords"].append(kw)
            else:
                r = dict(r)
                r["matched_keywords"] = [kw]
                r["matched_journals"] = []
                seen[key] = r

    for j in pinned_journals:
        try:
            res = _epmc_get(f'JOURNAL:"{j}" AND {df}', page_size)
        except Exception as e:
            failed.append((f"journal:{j}", f"{type(e).__name__}: {e}"))
            continue
        for r in res:
            jt = (r.get("journalTitle") or "").strip().lower()
            ji = r.get("journalInfo") or {}
            jt_alt = ""
            if isinstance(ji, dict):
                jr = ji.get("journal") or {}
                if isinstance(jr, dict):
                    jt_alt = (jr.get("title") or "").strip().lower()
            if j.lower() not in {jt, jt_alt}:
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

    return list(seen.values()), failed


# ---------------------------------------------------------------------------
# helpers used by Slack summary
# ---------------------------------------------------------------------------

def journal_of(p: dict) -> str:
    j = (p.get("journalTitle") or "").strip()
    if j:
        return j
    ji = p.get("journalInfo") or {}
    if isinstance(ji, dict):
        jr = ji.get("journal") or {}
        if isinstance(jr, dict):
            return (jr.get("title") or "").strip()
    return ""


def source_label(s: str) -> str:
    return {"MED": "PubMed", "PMC": "PMC", "PPR": "Preprint"}.get(s, s)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _run_build_site() -> None:
    """Try to invoke the site renderer. Tolerates absence (e.g. on a stripped install)."""
    bs = ROOT / "build_site.py"
    if not bs.exists():
        print("(build_site.py missing — skipping site rebuild)")
        return
    try:
        subprocess.run([sys.executable, str(bs)], check=True, cwd=ROOT)
    except subprocess.CalledProcessError as e:
        print(f"(build_site.py exit {e.returncode} — site may be stale)")


def main(argv: list[str]) -> int:
    config = load_config()
    keywords = config["keywords"]
    pinned_journals = config.get("always_include_journals", []) or []
    DATA_DIR.mkdir(exist_ok=True)

    target_dates = resolve_target_dates(argv)
    results: list[tuple[dt.date, list[dict], list[tuple[str, str]]]] = []
    for d in target_dates:
        papers, failed = collect_for(d, config)
        # save raw records (full paper JSON, used by build_site.py for keyword pages)
        (DATA_DIR / f"{d.isoformat()}_papers.json").write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # per-day meta (counts only — used by index renderer)
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
            "date": d.isoformat(),
            "total": len(papers),
            "by_source": dict(by_src),
            "by_keyword": {k: by_kw.get(k, 0) for k in keywords},
            "by_pinned_journal": {j: by_pinned.get(j, 0) for j in pinned_journals},
            "pinned_paper_count": pinned_count,
            "failed": [k for k, _ in failed],
            "generated_at": kst_now().isoformat(),
        }
        (DATA_DIR / f"{d.isoformat()}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results.append((d, papers, failed))
        print(f"[{d.isoformat()}] {len(papers)} papers"
              + (f" · {len(failed)} failed fetches" if failed else ""))

    # delegate site rendering to build_site.py (reads all data/*_papers.json)
    print("---\nRendering site...")
    _run_build_site()

    # Slack-friendly summary line(s)
    print("---")
    if len(results) == 1:
        d, papers, failed = results[0]
        print(_format_one_day_slack(d, papers, failed, keywords, pinned_journals))
    else:
        all_papers: list[dict] = []
        for _, ps, _ in results:
            all_papers.extend(ps)
        first, last = results[0][0], results[-1][0]
        print(f"*Paper Digest — {first.isoformat()} ~ {last.isoformat()}* · "
              f"*{len(all_papers)}* new across {len(results)} days")
        for d, papers, _ in results:
            print(f"• `{d.isoformat()}` · {len(papers)}편 → "
                  f"https://genomicdiversitylab.github.io/research-digest/digests/{d.isoformat()}.html")

    any_success = any(len(papers) > 0 or not failed for _, papers, failed in results)
    return 0 if any_success else 2


def _format_one_day_slack(d: dt.date, papers: list[dict], failed: list[tuple[str, str]],
                          keywords: list[str], pinned_journals: list[str]) -> str:
    by_src: dict[str, int] = defaultdict(int)
    by_journal: dict[str, int] = defaultdict(int)
    by_kw: dict[str, int] = defaultdict(int)
    by_pinned: dict[str, int] = defaultdict(int)
    pinned_count = 0
    for p in papers:
        by_src[p.get("source") or "?"] += 1
        by_journal[journal_of(p) or "(unspecified)"] += 1
        for k in p.get("matched_keywords", []):
            by_kw[k] += 1
        if p.get("matched_journals"):
            pinned_count += 1
            for pj in p["matched_journals"]:
                by_pinned[pj] += 1

    total = len(papers)
    if total == 0:
        body = [f"*Paper Digest — {d.isoformat()}* · *0* new",
                "_Europe PMC (PubMed + preprints)_",
                "",
                "No new entries matched the watched keywords."]
    else:
        src_str = ", ".join(f"{source_label(s)} {c}" for s, c in sorted(by_src.items(), key=lambda kv: -kv[1]))
        top_journals = sorted(by_journal.items(), key=lambda kv: -kv[1])[:5]
        journals_str = ", ".join(f"{j} ({c})" for j, c in top_journals) or "—"
        kw_str = ", ".join(f"{k} {by_kw.get(k, 0)}" for k in keywords)
        body = [
            f"*Paper Digest — {d.isoformat()}* · *{total}* new",
            f"• *Source:* {src_str or '—'}",
            f"• *Top journals:* {journals_str}",
            f"• *Keyword hits:* {kw_str}",
        ]
        if pinned_journals:
            pinned_str = ", ".join(f"{j} {by_pinned.get(j, 0)}" for j in pinned_journals)
            body.append(f"• 📌 *Pinned journals:* {pinned_str}  _({pinned_count} unique)_")
    body.append("")
    body.append(f"📄 https://genomicdiversitylab.github.io/research-digest/digests/{d.isoformat()}.html")
    if failed:
        body.append(f"⚠️ Failed fetches: {', '.join(k for k, _ in failed)}")
    return "\n".join(body)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
