import copy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from intern_radar.models import Posting
from intern_radar.ucsd import deliver, eligible, priority, reconcile, save
from intern_radar.sources.nintendo import parse_cards
from intern_radar.sources.greenhouse import parse_greenhouse
import intern_radar.ucsd as ucsd


def job(**kwargs):
    base = Posting("a", "simplify", "TikTok", "Software Engineer Intern",
                   "https://jobs.lever.co/tiktok/123", ("San Diego, CA",),
                   ("Summer 2027",), "Software", ("Bachelor's",))
    return replace(base, **kwargs)


@pytest.mark.parametrize("changes,expected", [
    ({}, True),
    ({"company": "ByteDance", "degrees": ()}, True),
    ({"title": "Software Intern - Freshmen and Sophomores"}, True),
    ({"title": "Software Intern - graduating 2029–2030"}, True),
    ({"title": "Software Intern - graduating 2028"}, False),
    ({"title": "Software Intern - Juniors only"}, False),
    ({"title": "Software Intern - third-year"}, False),
    ({"title": "PhD Machine Learning Intern"}, False),
    ({"degrees": ("Master's",)}, False),
    ({"locations": ("Toronto, Ontario, CA",)}, False),
    ({"locations": ("London, UK", "Seattle, WA")}, True),
    ({"locations": ("Remote",)}, False),
    ({"locations": ("Remote, United States",)}, True),
    ({"terms": ("Summer 2026",)}, False),
    ({"terms": (), "title": "Software Intern Summer 2027"}, True),
    ({"terms": (), "title": "Software Intern Fall 2027"}, False),
    ({"terms": (), "title": "Software Intern"}, False),
    ({"title": "Marketing Intern", "category": "Marketing"}, False),
    ({"company": "Google", "title": "STEP Intern"}, True),
    ({"company": "Google", "title": "ASDI"}, True),
    ({"company": "Google", "title": "Associate Software Developer Intern"}, True),
    ({"company": "Microsoft", "title": "Explore"}, True),
    ({"company": "Other", "title": "Explore"}, False),
    ({"url": "javascript:alert(1)"}, False),
    ({"description": "A rising senior pursuing a BS in Computer Science."}, False),
    ({"description": "Open to freshmen and sophomores studying CS."}, True),
    ({"terms": (), "description": "Join us during the summer of 2027."}, True),
    ({"description": "Expected graduation between May 2026 and December 2029."}, True),
])
def test_profile(changes, expected):
    assert eligible(job(**changes)) is expected


def test_dedupe_and_meaningful_changes():
    p = job()
    variant = replace(p, key="b", source="lever", terms=(),
                      url=p.url + "/apply?utm_source=feed")
    state = reconcile({}, [p, variant])
    assert len(state["pending"]) == 1
    assert len(state["pending"][0]["events"]) == 1
    state["pending"] = []
    reconcile(state, [replace(p, posted_at="2026-11-01"), variant])
    assert not state["pending"]
    reconcile(state, [replace(p, locations=("Seattle, WA",))])
    assert len(state["pending"]) == 1
    state["pending"] = []
    reconcile(state, [])
    reconcile(state, [replace(p, locations=("Seattle, WA",))])
    assert not state["pending"]


def test_baseline_and_ineligible_to_eligible():
    state = reconcile({}, [job()], baseline=True)
    assert not state.get("pending")
    reconcile(state, [job(title="Software Intern - graduating 2028")])
    assert not state.get("pending")
    reconcile(state, [job(title="Software Intern - graduating 2029")])
    assert len(state["pending"]) == 1


class FakeGitHub:
    def __init__(self):
        self.sent = []
        self.fail = False

    def markers(self):
        return {b["id"] for b in self.sent}

    def send(self, batch):
        if self.fail:
            raise OSError("delivery failed")
        self.sent.append(copy.deepcopy(batch))


def test_failed_delivery_retry_and_lost_receipt(tmp_path):
    state = reconcile({}, [job()])
    path = tmp_path / "state.json"
    save(path, state)
    github = FakeGitHub()
    github.fail = True
    with pytest.raises(OSError):
        deliver(path, state, github)
    assert state["pending"]
    github.fail = False
    lost_receipt = copy.deepcopy(state)
    deliver(path, state, github)
    deliver(path, lost_receipt, github)
    assert not state["pending"] and not lost_receipt["pending"]
    assert len(github.sent) == 1


def test_no_changes_never_contacts_github(tmp_path):
    deliver(tmp_path / "state.json", {}, object())


def test_large_batches_split():
    state = reconcile({}, [job(url=f"https://example.com/{n}") for n in range(81)])
    assert [len(b["events"]) for b in state["pending"]] == [40, 40, 1]


def test_gaming_priority_keeps_other_companies():
    jobs = [job(company=c) for c in ["Stripe", "NVIDIA", "Google", "Nintendo", "Xbox"]]
    assert [p.company for p in sorted(jobs, key=priority)] == [
        "Xbox", "Nintendo", "NVIDIA", "Google", "Stripe"]
    assert all(eligible(p) for p in jobs)


def test_description_and_list_metadata_merge_before_eligibility():
    p = job()
    ats = replace(p, source="greenhouse", terms=(), description="Open to first-year students.")
    state = reconcile({}, [p, ats])
    assert len(state["pending"][0]["events"]) == 1
    assert state["pending"][0]["events"][0]["posting"]["description"]


def test_feed_outage_does_not_erase_eligibility_and_renotify():
    p = job(description="Open to freshmen for summer 2027.")
    state = reconcile({}, [p], baseline=True)
    reconcile(state, [replace(p, source="lever", terms=(), degrees=(), category="",
                              description="")])
    assert not state.get("pending")
    reconcile(state, [p])
    assert not state.get("pending")


def test_repeated_material_changes_get_distinct_delivery_ids():
    state = reconcile({}, [job()])
    original = state["pending"][0]["id"]
    state["pending"] = []
    reconcile(state, [job(locations=("Seattle, WA",))])
    state["pending"] = []
    reconcile(state, [job()])
    assert state["pending"][0]["id"] != original


def test_irrelevant_fulltime_jobs_do_not_bloat_saved_history():
    state = reconcile({}, [job(title="Software Engineer"), job(url="https://example.com/intern")])
    assert len(state["known"]) == 1


def test_conflicting_spring_url_is_not_summer():
    assert not eligible(job(url="https://example.com/Reporting-Co-op-Spring-2027_JR123"))
    assert eligible(job(title="Software Intern Spring 2027 & Summer 2027",
                        url="https://example.com/spring-2027"))


def test_short_links_resolve_before_dedupe_and_keep_seen_history(monkeypatch):
    direct = job()
    short = replace(direct, url="https://go.vansh.nyc/abc", source="vanshb03")
    state = reconcile({}, [short], baseline=True)
    monkeypatch.setattr(ucsd, "expand_short_link", lambda url: direct.url)
    rows = ucsd.resolve_short_links([direct, short], state)
    assert rows[0].url == rows[1].url
    reconcile(state, rows)
    assert not state.get("pending")
    assert len(state["known"]) == 1
    monkeypatch.setattr(ucsd, "expand_short_link", lambda url: pytest.fail("cache missed"))
    assert ucsd.resolve_short_links([short], state)[0].url == direct.url


def test_link_cleanup_does_not_alert_on_old_source_metadata_changes(monkeypatch):
    short = job(url="https://go.vansh.nyc/abc", locations=("Seattle, WA",))
    direct = job()
    state = reconcile({}, [short, direct], baseline=True)
    monkeypatch.setattr(ucsd, "expand_short_link", lambda url: direct.url)
    reconcile(state, ucsd.resolve_short_links([short, direct], state))
    assert not state.get("pending")
    assert "_link_migrations" not in state


def test_unresolved_short_links_wait_without_alerting(monkeypatch):
    short = job(url="https://go.vansh.nyc/abc")
    def fail(url):
        raise OSError("shortener unavailable")
    monkeypatch.setattr(ucsd, "expand_short_link", fail)
    state = {}
    assert not ucsd.resolve_short_links([short], state)
    reconcile(state, [])
    assert not state.get("pending")


def test_gaming_parsers():
    cards = '<a href="/jobs/123/"><h3>Software Intern</h3><span>Redmond, WA</span></a>'
    assert parse_cards(cards)[0].company == "Nintendo"
    assert not parse_cards(cards.replace("Software Intern", "Software Engineer"))
    rows = parse_greenhouse("test", {"jobs": [{"id": 1, "title": "Software Intern",
        "absolute_url": "https://example.com/1", "content": "&lt;p&gt;First year&lt;/p&gt;"}]})
    assert rows[0].description == "First year"


def test_workflow_security_and_persistence_order():
    root = Path(__file__).resolve().parents[1]
    watch = yaml.load((root / ".github/workflows/watch.yml").read_text(), Loader=yaml.BaseLoader)
    assert watch["on"]["schedule"][0]["cron"] == "17 * * * *"
    assert set(watch["on"]) == {"schedule", "workflow_dispatch", "push"}
    assert watch["on"]["push"]["branches"] == ["customize-ucsd-ai-2027"]
    assert watch["on"]["push"]["paths"] == [".github/workflows/watch.yml", "src/intern_radar/**"]
    assert watch["concurrency"]["cancel-in-progress"] == "false"
    steps = watch["jobs"]["watch"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert names.index("Persist notification outbox") < names.index("Deliver pending notifications")
    assert names.index("Deliver pending notifications") < names.index("Persist delivery receipts")
    assert steps[-1]["if"] == "always()"
    ci = yaml.load((root / ".github/workflows/test.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in ci["on"]
    assert "--dry-run" in ci["jobs"]["tests"]["steps"][-1]["run"]


def test_first_run_baseline_and_failed_source_preserves_state(tmp_path, monkeypatch):
    state = tmp_path / "ucsd.json"
    monkeypatch.setattr("sys.argv", ["ucsd", "--state", str(state)])
    monkeypatch.setattr(ucsd, "fetch", lambda config: ([job()], ["bad source"], 2))
    assert ucsd.main() == 1
    assert not state.exists()
    monkeypatch.setattr(ucsd, "fetch", lambda config: ([job()], [], 1))
    assert ucsd.main() == 0
    import json
    assert not json.loads(state.read_text()).get("pending")
    before = state.read_bytes()
    monkeypatch.setattr("sys.argv", ["ucsd", "--state", str(state), "--dry-run"])
    assert ucsd.main() == 0
    assert state.read_bytes() == before


def test_delivery_checks_issues_enabled_even_when_no_pending(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ucsd", "--phase", "deliver", "--state",
                                    str(tmp_path / "ucsd.json")])
    class Disabled:
        def request(self, route):
            return {"has_issues": False}
    monkeypatch.setattr(ucsd, "GitHub", Disabled)
    with pytest.raises(RuntimeError, match="Enable Issues"):
        ucsd.main()
