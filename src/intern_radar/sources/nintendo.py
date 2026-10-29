"""Public Nintendo careers cards and internship detail pages, no browser login."""
import re

from intern_radar.http import get_text
from intern_radar.jd import html_to_text
from intern_radar.models import Posting

BASE = "https://careers.nintendo.com"


def parse_cards(markup: str) -> list[Posting]:
    postings = []
    for job_id, card in re.findall(r'<a\b[^>]*href="/jobs/(\d+)/"[^>]*>(.*?)</a>',
                                    markup, re.S):
        title = re.search(r"<h3\b[^>]*>(.*?)</h3>", card, re.S)
        locations = re.findall(r"<span\b[^>]*>(.*?)</span>", card, re.S)
        if title is None or not locations:
            continue
        name = html_to_text(title.group(1))
        if not re.search(r"\bintern(?:ship)?\b", name, re.I):
            continue
        postings.append(Posting(
            key=f"nintendo:{job_id}", source="nintendo", company="Nintendo",
            title=name, url=f"{BASE}/jobs/{job_id}/",
            locations=(html_to_text(locations[0]),),
        ))
    return postings


def fetch_nintendo() -> list[Posting]:
    from dataclasses import replace
    markup = get_text(BASE + "/jobs/")
    # A changed layout must fail visibly instead of reporting a healthy empty feed.
    if not re.search(r'href="/jobs/\d+/"', markup):
        raise ValueError("Nintendo: no job cards found; inspect public careers layout")
    return [replace(p, description=html_to_text(get_text(p.url))) for p in parse_cards(markup)]
