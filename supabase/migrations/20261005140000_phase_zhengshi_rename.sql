-- Owner decision (2026-10-05): the A–D phase is called 正式赛 in Chinese (not 线上赛/线上比赛).
update public.phases set name_zh = '正式赛' where slug = 'online';
update public.phases set description_zh = replace(description_zh, '线上赛结束后', '正式赛结束后') where slug = 'final-hidden';
