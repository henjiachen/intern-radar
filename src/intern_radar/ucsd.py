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
import urllib.request
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from intern_radar.config import load_config
from intern_radar.models import Posting, canon_key
from intern_radar.notify import format_lines
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
    return (next((i for i, name in enumerate(PRIORITY) if name in text), len(PRIORITY)),
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
    known = state.setdefault("known", {})
    for key, rows in sorted(groups.items()):
        previous = known.get(key)
        p = max(rows, key=lambda p: (bool(p.description), bool(p.terms), len(p.degrees),
                                    len(p.locations), p.source, p.key))
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
        if good and (previous is None or not previous["eligible"]
                     or previous["fingerprint"] != fingerprint):
            if not baseline:
                event = {"posting": asdict(p), "kind": "new" if previous is None
                         else "changed", "id": digest([key, fingerprint])}
                events.append(event)
        known[key] = {"eligible": good, "fingerprint": fingerprint, "evidence": fields}
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
                found.update(re.findall(r"<!-- radar-ucsd:([a-f0-9]{64}) -->",
                                        row.get("body") or ""))
            if len(rows) < 100:
                return found
        raise RuntimeError("Issue history exceeds reconciliation limit; delivery stopped safely")

    def send(self, batch: dict[str, Any]) -> None:
        postings = [Posting(**e["posting"]) for e in batch["events"]]
        lines = [line for p in sorted(postings, key=priority) for line in format_lines([p])]
        body = ("Summer 2027 US SWE/CS/AI/ML candidates for a UCSD first-year "
                "CS student (2029–2030 graduation). Gaming companies appear first.\n\n"
                "Eligibility is based on listing metadata. A missing graduation year is "
                "allowed; verify the full description, class standing, and work authorization.\n\n"
                + "\n".join(lines)
                + f"\n\n<!-- radar-ucsd:{batch['id']} -->")
        if len(body) > 60000:
            raise RuntimeError("Notification too large; split pending batch before retrying")
        self.request("issues", {"title": f"{len(postings)} new or changed internship candidates",
                                 "body": body, "assignees": [self.repo.split('/')[0]]})


def deliver(path: Path, state: dict[str, Any], github: Any) -> None:
    if not state.get("pending"):
        return
    markers = github.markers()
    for batch in list(state["pending"]):
        if batch["id"] not in markers:
            github.send(batch)
        state["pending"].remove(batch)
        save(path, state)  # receipt; remote marker covers a crash before this save


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
    print(json.dumps({"sources": total, "failures": failures,
                      "fetched": len(postings), "candidates": sum(map(eligible, postings)),
                      "priority_candidates": [p.company + ": " + p.title
                                              for p in sorted(postings, key=priority)
                                              if eligible(p) and priority(p)[0] < len(PRIORITY)]}))
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
