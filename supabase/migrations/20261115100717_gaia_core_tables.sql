-- GAIA persistence: monitors, jobs, engine results and caches move from the local data/ folder
-- into Postgres.
--
-- Access model — server only, no anon access:
--   * The FastAPI backend is the only client. It uses the SECRET key (sb_secret_…, role service_role),
--     which bypasses RLS. The browser never talks to Supabase; it calls the backend.
--   * anon and authenticated get NO grants and there are NO policies, so the publishable/anon key
--     can't read or write anything here — not even through a misconfigured client.
--   * RLS is still enabled on every table (defence in depth: if a grant is ever added by mistake,
--     RLS with no policies still returns nothing).
--   * Tables are not exposed to the Data API by default (Supabase, Oct 2026), so service_role
--     gets an explicit GRANT.
--
-- Each row keeps the full document the backend already produces in `doc` (jsonb). A few fields
-- are copied into real columns for filtering, ordering and indexes; the backend writes both.

-- --------------------------------------------------------------------------------------------
-- updated_at helper
-- --------------------------------------------------------------------------------------------
create or replace function public.gaia_touch_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- Functions are executable by PUBLIC by default; this one is only for triggers.
revoke all on function public.gaia_touch_updated_at() from public, anon, authenticated;

-- --------------------------------------------------------------------------------------------
-- monitors: a place (or drawn area) the user asked GAIA to watch
-- --------------------------------------------------------------------------------------------
create table public.monitors (
  id                 text primary key,
  query              text,
  name               text,
  last_job_id        text,
  last_run_status    text,
  latest_acquisition date,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  doc                jsonb not null
);

comment on table public.monitors is 'GAIA monitored areas (one per search/drawn area). Backend-only (service_role).';

-- list_monitors() returns newest first
create index monitors_created_at_idx on public.monitors (created_at desc);

create trigger monitors_touch_updated_at
  before update on public.monitors
  for each row execute function public.gaia_touch_updated_at();

-- --------------------------------------------------------------------------------------------
-- jobs: one background run of the engines for a monitor
-- --------------------------------------------------------------------------------------------
create table public.jobs (
  id          text primary key,
  monitor_id  text references public.monitors (id) on delete set null,
  kind        text,
  status      text not null
              check (status in ('queued', 'running', 'done', 'partial', 'failed', 'cancelled')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  doc         jsonb not null
);

comment on table public.jobs is 'GAIA analysis jobs (engine status + results per run). Backend-only (service_role).';

-- foreign keys are not indexed automatically
create index jobs_monitor_id_idx on public.jobs (monitor_id);

-- the queue: only queued/running jobs, in run order (small, hot)
create index jobs_active_idx on public.jobs (created_at)
  where status in ('queued', 'running');

create trigger jobs_touch_updated_at
  before update on public.jobs
  for each row execute function public.gaia_touch_updated_at();

-- --------------------------------------------------------------------------------------------
-- results: engine outputs (the full uncertainty contract), keyed by the pipeline's cache key
-- --------------------------------------------------------------------------------------------
create table public.results (
  cache_key   text primary key,
  engine      text not null
              check (engine in ('water_change', 'flood', 'vegetation', 'deformation')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  doc         jsonb not null
);

comment on table public.results is 'GAIA engine results (aoi, dates, coverage, flags, calibration, stats, limitations, confidence). Backend-only.';

create index results_engine_created_idx on public.results (engine, created_at desc);

create trigger results_touch_updated_at
  before update on public.results
  for each row execute function public.gaia_touch_updated_at();

-- --------------------------------------------------------------------------------------------
-- cache_entries: small lookups that are expensive to repeat
--   geocode    → OpenStreetMap Nominatim answers (keyed by normalised query)
--   rain       → GPM IMERG 3-day rainfall per pass and area
--   references → JRC Global Surface Water reference areas per AOI
-- --------------------------------------------------------------------------------------------
create table public.cache_entries (
  namespace   text not null check (namespace in ('geocode', 'rain', 'references')),
  key         text not null,
  doc         jsonb not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  primary key (namespace, key)
);

comment on table public.cache_entries is 'GAIA lookup caches (geocode, rain, reference areas). Backend-only.';

create trigger cache_entries_touch_updated_at
  before update on public.cache_entries
  for each row execute function public.gaia_touch_updated_at();

-- --------------------------------------------------------------------------------------------
-- Access: service_role only. No policies for anon/authenticated on purpose.
-- --------------------------------------------------------------------------------------------
alter table public.monitors      enable row level security;
alter table public.jobs          enable row level security;
alter table public.results       enable row level security;
alter table public.cache_entries enable row level security;

revoke all on table public.monitors, public.jobs, public.results, public.cache_entries
  from anon, authenticated;

grant select, insert, update, delete
  on table public.monitors, public.jobs, public.results, public.cache_entries
  to service_role;
