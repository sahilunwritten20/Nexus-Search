# Review Worklog — review/hardening

Chronological log of the hostile engineering review. Every entry: date, action,
command(s), real output, decision. A fresh session must be able to resume from
this file + REVIEW_CHECKLIST.md + CLAIMS_LEDGER.md alone.

Conventions:
- `[E]` = evidence (command + output pasted or referenced)
- `[F]` = finding (gets an ID in REVIEW_CHECKLIST.md)
- `[D]` = decision with rationale
- PowerShell 5.1 / Windows, Python 3.14.7, repo at `C:\Users\Admin\Desktop\nexus search\nexus-search`

---

## 2026-10-13 — Session 1

### S1.0 Branch + environment established

```
[E] git checkout -b review/hardening   -> "Switched to a new branch 'review/hardening'"
[E] .venv\Scripts\python.exe --version -> Python 3.14.7
[E] pip list (key): fastapi 0.141.1, pytest 9.1.1, pytest-xdist 3.8.0, pytest-cov 7.1.0,
    httpx 0.28.1, sentence-transformers 6.1.0, torch 2.14.0, networkx 3.7 (NOTE: networkx
    is installed in the venv but is NOT in requirements.txt or requirements-dev.txt —
    no import found yet; if it's only used by dev scripts this is fine, else a finding.)
[E] pytest.ini: testpaths = tests (only)
[E] Repo tree mapped (full listing in git history of this commit). Source layout:
    nexus_search/{core,ingestion,crawler,ranking,links,rag,evaluation}
    tests/{core,crawler,evaluation,ingestion,links,rag,ranking,golden} + root tests
    Databases committed-ish in worktree (gitignored per .gitignore - verify):
    nexus_search.db, real_crawl.db, real_crawl_frontier.db, smoke.db, smoke_frontier.db
```

[D] Use existing .venv for the first baseline run (packages were installed from
the pinned requirements; pip freeze matches requirements.txt pins for the
runtime deps — spot-checked fastapi/uvicorn/pydantic/pypdf/requests versions).
A from-scratch clean-venv install is scheduled separately (A2 requirement).

Next: A2 baseline suite runs.
