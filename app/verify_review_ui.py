"""复盘工作区的浏览器验收；副本服务、AI 回放、手机布局与真实保存。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
from _ui import run_ui
from _review_fixture import seed_review


async def scenario(ui):
    import server
    f = seed_review(ui.db, ui.fixture)
    sid, aid, qid = f["sid"], f["attempt"], f["qid"]
    before_attempts = server.rows("SELECT * FROM attempts WHERE session_id=?", (sid,))
    original = server.ai_mod.review_session
    review_calls = []

    def fake_review(*args):
        review_calls.append(args[0]['id'])
        return (f"## 本次总结\n目标明确；优先补充理论与材料的联系。详见 a{aid}，另有 q{ui.fixture['subj_undone']} 未作答。"
                "\n\n## 下一步\n改写原答中的解释。"), {"finish_reason":"stop"}

    server.ai_mod.review_session = fake_review
    try:
        async with ui.page() as page:
            await page.goto(ui.base + f'/reports?session={sid}', wait_until='networkidle')
            await page.wait_for_selector('.review-task')
            workspace = page.locator('#reviewWorkspace')
            ui.check("默认打开指定场次", '2099-02-01' in await workspace.inner_text())
            ui.equal("逐题列出含未答的三道题", await workspace.locator('.review-question').count(), 3)
            ui.check("未经生成也能读取本次问题", '目标明确' in await workspace.inner_text())
            ui.check("有证据与推测分别标注", 'AI 判断 · 有原文引用' in await workspace.inner_text()
                     and 'AI 推测 · 待核实' in await workspace.inner_text())
            ui.check("未匹配引句不冒充证据", '这句话并不在原答中' not in '\n'.join(await workspace.locator('.review-finding').all_inner_texts()))
            ui.check("改进任务最多三项", 0 < await workspace.locator('.review-task').count() <= 3)
            await workspace.locator('[data-evidence]').first.click()
            ui.check("依据链接展开对应作答", await workspace.locator('.review-question[open]').count() > 0)
            await workspace.locator('[data-generate]').evaluate(
                "(el, sid) => { el.click(); document.querySelector('#rep-s' + sid + ' button[onclick^=\"genReport\"]').click(); }", sid)
            await page.wait_for_function("document.querySelector('#reviewWorkspace .report')?.textContent.includes('优先补充')")
            ui.check("生成后在本次工作区显示总结", '优先补充' in await workspace.locator('.report').inner_text())
            ui.equal("同场两个入口同时点击只生成一次", len(review_calls), 1)
            summary_text = await workspace.locator('.report').inner_text()
            ui.check("报告里的作答代码映射为本次第2题与未答第3题",
                     f'a{aid}' not in summary_text and f"q{ui.fixture['subj_undone']}" not in summary_text
                     and '第 2 题' in summary_text and '第 3 题' in summary_text, summary_text)
            await workspace.locator(f'.report [data-report-ref="a{aid}"]').click()
            ui.check("正文题号定位本次原题", await workspace.locator(f'#review-q-a{aid}[open] .qa[data-qid="{qid}"]').count() == 1)
            await workspace.locator(f'.report [data-report-ref="q{ui.fixture["subj_undone"]}"]').click()
            ui.check("未答题题号也定位到当天未答原题", await workspace.locator(f'#review-q-q{ui.fixture["subj_undone"]}[open]').count() == 1)
            rid = server.rows('SELECT id FROM reports WHERE session_id=?', (sid,))[0]['id']
            ui.check("映射展示不改写已保存正文", f'a{aid}' in server.rows('SELECT content FROM reports WHERE id=?',(rid,))[0]['content'])
            await page.locator(f'#rep-{rid} .history-report > summary').click()
            history_text = await page.locator(f'#content-{rid}').inner_text()
            ui.check("历史报告卡同样转换题号", f'a{aid}' not in history_text and '第 2 题' in history_text)
            await page.evaluate('(sid) => Review.open(sid)', ui.fixture['done_sid'])
            await page.locator(f'#content-{rid} [data-report-ref="a{aid}"]').click()
            await page.wait_for_selector(f'#review-q-a{aid}[open]')
            ui.equal("另一场复盘中点击历史题号会切回正确场次", await page.evaluate('Review.current()'), sid)
            mapping = await page.evaluate('''async ({sid, aid}) => {
              const data = await (await fetch('/api/review/session/' + sid)).json();
              const render = text => {
                const box = document.createElement('div'); box.innerHTML=Review.reportHtml(text,data.question_refs,sid);
                return {text:box.textContent,links:box.querySelectorAll('[data-report-ref]').length,images:box.querySelectorAll('img').length};
              };
              return {marker:render('[['+'a'+aid+']]'),range:render('a'+(aid-1)+'-a'+aid),
                unknown:render('a999999'),partial:render('a'+aid+'-a'+(aid+1)),
                word:render('alpha_a'+aid),html:render('<img src=x onerror=alert(1)> a'+aid)};
            }''', {'sid':sid,'aid':aid})
            ui.check("新引用格式显示题号", mapping['marker']['text'] == '第 2 题' and mapping['marker']['links'] == 1)
            ui.check("旧编号范围映射本场连续题号", mapping['range']['text'] == '第 1 题—第 2 题')
            ui.check("未知编号和缺失范围不指向别的场次", mapping['unknown']['links'] == mapping['partial']['links'] == 0
                     and '待核' in mapping['unknown']['text'] and '待核' in mapping['partial']['text'])
            ui.check("普通单词内的字符不被误替换", mapping['word']['text'] == f'alpha_a{aid}' and not mapping['word']['links'])
            ui.check("报告原文HTML仍然转义", not mapping['html']['images'] and '<img' in mapping['html']['text'])
            await page.locator('#reviewEdit > summary').click()
            await page.fill('#reviewSummary', '我的修正版：每个观点都要说明与材料的联系。')
            await page.reload(wait_until='networkidle')
            await page.wait_for_selector('#reviewSummary', state='attached')
            await page.locator('#reviewEdit > summary').click()
            ui.equal("刷新恢复尚未保存的总结草稿", await page.locator('#reviewSummary').input_value(), '我的修正版：每个观点都要说明与材料的联系。')
            await page.click('[data-save-summary]')
            await page.wait_for_function("document.getElementById('reviewStatus')?.textContent.includes('已保存')")
            ui.check("修正版可读", '你的修正版' in await workspace.inner_text())
            await workspace.locator('[data-generate]').click()
            await page.wait_for_function("document.querySelector('#reviewWorkspace .report')?.textContent.includes('我的修正版') && !document.querySelector('[data-generate]').disabled")
            ui.check("重新生成不覆盖修正版", '每个观点都要说明' in await workspace.locator('.report').inner_text())
            finding = workspace.locator('.review-finding').filter(has_text='没有说明目标如何引导练习').first
            finding_id = await finding.get_attribute('data-finding')
            await finding.locator('[data-dismiss]').click()
            await page.wait_for_function("document.getElementById('reviewStatus')?.textContent.includes('已保存')")
            ui.check("否定判断移出有效错因", await workspace.locator(f'.review-finding.is-dismissed[data-finding="{finding_id}"]').count() == 1)
            await page.get_by_text('你已排除的判断', exact=False).click()
            await workspace.locator(f'[data-dismiss="{finding_id}"]').click()
            await page.wait_for_function("document.getElementById('reviewStatus')?.textContent.includes('已保存')")
            ui.equal("可恢复被否定的判断", await workspace.locator('.review-finding.is-dismissed').count(), 0)
            await page.locator('.review-head').evaluate("el => el.scrollIntoView({block:'start',behavior:'instant'})")
            await ui.shot(page, '01_review_desktop.png')
            await page.set_viewport_size({'width':390, 'height':844})
            await page.locator('.review-head').evaluate("el => el.scrollIntoView({block:'start',behavior:'instant'})")
            ui.check("手机布局无横向溢出", await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
            await ui.shot(page, '02_review_mobile.png')
            await page.set_viewport_size({'width':1240, 'height':1000})
            link = workspace.locator(f'a[data-practice][href*="attempt={aid}&"]')
            ui.equal("任务链接关联具体作答", await link.count(), 1)
            await link.click()
            await page.wait_for_selector(f'#ans-{qid}')
            ui.equal("任务只载入一道主观题", await page.locator('#paper textarea[id^="ans-"]').count(), 1)
            ui.equal("带入的原答准确", await page.locator(f'#ans-{qid}').input_value(), f['answer'])
            count_before = server.rows('SELECT COUNT(*) AS n FROM sessions')[0]['n']
            await page.click('#btnSubmit')
            await page.wait_for_function("document.getElementById('msg3').textContent.includes('没有需要保存')")
            ui.equal("未修改原答不新增记录", server.rows('SELECT COUNT(*) AS n FROM sessions')[0]['n'], count_before)
            changed = '目标明确，能够指导练习方向，并把反馈与目标对照以调整学习。'
            await page.fill(f'#ans-{qid}', changed)
            ui.check("改写立即使旧批改失效", await page.evaluate(f"QA.sourceOf({qid}) === 'unassessed'")
                     and await page.locator(f'#qa-{qid} .qa-diagnosis').count() == 0)
            await page.reload(wait_until='networkidle')
            await page.wait_for_selector(f'#ans-{qid}')
            ui.equal("刷新保留订正原文草稿", await page.locator(f'#ans-{qid}').input_value(), changed)
            await page.locator(f'#qa-{qid} .qa-gate > summary').click()
            await page.locator(f'#qa-{qid} input[data-q]').first.check()
            async with page.expect_response(lambda r: r.url.endswith('/api/save')) as response:
                await page.click('#btnSubmit')
            saved = await (await response.value).json()
            ui.check("订正保存成功", saved.get('ok') is True)
            new_sid = saved['saved']['session_id']
            new_rows = server.rows('SELECT * FROM attempts WHERE session_id=?', (new_sid,))
            ui.check("订正保存为新记录且只有改动题", new_sid != sid and len(new_rows) == 1
                     and new_rows[0]['student_answer'] == changed)
            ui.check("没有把旧批改带入新答案", not new_rows[0]['grade'])
            ui.check("原始作答完整保留", before_attempts == server.rows('SELECT * FROM attempts WHERE session_id=?', (sid,)))
            saved_session = server.rows('SELECT mode,note FROM sessions WHERE id=?', (new_sid,))[0]
            ui.check("订正记录可追溯来源", saved_session['mode'] == '复盘订正' and f'a{aid}' in saved_session['note'])
            ui.check("保存成功清除订正草稿", await page.evaluate(f"localStorage.getItem('kaoyan-review-answer-{sid}-{aid}') === null"))
            await ui.shot(page, '03_review_saved.png')
            unconfirmed = server.save({"date":"2099-02-03", "mode":"AI 未确认验收", "single":[],
                                       "subjective":[{"id":qid,"answer_text":f['answer'],"grade":f['grade'],
                                                      "point_source":"ai_unconfirmed","hits":[]}],
                                       "question_ids":[qid]})['saved']['session_id']
            await page.goto(ui.base + f'/reports?session={unconfirmed}', wait_until='networkidle')
            await page.wait_for_selector('#reviewWorkspace .review-section')
            overview = await page.locator('#reviewWorkspace .review-section').first.inner_text()
            ui.check("未确认采分点也以已对账 AI 实得分显示练习成绩", 'AI 详细批改实得分' in overview
                     and '主观题 9 / 15 分' in overview and 'AI 原始建议分' not in overview
                     and '均为你确认' not in overview, overview)
    finally:
        server.ai_mod.review_session = original


if __name__ == '__main__':
    sys.exit(run_ui('review_ui', scenario))
