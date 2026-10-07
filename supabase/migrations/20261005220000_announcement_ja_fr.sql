-- Optional Japanese / French announcement text. Empty or null falls back to English on the site.
alter table public.announcements add column if not exists title_ja text;
alter table public.announcements add column if not exists body_ja text;
alter table public.announcements add column if not exists title_fr text;
alter table public.announcements add column if not exists body_fr text;
