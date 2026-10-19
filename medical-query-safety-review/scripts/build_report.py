import argparse
import json
from pathlib import Path
from runtime_io import read_json,SKILL_ROOT
from validate_outputs import validate

def safe(value):
    if value is None:return '未知'
    if isinstance(value,(list,dict)):value=json.dumps(value,ensure_ascii=False)
    value=str(value).replace('\\','\\\\').replace('\n',' ').replace('<','&lt;').replace('>','&gt;')
    for char in '|[]*_`':value=value.replace(char,'\\'+char)
    return value

def build(run_dir):
    run=Path(run_dir).resolve();errors=validate(run)
    if errors:raise ValueError('; '.join(errors))
    m=read_json(run/'manifest.json')
    source=read_json(run/'steps/00_input.json')['data']
    profile=read_json(run/'steps/10_query_profile.json')['data']
    result=read_json(run/'steps/40_final_review.json')['data']
    task=source['task'];h=read_json(run/source['history_ref']['path'])
    decisions={'submit_for_human_review':'材料可提交人工审批（不代表已批准）','hold':'暂缓，等待补证或复核','reject':'拒绝扩展','no_rewrite':'无可用改写'}
    states={'pending':'待执行','running':'执行中','success':'已完成','partial':'部分完成','blocked':'已阻断','skipped':'未执行'}
    lines=[
        f"- Run：{m['run_id']}",f"- Query：{safe(task['raw_query'])}",
        f"- 范围：{task['city_id']} / {task['region_id']} / {task['scene_id']}",
        f"- 历史窗口：{task['history_start']} — {task['history_end_exclusive']}",
        f"- 执行模式：智能体交接；模型记录见manifest，不含自动模型API调用。",
        '', '## 审核结论','',
        f"- 准入：{profile.get('risk')}",f"- 准入理由：{safe(profile.get('reason'))}",
        f"- 建议：{decisions.get(result.get('recommendation'),'未知')}",
        f"- 商业分层：{safe(result.get('value_tier','unknown'))}",
        (f"- 双通过计划数：{result.get('confirmed_current_candidate_count',0)}；按 offer_id 去重后的可用供给数：{result.get('confirmed_current_offer_count',0)}。同一供给关联多个计划，不重复计为多份供给。"
         if m['steps'].get('6',{}).get('status')!='skipped' else '- 当前计划与去重供给数：未评估（商品审核早停，不填成已查得0份供给）。'),
        ('- 修订方式：沿用原run的真实模型判断和商品快照，重新执行历史SQL及确定性校验；本次修订没有新增模型或商品接口调用。' if m.get('revision') else '- 本次执行记录见manifest及audit-log。'),
        '', '## 实际执行轨迹','', '| 步骤 | 状态 |','| --- | --- |']
    names=['任务与规则','历史漏损','Query准入','改写与索引','当前证据','相关性与资格','审批建议','校验与报告']
    for i,name in enumerate(names,1):
        status=m['steps'].get(str(i),{}).get('status','pending')
        # Validation has passed; finalization is committed after this file is written.
        display='结构与引用已校验；最终状态见manifest' if i==8 else states.get(status,status)
        lines.append(f"| {i}. {name} | {display} |")
    lines+=['','## 历史证据','']
    if m['steps'].get('2',{}).get('status')=='skipped':
        lines.append('准入阶段已停止，本次未执行历史SQL；不把未执行解释为零漏损，也不要求为已拒绝任务补商业价值证据。')
    else:
        lines+=['| 指标 | 结果 |','| --- | ---: |']
        metrics={'request_count':'有效请求数','eligible_count':'已合格请求数','keyword_miss_count':'关键词缺口请求数',
            'supply_fail_count':'供给失败请求数','plan_fail_count':'计划失败请求数','unknown_count':'未知请求数',
            'verified_gap_count':'独立回放核验的潜在缺口请求数','complete':'数据是否完整'}
        for key,label in metrics.items():lines.append(f"| {label} | {safe(h.get(key))} |")
        lines+=['',f"行级证据保留 {h.get('evidence_rows_returned',0)} / {h.get('evidence_rows_total',0)} 条；是否截断：{h.get('output_truncated',False)}。顶层统计来自全窗口SQL，不由截断明细推算；明细中未找到关联只能暂缓，不能据此认定全量没有机会。"]
    lines+=['','历史回放只提供潜在承接证据，不代表可追回曝光、成交或收入。','',
        '## 改写审核','','| term | 硬约束后结论 |','| --- | --- |']
    for r in result.get('rewrites',[]):lines.append(f"| {safe(r.get('term_id'))} | {safe(r.get('decision',r.get('status','unknown')))} |")
    if not result.get('rewrites'):lines.append('| — | 本次未执行改写或无候选 |')
    lines+=['','## 逐候选审核','', '下表一行是一个计划级候选，不是一份独立供给；供给数量按 offer_id 去重。', '',
        '| term / candidate（计划） | offer_id（供给） | 相关性 | 当前资格 | 依据 |','| --- | --- | --- | --- | --- |']
    for item in result.get('items',[]):
        lines.append(f"| {safe(item.get('term_id'))} / {safe(item.get('candidate_id'))} | {safe(item.get('offer_id'))} | {safe(item.get('relevance'))} | {safe(item.get('eligibility'))} | {safe(item.get('reasons',item.get('reason_codes',[])))} |")
    if not result.get('items'):lines.append('| — | — | 未执行 | 未执行 | 没有进入商品审核分支 |')
    lines+=['','## 待复核事项','']
    queue=result.get('review_queue',[])
    for row in queue:
        lines.append('- '+safe(row.get('type'))+'：'+safe('；'.join(row.get('reasons',[]))))
        lines.append('  - 规则：'+safe(', '.join(row.get('rule_refs',[])))+'；证据：'+safe(', '.join(row.get('evidence_refs',[])) or '见对应阶段记录'))
    if not queue:lines.append('本次未列出额外复核事项；只有提交建议才进入人工审批，拒绝或暂缓不等于已获批准。')
    lines+=['','## 追溯','',
        '结构化结论见steps/，实际取数与响应见evidence/，规则版本及模型交接记录见manifest.json。',
        '当前资格只对应本次业务快照；不保证未来请求的实时可投状态。','']
    template=(SKILL_ROOT/'assets/review-report-template.md').read_text(encoding='utf-8')
    if '{{report_body}}' not in template:raise ValueError('Report template slot missing')
    path=run/'review-report.md';path.write_text(template.replace('{{report_body}}','\n'.join(lines)),encoding='utf-8')
    return path

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True);print(build(p.parse_args().run_dir))
