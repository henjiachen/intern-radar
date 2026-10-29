"""UCSD first-year profile and durable, retryable GitHub Issue delivery.

The legacy watch/triage tools remain available; this hourly entry point owns
only data/ucsd.json, never the previous owner's application history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from intern_radar.config import load_config
from intern_radar.models import Posting, canon_key
from intern_radar.notify import format_lines, md_escape
from intern_radar.poll import PollJob, run_jobs
from intern_radar.sources import (
    fetch_ashby, fetch_greenhouse, fetch_lever, fetch_simplify,
    fetch_smartrecruiters, fetch_workday,
)
from intern_radar.sources.lists import fetch_speedyapply, fetch_vansh
from intern_radar.sources.nintendo import fetch_nintendo

PRIORITY = ("xbox", "playstation", "sony interactive", "usinternshipspubliccareerssite",
            "nintendo", "nvidia", "google", "microsoft", "salesforce")


def priority(p: Posting) -> tuple[int, str, str]:
    text = (p.company + " " + p.title + " " + p.url).casefold()
    rank = next((i + 2 for i, name in enumerate(PRIORITY) if name in text), len(PRIORITY) + 2)
    if program(p):
        rank = 0 if "google" in p.company.casefold() else 1
    return (rank,
            p.company.casefold(), p.title.casefold())

TECH = re.compile(
    r"\b(software|swe|computer|computing|developer|programming|machine learning|"
    r"artificial intelligence|ai|ml|data science|data engineer|robotics|firmware|"
    r"platform engineer|systems engineer|security engineer|gameplay|graphics|game engine)\b", re.I
)
INTERN = re.compile(r"\b(intern(?:ship)?s?|co[ -]?op)\b", re.I)
OFFCYCLE = re.compile(r"\b(fall|autumn|winter|spring)\b", re.I)
US = re.compile(
    r"\b(united states(?: of america)?|usa|u\.s\.(?:a\.)?|us)\b|"
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b|"
    r"\b(California|New York|Washington|Texas|Massachusetts|Virginia|Illinois|"
    r"San Diego|San Francisco|San Jose|Mountain View|Palo Alto|Sunnyvale|Menlo Park|"
    r"Seattle|Redmond|Bellevue|Boston|New York City|Austin|Chicago|Los Angeles)\b", re.I
)
FOREIGN = re.compile(
    r"\b(Canada|Toronto|Vancouver|Montreal|Ontario|India|Bangalore|Bengaluru|"
    r"China|Beijing|Shanghai|Singapore|London|United Kingdom|UK|Germany|France|"
    r"Australia|Sydney|Japan|Tokyo|Brazil|Mexico|Dublin|Ireland|Netherlands|Poland)\b", re.I
)


def program(p: Posting) -> bool:
    company, title = p.company.lower(), p.title.lower()
    return (
        ("google" in company and bool(re.search(
            r"\b(step|asdi)\b|student training in engineering|"
            r"associate software developer intern", title)))
        or ("microsoft" in company and "explore" in title)
    )


def graduation_badge(p: Posting) -> str:
    """Report available evidence, never infer that an unstated rule does not exist."""
    for line in (p.title + "\n" + p.description).splitlines():
        match = re.search(r"\b(?:graduat\w*|class of)[^.;\n]{0,120}", line, re.I)
        if match and re.search(r"\b20\d{2}\b", match.group()):
            return "📅 Grad year stated: " + md_escape(match.group().strip()[:160])
        match = re.search(r"\b20\d{2}[^.;\n]{0,70}\bgraduat\w*", line, re.I)
        if match:
            return "📅 Grad year stated: " + md_escape(match.group().strip()[:160])
    if p.description.strip():
        return "➖ No grad year found in the available description"
    return "❔ Grad year unknown — full description unavailable"


def candidate_line(p: Posting) -> str:
    line = format_lines([p])[0]
    star = "⭐ " if program(p) else ""
    return line.replace("- [ ] ", "- [ ] " + star, 1) + " — " + graduation_badge(p)


def eligible(p: Posting) -> bool:
    """Potentially eligible from listing metadata; missing grad year is allowed.

    This is a discovery filter, not a claim of employer-confirmed eligibility.
    Descriptions/work authorization are not supplied by all feeds.
    """
    title = p.title
    text = title + "\n" + p.description
    if urlsplit(p.url).scheme not in {"https", "http"}:
        return False
    if not any(US.search(loc) and not FOREIGN.search(loc) for loc in p.locations):
        return False  # generic Remote / unknown country is not verified US
    if not program(p) and not (INTERN.search(title + " " + p.employment_type)
                               and TECH.search(title + " " + p.category)):
        return False
    if re.search(r"\b(senior|staff|principal|ph\.?d|doctoral|mba|unpaid)\b", title, re.I):
        return False
    if p.degrees and not any(re.search(r"bachelor|undergrad|\bbs\b|\bba\b", d, re.I)
                             for d in p.degrees):
        return False
    if re.search(r"\b(juniors?|seniors?)\s+only\b|\b(3rd|4th|third|fourth)[ -]year", title, re.I):
        return False
    if re.search(r"\b(?:rising|current(?:ly)?(?: a)?|must be(?: a)?)\s+(?:college\s+)?"
                 r"(?:juniors?|seniors?)\b|\b(?:juniors?|seniors?)\s+only\b|"
                 r"\b(?:third|fourth|3rd|4th)[ -]year\s+(?:student|undergrad)",
                 p.description, re.I):
        return False
    # Graduation years in a title are not the internship cycle. Keep 2029/30;
    # reject explicit 2028-only graduation requirements, never infer from silence.
    grad = re.search(r"(?:graduat\w*|class of)[^.;\n]{0,100}", text, re.I)
    cycle_title = title
    if grad:
        years = {int(y) for y in re.findall(r"\b20\d{2}\b", grad.group())}
        # Ranges such as 2026–2029 overlap 2029. No exact year required.
        if len(years) >= 2:
            years = set(range(min(years), max(years) + 1))
        if years and not years & {2029, 2030}:
            return False
        if grad.start() < len(title):
            cycle_title = title[:grad.start()]
    if p.terms:
        # A community feed can mislabel the season. A specific cycle in the
        # actual job link is stronger evidence than a generic list label.
        link = unquote(urlsplit(p.url).path).replace("_", " ").replace("-", " ")
        wrong_cycle = re.search(r"\b(?:spring|fall|autumn|winter)\s+2027\b", link, re.I)
        summer = re.search(r"summer\s+(?:of\s+)?2027", title + " " + link, re.I)
        if wrong_cycle and not summer:
            return False
        return any(term.casefold() == "summer 2027" for term in p.terms)
    # A dated 2027 internship with no other season is a summer candidate.
    # Undated ATS jobs remain excluded rather than silently becoming Summer 2027.
    if OFFCYCLE.search(cycle_title):
        return False
    if re.search(r"summer\s+(?:of\s+)?2027", p.description, re.I):
        return True
    years = set(re.findall(r"\b20\d{2}\b", cycle_title))
    return "2027" in years and not (years - {"2027"})


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def expand_short_link(url: str) -> str:
    """Read only the shortener's redirect, without downloading the job page."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "intern-radar"})
    try:
        with opener.open(request, timeout=8) as response:
            target = response.geturl()
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308} or not error.headers.get("Location"):
            raise
        target = urljoin(url, error.headers["Location"])
        error.close()
    if (urlsplit(target).scheme not in {"http", "https"}
            or urlsplit(target).hostname == "go.vansh.nyc"):
        raise ValueError("Short link did not provide a direct job URL")
    return target


def resolve_short_links(postings: list[Posting], state: dict[str, Any], *,
                        budget: int = 100) -> list[Posting]:
    from concurrent.futures import ThreadPoolExecutor
    cache = state.setdefault("links", {})
    attempts = state.setdefault("link_checks", {})
    sequence = state.get("scan_sequence", 0) + 1
    state["scan_sequence"] = sequence
    candidates = {p.url: p for p in postings
                  if urlsplit(p.url).hostname == "go.vansh.nyc" and eligible(p)}
    # Baseline the short-link backlog once, using the original case-sensitive
    # URLs. Legacy normalized keys folded case and could conflate short IDs.
    baseline_urls = set(state.setdefault("short_baseline", list(candidates)))
    # Resolve new openings first, then rotate any failed or deferred old URLs.
    missing = sorted((url for url in candidates if url not in cache),
                     key=lambda url: (canon_key(url) in state.get("known", {}),
                                      attempts.get(url, 0), priority(candidates[url])))[:budget]
    def resolve(url):
        try:
            return url, expand_short_link(url)
        except (OSError, ValueError):
            return url, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for url, target in pool.map(resolve, missing):
            attempts[url] = sequence
            if target:
                cache[url] = target
    out = []
    deferred = 0
    known = state.setdefault("known", {})
    migrated = state.setdefault("_link_migrations", [])
    for p in postings:
        if p.url in candidates:
            target = cache.get(p.url)
            if not target:
                deferred += 1
                continue  # never alert an unresolved duplicate/unknown-cycle URL
            old_key, new_key = canon_key(p.url), canon_key(target)
            if p.url in baseline_urls:
                migrated.append(new_key)
                baseline_urls.remove(p.url)
            if old_key in known:
                old = known[old_key].get("evidence", {})
                if old.get("company") == " ".join(p.company.casefold().split()):
                    known.setdefault(new_key, known.pop(old_key))
            p = replace(p, url=target)
        out.append(p)
    print(f"Short links: resolved {len(candidates) - deferred}; deferred {deferred}")
    state["short_baseline"] = sorted(baseline_urls)
    return out


def snapshot(p: Posting) -> dict[str, Any]:
    # Source, tracking URL, order, and posting-date refreshes are not changes.
    clean = lambda s: " ".join(s.casefold().split())
    return {
        "company": clean(p.company), "title": clean(p.title),
        "locations": sorted({clean(x) for x in p.locations}),
        "terms": sorted({clean(x) for x in p.terms}),
        "degrees": sorted({clean(x) for x in p.degrees}),
        "category": clean(p.category),
        "requirements": sorted({clean(line) for line in p.description.splitlines()
                                if re.search(r"graduat|class of|freshm|sophom|junior|senior|"
                                             r"summer|sponsor|authoriz", line, re.I)}),
    }


def reconcile(state: dict[str, Any], postings: list[Posting], *,
              baseline: bool = False) -> dict[str, Any]:
    # Prefer richer metadata deterministically across overlapping sources.
    groups: dict[str, list[Posting]] = {}
    for p in postings:
        groups.setdefault(canon_key(p.url), []).append(p)
    events = []
    migrations = set(state.pop("_link_migrations", []))
    known = state.setdefault("known", {})
    for key, rows in sorted(groups.items()):
        previous = known.get(key)
        p = max(rows, key=lambda p: (bool(p.description), bool(p.terms), len(p.degrees),
                                    len(p.locations), p.source, p.key))
        if not program(p) and not (INTERN.search(p.title + " " + p.employment_type)
                                   and TECH.search(p.title + " " + p.category)):
            if previous is None or not previous["eligible"]:
                known.pop(key, None)
                continue  # do not retain thousands of unrelated full-time jobs
        p = replace(p,
                    terms=tuple(sorted({x for row in rows for x in row.terms})),
                    degrees=tuple(sorted({x for row in rows for x in row.degrees})),
                    locations=tuple(sorted({x for row in rows for x in row.locations})),
                    category=next((row.category for row in rows if row.category), ""))
        # An unavailable/richer feed is not evidence that requirements changed.
        # Retain prior metadata only when the current sources omit that field.
        evidence = previous.get("evidence", {}) if previous else {}
        p = replace(p, terms=p.terms or tuple(evidence.get("terms", [])),
                    degrees=p.degrees or tuple(evidence.get("degrees", [])),
                    category=p.category or evidence.get("category", ""),
                    description=p.description or "\n".join(evidence.get("requirements", [])))
        good, fields = eligible(p), snapshot(p)
        fingerprint = digest(fields)
        changed = (previous is None or previous["eligible"] != good
                   or previous["fingerprint"] != fingerprint)
        revision = (previous.get("revision", 0) if previous else 0) + int(changed)
        if good and (previous is None or not previous["eligible"]
                     or previous["fingerprint"] != fingerprint):
            if not baseline and key not in migrations:
                event = {"posting": asdict(p), "kind": "new" if previous is None
                         else "changed", "id": digest([key, fingerprint, revision])}
                events.append(event)
        known[key] = {"eligible": good, "fingerprint": fingerprint, "revision": revision}
        if good:
            known[key]["evidence"] = fields
    # Missing rows never mean closure: partial feeds and outages are common.
    events.sort(key=lambda event: priority(Posting(**event["posting"])))
    for start in range(0, len(events), 40):
        chunk = events[start:start + 40]
        batch = {"id": digest([e["id"] for e in chunk]), "events": chunk}
        state.setdefault("pending", []).append(batch)
    state["version"] = 1
    return state


def save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


class GitHub:
    def __init__(self) -> None:
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GITHUB_TOKEN"]

    def request(self, route: str, payload: Any = None) -> Any:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{route}".rstrip("/"),
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json", "User-Agent": "intern-radar",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def markers(self) -> set[str]:
        found: set[str] = set()
        for page in range(1, 101):
            rows = self.request(f"issues?state=all&per_page=100&page={page}")
            for row in rows:
                ids = re.findall(r"<!-- radar-ucsd:([a-f0-9]{64}) -->", row.get("body") or "")
                found.update(ids)
                if ids:
                    self.last_alert_hour = max(getattr(self, "last_alert_hour", ""),
                                               row.get("created_at", "")[:13])
            if len(rows) < 100:
                return found
        raise RuntimeError("Issue history exceeds reconciliation limit; delivery stopped safely")

    def send(self, batch: dict[str, Any]) -> None:
        postings = [Posting(**e["posting"]) for e in batch["events"]]
        header = ("**Intern Radar** — Summer 2027 US internships for a UCSD freshman in CS.\n\n"
                  "⭐ Priority program: Google STEP/ASDI or Microsoft Explore.\n"
                  "📅 Graduation year explicitly stated · ➖ Not found in available description · "
                  "❔ Full description unavailable. Missing information does not prove eligibility.\n\n")
        lines = []
        size = len(header)
        for p in sorted(postings, key=priority):
            line = candidate_line(p)
            if size + len(line) > 55000:
                break
            lines.append(line)
            size += len(line) + 1
        branch = quote(os.environ.get("GITHUB_REF_NAME", "main"), safe="")
        archive = f"https://github.com/{self.repo}/blob/{branch}/data/alerts/{batch['id']}.json"
        suffix = (f"\n\nShowing {len(lines)} of {len(postings)} matches. "
                  f"[Full saved list]({archive})." if len(lines) < len(postings) else "")
        body = header + "\n".join(lines) + suffix + f"\n\n<!-- radar-ucsd:{batch['id']} -->"
        self.request("issues", {"title": f"Intern Radar: {len(postings)} new or changed candidates",
                                 "body": body, "assignees": [self.repo.split('/')[0]]})


def deliver(path: Path, state: dict[str, Any], github: Any, *, now: datetime | None = None) -> None:
    if not state.get("pending"):
        return
    markers = github.markers()
    remaining = [batch for batch in state["pending"] if batch["id"] not in markers]
    if not remaining:
        state["pending"] = []
        save(path, state)
        return
    hour = (now or datetime.now(UTC)).isoformat()[:13]
    last_hour = max(state.get("last_alert_hour", ""), getattr(github, "last_alert_hour", ""))
    if last_hour >= hour:
        state["pending"] = remaining
        save(path, state)
        print("Already sent this hour; new matches will be included in the next hourly alert")
        return
    # One issue per check, including any pending retries. Remove already-sent
    # envelopes before combining, so a lost receipt never duplicates an alert.
    batch = remaining[0] if len(remaining) == 1 else {
        "id": digest([b["id"] for b in remaining]),
        "events": [e for b in remaining for e in b["events"]],
    }
    state["pending"] = [batch]
    save(path, state)
    archive = path.parent / "alerts" / f"{batch['id']}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(batch, indent=1) + "\n", encoding="utf-8")
    github.send(batch)
    state["last_alert_hour"] = hour
    state["pending"] = []
    save(path, state)  # remote marker covers a crash before this receipt save


def fetch(config_path: Path) -> tuple[list[Posting], list[str], int]:
    config = load_config(config_path)
    s = config.sources
    jobs = []
    def add(name: str, host: str, fn: Any) -> None:
        jobs.append(PollJob(name, name.split(':')[0], host, fn))
    for enabled, name, fn in [(s.simplify, "simplify", fetch_simplify),
                              (s.vanshb03, "vanshb03", fetch_vansh),
                              (s.speedyapply, "speedyapply", fetch_speedyapply)]:
        if enabled:
            add(name, "raw.githubusercontent.com", fn)
    add("nintendo", "careers.nintendo.com", fetch_nintendo)
    from functools import partial
    for family, host, names, fn in [
        ("greenhouse", "boards-api.greenhouse.io", s.greenhouse_boards,
         partial(fetch_greenhouse, content=True)),
        ("lever", "api.lever.co", s.lever_companies, fetch_lever),
        ("ashby", "api.ashbyhq.com", s.ashby_orgs, fetch_ashby),
        ("workday", "myworkdayjobs.com", s.workday_boards, fetch_workday),
        ("smartrecruiters", "api.smartrecruiters.com", s.smartrecruiters_companies,
         lambda name: fetch_smartrecruiters(name, s.smartrecruiters_country)),
    ]:
        for name in names:
            add(f"{family}:{name}", host, partial(fn, name))
    results = run_jobs(jobs)
    failures = [r.job.name + ": " + r.error for r in results if not r.ok]
    return [p for r in results for p in r.postings], failures, len(results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["scan", "deliver"], default="scan")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state", type=Path, default=Path("data/ucsd.json"))
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    if args.phase == "deliver":
        if not args.dry_run:
            github = GitHub()
            if not github.request("").get("has_issues"):
                raise RuntimeError("Enable Issues in repository Settings > Features; alerts are blocked")
            deliver(args.state, state, github)
        return 0
    postings, failures, total = fetch(args.config)
    postings = resolve_short_links(postings, state)
    print(json.dumps({"sources": total, "failures": failures,
                      "fetched": len(postings), "candidates": sum(map(eligible, postings)),
                      "priority_candidates": [p.company + ": " + p.title
                                              for p in sorted(postings, key=priority)
                                              if eligible(p) and priority(p)[0] < len(PRIORITY) + 2]}))
    if total == 0 or len(failures) == total:
        return 1
    # A new user's first scan is quiet, never seeded from legacy history.
    # Do not silently baseline unseen openings from a broken source.
    baseline = args.baseline or not args.state.exists()
    if baseline and failures:
        return 1
    reconcile(state, postings, baseline=baseline)
    print("Quiet initial baseline" if baseline else "Compared with saved profile history")
    if not args.dry_run:
        save(args.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
