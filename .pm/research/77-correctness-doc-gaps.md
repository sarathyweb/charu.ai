# 77 — Small Correctness And Documentation Gaps

## Requirement Covered

Fix the first priority item from `mssing_features_todo.md`:

- `.env.example` missing `GOOGLE_CLOUD_LIVE_LOCATION`
- Dashboard metrics undercount/overcount and use server-local dates
- Dashboard integration connect flow has an auth mismatch
- `README.md` overclaims implemented AI features

## Research Inputs

- Codemigo knowledge search: `dashboard metrics timezone streak weekly completion auth integration env documentation audit`
- Codemigo use-case search: `fix dashboard metrics timezone streak integration auth token query header env README overclaims`
- Existing focused research:
  - `.pm/research/69-fix-weekly-call-stats-undercounting.md`
  - `.pm/research/70-fix-goal-completion-percentage-bias.md`
  - `.pm/research/71-fix-dashboard-timezone-handling.md`
  - `.pm/research/72-fix-streak-calculation-84-day-cap.md`
  - `.pm/research/73-document-google-cloud-live-location.md`
- Official docs checked:
  - Python `zoneinfo` for IANA timezone handling
  - Firebase Admin ID token verification docs for Bearer-token server verification
- Local implementation:
  - `app/api/dashboard.py`
  - `app/auth/firebase.py`
  - `website/src/app/(app)/integrations/page.tsx`
  - `website/src/lib/api.ts`
  - `app/config.py`
  - `.env.example`
  - `README.md`

## Findings

- Dashboard `today` should come from the user's IANA timezone and fall back to UTC for missing or invalid values.
- Heatmap can stay limited to 84 days, but streak calculations need all completed call dates.
- Weekly calls should count completed scheduled call rows, not distinct dates with activity.
- Weekly total should be `active call windows * 7`, not a hard-coded `7`.
- Goal completion should use morning/afternoon calls and `call_outcome_confidence in {"clear", "partial"}`, matching weekly-summary semantics.
- The dashboard Google connect route expects Firebase Bearer auth, but the frontend redirects with a query-string ID token. Browser redirects cannot attach an `Authorization` header.
- The safest local pattern is for the frontend to call the authenticated endpoint with `authFetch`, receive a generated OAuth start URL, then navigate the browser to that URL.
- `.env.example` should document `GOOGLE_CLOUD_LIVE_LOCATION` separately from `GOOGLE_CLOUD_LOCATION`.
- `README.md` currently describes missing features as implemented: full task CRUD/snooze, dedicated goal CRUD, calendar CRUD, Gmail compose/search/archive, voice web search, and web chat frontend.

## Implementation Pattern

- Add small dashboard helpers for timezone-aware `today`, all-history streaks, completed scheduled-call counts, active window counts, and goal completion percentages.
- Keep dashboard queries in SQLModel.
- Add a `redirect` query parameter to `/api/integrations/{service}/connect`; default keeps the old 302 behavior, while `redirect=false` returns JSON for the authenticated frontend.
- Update the frontend integration button to use `authFetch` and avoid query-string Firebase tokens.
- Update docs to accurately describe current capabilities.

## Required Packages

No new packages required.
