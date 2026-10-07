"""The online board with the added cards A1-D1 in the browser: before the switch nothing new shows; after it the
overall tab and the A-D tabs are unchanged (old 4-card evaluations keep their ranks), and the super board (超级总榜)
and the A1-D1 tabs appear after them. SUPERBOARD_SHOTS=<dir> also saves desktop and mobile screenshots.
Card scores of the plus-staging team are the two real 8-card staging evaluations of 2026-10-05."""
import os
import re
import secrets
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from test_project_http import edge_stack  # noqa: F401
from test_project_portal_browser import portal_site  # noqa: F401
from test_project_database import identity, query
from test_card_board import batch, card
from test_card_board_browser import team_named, texts
from test_super_board_db import fail_run

pytestmark = pytest.mark.skipif(not all(os.environ.get(k) for k in ('OBSERVER_DENO_BIN', 'SAC_POSTGREST_BIN', 'SAC_NODE_BIN')),
                                reason='Local browser toolchain required')

STAGING = [  # A, B, C, D, A1, B1, C1, D1
    [23236.3, 37064.6, 25427.6, 31848.9, 22063.4, 33177.9, 24969.6, 28096.5],
    [23256.3, 37038.6, 25154.5, 31842.4, 22118.0, 35200.5, 24958.8, 31404.2],
]
VIEWS = {'desktop': {'width': 1365, 'height': 950}, 'mobile': {'width': 390, 'height': 844}}


def test_online_board_with_added_cards(portal_site, edge_stack):
    uri = edge_stack['harness'].db_uri; tag = secrets.token_hex(3)
    shots = Path(os.environ['SUPERBOARD_SHOTS']) if os.environ.get('SUPERBOARD_SHOTS') else None
    query(uri, "update public.phases set slug=slug||'-old-'||%s,is_active=false where slug='online'", (tag,))
    phase = query(uri, """insert into public.phases(slug,name_en,name_zh,counts_for_final,is_active,leaderboard_mode,starts_at,ends_at)
                  values('online-'||%s,'Online','正式赛',false,true,'live',now()-interval '1 day',now()+interval '2 days') returning id""", (tag,))[0][0]
    query(uri, """insert into public.observer_phase_settings(phase_id,projects_enabled,local_sessions_enabled,daily_batches,
                  model_token_limit,model_call_limit,board_layout) values(%s,true,true,80,1000,10,'cards_overall')""", (phase,))
    query(uri, "update private.observer_site_mode set mode='competition',phase_id=%s", (phase,))
    ad = [card(uri, phase, f'v4-{c}', f'Card {c.upper()}') for c in 'abcd']
    s = {'uri': uri, 'phase': phase}

    def online(shown):
        # Batches are created under a working slug (local, CSV-free test batches are refused on 'online'), shown as 'online'.
        query(uri, "update public.phases set slug=%s where id=%s", ('online' if shown else 'online-' + tag, phase))

    old_team = team_named(uri, 'Steady ' + tag)
    batch(uri, phase, old_team, dict(zip(ad, [30000, 30000, 30000, 30000])))           # before the switch

    def board_state(page):
        return texts(page.get_by_test_id('board-cards').locator('button'))

    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('OBSERVER_BROWSER_CHANNEL'))

        def shoot(name, url, view='desktop'):
            page = browser.new_page(viewport=VIEWS[view])
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.goto(portal_site + url)
            expect(page.get_by_test_id('lb-row').first).to_be_visible(timeout=15000)
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f'{name}-{view}.png'), full_page=True)
            return page

        online(True)
        before = shoot('1-before-switch-overall', '/leaderboard/online?lang=zh&scenario=overall')
        assert board_state(before) == ['总榜', '任务卡 A', '任务卡 B', '任务卡 C', '任务卡 D']
        before_rows = texts(before.get_by_test_id('lb-row'))
        expect(before.get_by_test_id('board-tab-super')).to_have_count(0)
        before.close()

        # Switch on: A1-D1 join the phase (staging-like version suffix); evaluations now run 8 cards.
        extra = [card(uri, phase, f'v4-{c}1-v1', f'Card {c.upper()}1 v1') for c in 'abcd']
        page = shoot('2-after-switch-overall', '/leaderboard/online?lang=zh&scenario=overall')
        online(False)
        assert board_state(page) == ['总榜', '任务卡 A', '任务卡 B', '任务卡 C', '任务卡 D',
                                     '超级总榜', '任务卡 A1', '任务卡 B1', '任务卡 C1', '任务卡 D1']
        assert texts(page.get_by_test_id('lb-row')) == before_rows                       # nothing moved
        # The super board is a card tab inside 正式赛, never a top-level board tab; the debug board stays far right.
        phase_tabs = [t.get_attribute('data-testid') for t in page.locator('.tabs a').all()]
        assert 'board-tab-super' not in phase_tabs
        assert 'board-tab-practice' not in phase_tabs or phase_tabs[-1] == 'board-tab-practice'
        expect(page.get_by_test_id('board-tab-online')).to_have_class(re.compile(r'(^|\s)active(\s|$)'))
        page.close()

        staging = team_named(uri, 'plus-staging ' + tag)
        for scores in STAGING:
            batch(uri, phase, staging, dict(zip(ad + extra, scores)))
        spiky = team_named(uri, 'Spiky ' + tag)
        batch(uri, phase, spiky, dict(zip(ad + extra, [20000] * 4 + [40000] * 4)))
        broken_user = team_named(uri, 'A1 crashed ' + tag)
        s['user'] = broken_user
        b = batch_running(uri, phase, broken_user, dict(zip(ad + extra[1:], [32000] * 4 + [1000] * 3)))
        fail_run(s, b, extra[0])                                                          # A1 failed: still on A-D
        online(True)

        for view in VIEWS:
            page = shoot('3-overall', '/leaderboard/online?lang=zh&scenario=overall', view)
            rows = page.get_by_test_id('lb-row')
            expect(rows).to_have_count(4)
            expect(rows.nth(0)).to_contain_text('A1 crashed ' + tag)                       # 32000
            expect(rows.nth(1)).to_contain_text('Steady ' + tag)                           # 30000 (old 4-card)
            expect(rows.nth(2)).to_contain_text('plus-staging ' + tag)                     # 29394.35 (A-D mean)
            expect(page.get_by_test_id('card-col-v4-a1-v1')).to_have_count(0)
            page.close()

            page = shoot('4-super', '/leaderboard/online?lang=zh&scenario=super', view)
            expect(page.get_by_test_id('board-card-super')).to_have_attribute('aria-pressed', 'true')
            expect(page.get_by_test_id('board-tab-super')).to_have_count(0)
            expect(page.get_by_test_id('board-tab-online')).to_have_class(re.compile(r'(^|\s)active(\s|$)'))
            rows = page.get_by_test_id('lb-row')
            expect(rows).to_have_count(2)                                                  # only complete 8-card evaluations
            expect(rows.nth(0)).to_contain_text('Spiky ' + tag)                            # 240000
            expect(rows.nth(1)).to_contain_text('plus-staging ' + tag)
            expect(rows.nth(1).locator('td').nth(2)).to_contain_text(re.compile(r'230[,\s]?973'))  # its best 8-card total
            expect(page.get_by_test_id('card-col-v4-d1-v1')).to_be_visible()
            page.close()

            page = shoot('5-card-a1', '/leaderboard/online?lang=zh&scenario=v4-a1-v1', view)
            rows = page.get_by_test_id('lb-row')
            expect(rows.nth(0)).to_contain_text('Spiky ' + tag)
            page.close()

        page = shoot('6-overall-en', '/leaderboard/online?lang=en')
        assert board_state(page)[-5:] == ['SUPER BOARD', 'Card A1', 'Card B1', 'Card C1', 'Card D1']
        page.close()
        browser.close()
    query(uri, "update public.phases set is_active=false,slug='online-super-'||%s where id=%s", (tag, phase))
    query(uri, "update private.observer_site_mode set mode='practice',phase_id=null")
    assert not errors, errors


def batch_running(uri, phase, user, scores):
    """A batch whose listed cards score while the others are still running."""
    from test_project_database import rpc
    b = rpc(uri, 'observer_create_batch', phase, None, role='authenticated', user=user)
    for run, scenario in query(uri, 'select id,scenario_id from public.observer_runs where batch_id=%s', (b,)):
        if scenario in scores:
            query(uri, "update public.observer_runs set status='scored',score=%s,finished_at=now() where id=%s", (scores[scenario], run))
        else:
            query(uri, "update public.observer_runs set status='running' where id=%s", (run,))
    return b
