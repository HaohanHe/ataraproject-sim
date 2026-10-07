-- Partial indexes for the per-organization load and public-pool queries
-- (observer_organizations_by_load, observer_public_free_target, the max_active count).
-- Without them every dispatch and the two per-minute reconcilers scanned observer_jobs in full.
-- Applied in production with CREATE INDEX CONCURRENTLY on 2026-10-05; IF NOT EXISTS keeps this a no-op there.
-- The predicate must stay textually status in ('queued','dispatched','claimed') to match those queries.
create index if not exists observer_jobs_active_by_org
  on private.observer_jobs (runner, organization)
  where status in ('queued','dispatched','claimed');

create index if not exists observer_jobs_finished_by_org
  on private.observer_jobs (runner, organization, finished_at) include (claimed_at)
  where claimed_at is not null and finished_at is not null;
