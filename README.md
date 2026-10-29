# Intern Radar — UCSD freshman CS profile

Find Summer 2027 US software, CS, AI and ML internship candidates. No prior
work experience, application cap, employer exclusion, or private history from
the original author is used by the hourly watcher.

Gaming companies get first placement in alerts: Xbox, PlayStation and Nintendo.
NVIDIA, Google, Microsoft and Salesforce stay priorities too. Other companies,
including TikTok and ByteDance, remain eligible.

## What runs

`.github/workflows/watch.yml` runs `python -m intern_radar.ucsd` hourly at minute
17 UTC. GitHub schedules are best effort, may run late, and use the default
branch only. The PR branch does not activate the production schedule.
The review branch also runs the real watcher when its watcher code or workflow changes,
so state writes and alert delivery can be checked before merging. Data-only
commits do not trigger it. Every watch run tests the code before scanning.

1. Fetch public job feeds.
2. Group duplicate job URLs and combine their available information.
3. Check US location, Summer 2027, CS-related internship, and freshman-compatible
   requirements. A missing graduation year is allowed. Explicit junior/senior
   requirements or incompatible graduation years are excluded when supplied.
4. Save new matches or important changes in `data/ucsd.json` before sending.
5. Create GitHub Issues for pending alerts, then save delivery receipts.

The first run creates a quiet baseline of current matches, so it does not flood
you with old openings. Optional **bootstrap** resets that baseline. Both require
every configured source to succeed. Unchanged checks send
nothing. Failed alerts stay pending. A marker inside each issue prevents
duplicate delivery after a crash. Changes to job title, location, term, degree,
or supplied eligibility requirements can alert; posting-date refreshes and
tracking links do not.
Shortened community links are expanded before duplicate and season checks.
At most 100 uncached links are checked per scan; failed or deferred links wait
for another scan and do not create alerts. Resolved links are cached. A job URL
that explicitly says Spring/Fall/Winter 2027 is not accepted solely because a
community list labels it Summer 2027.

This uses new state, separate from the original author's `data/seen.json`,
inbox, tracker, discovered boards and application history. Those legacy files
are not read by the hourly profile. Legacy command-line tools remain available.

## Coverage and honest limits

- Simplify, vanshb03 and speedyapply community feeds plus configured public
  Greenhouse, Lever, Ashby, Workday and SmartRecruiters boards.
- PlayStation: verified public Greenhouse internship board
  `usinternshipspubliccareerssite`. Description text is fetched.
- Nintendo: public careers job cards and internship descriptions.
- Xbox: Microsoft/Xbox listings from community feeds. No verified direct
  Microsoft career-site parser.
- NVIDIA and Salesforce: configured Workday boards plus community feeds.
- Google STEP/ASDI and Microsoft Explore: recognized when the feeds list them.
  A direct Google/Microsoft connection is not claimed.

Some sources omit full descriptions. Alerts are **potential matches**, not a
guarantee that the employer accepts freshmen. Work authorization and skills
still need checking. Unknown-country remote roles and undated direct listings
without Summer 2027 evidence are excluded. No closures are inferred from a
missing row or source failure. Reopening a previously seen unchanged URL is
not currently detected. No automatic applications are submitted.

Only GitHub Issues are used by this profile. Discord/ntfy settings in legacy
tools do not affect it. GitHub email depends on your notification settings;
verify receipt of the first real alert.

## Verification and operation

The read-only test workflow runs the full tests on Linux and a live dry scan.
It never creates job alerts or writes watcher state.

```sh
python -m pip install pytest pyyaml
python -m pytest -q
PYTHONPATH=src python -m intern_radar.ucsd --dry-run
```

The production workflow commits the outbox before delivery. If a push fails,
delivery stops; if delivery fails, pending alerts retry next hour. The receipt
step runs after partial failures. Source errors appear in Actions logs, not
extra health-notification issues. If every source fails, the run fails.

Before relying on it:
- Confirm a successful Linux test run and live scan on this PR.
- Run **watch** manually on the PR branch to check state writes and real issue
  delivery. Confirm you receive the notification.
- Enable **Issues** under repository Settings → Features. This fork initially
  has Issues disabled, which blocks GitHub alert delivery.
- Merge only after reviewing those results. Then confirm a scheduled run on
  the default branch. If this repository is a fork, enable Actions if needed.

GitHub can disable public-repo schedules after prolonged inactivity. Check
Actions if alerts unexpectedly stop. No claim of live operation is made by
this README; inspect the actual run history.
