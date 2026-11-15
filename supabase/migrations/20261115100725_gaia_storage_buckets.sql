-- GAIA output files: change/flood/season/velocity maps (Cloud Optimized GeoTIFF) and the coloured
-- overlay PNG + legend JSON the dashboard draws over the satellite map.
--
-- Private bucket, no storage.objects policies on purpose:
--   * storage.objects has RLS enabled by Supabase. With no policies, the anon / publishable key and
--     signed-in users can neither list, read nor upload anything.
--   * Only the FastAPI backend (secret key → service_role, which bypasses RLS) uploads and reads
--     files. The browser downloads them through the backend (/files, /overlay), never from Storage.
--
-- 50 MB per file: the Free plan's global upload cap. GAIA's COGs are well under it (Lagos ≈ 2–25 MB).
-- data/ingest (raw NISAR cut-outs, GBs) is deliberately NOT uploaded: it is a re-downloadable
-- scratch cache that stays on the machine running the backend.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'gaia-outputs',
  'gaia-outputs',
  false,
  52428800,
  array['image/tiff', 'image/png', 'application/json']
)
on conflict (id) do update
  set public             = excluded.public,
      file_size_limit    = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;
