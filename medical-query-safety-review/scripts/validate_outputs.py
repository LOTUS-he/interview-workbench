import argparse
import json
from pathlib import Path
from runtime_io import read_json,evidence_value,sha,source_hashes,SKILL_ROOT

NAMES=['00_input','10_query_profile','20_rewrite_check','30_term_item_check','40_final_review','50_decision_report']
def validate(run_dir):
    root=Path(run_dir).resolve();errors=[]
    from validate_resources import load_rule_ids
    known_rules=load_rule_ids()
    try:m=read_json(root/'manifest.json')
    except Exception as exc:return ['manifest: '+str(exc)]
    if m.get('source_hashes')!=source_hashes():errors.append('Skill source version changed; use matching source or rerun')
    objects={}
    for name in NAMES:
        try:
            obj=read_json(root/'steps'/(name+'.json'));objects[name]=obj
            if obj.get('run_id')!=m.get('run_id'):errors.append(name+': run mismatch')
            if obj.get('schema_version')!='1.1' or obj.get('step_id')!=name[:2]:errors.append(name+': schema/step mismatch')
            if obj.get('status') not in ['success','partial','blocked','skipped']:errors.append(name+': invalid execution status')
            if not isinstance(obj.get('data'),dict):errors.append(name+': data must be object')
            def walk(value):
                if isinstance(value,dict):
                    if not set(value.get('rule_refs',[]))<=known_rules:errors.append(name+': unknown rule reference')
                    for ref in value.get('evidence_refs',[]):
                        try:evidence_value(root,ref)
                        except Exception:errors.append(name+': missing or invalid evidence '+str(ref))
                    for child in value.values():walk(child)
                elif isinstance(value,list):
                    for child in value:walk(child)
            walk(obj)
        except Exception as exc:errors.append(name+': '+str(exc))
    expected_evidence={'evidence/'+name+'.json' for name in ['task','catalog','config-public','terminology','history','candidates']}
    actual_evidence={'evidence/'+p.name for p in (root/'evidence').glob('*.json') if p.is_file() and not p.is_symlink()}
    if actual_evidence!=expected_evidence or set(m.get('evidence_hashes',{}))!=expected_evidence:
        errors.append('Evidence inventory/hash coverage differs from contract')
    for name in expected_evidence:
        try:
            if sha(root/name)!=m.get('evidence_hashes',{}).get(name):errors.append('Evidence hash mismatch '+name)
        except OSError:errors.append('Missing evidence '+name)
    expected_decisions={'query-decision.json','item-decision.json'}
    if set(m.get('decision_hashes',{}))!=expected_decisions:errors.append('Decision inventory/hash coverage differs from contract')
    for name in expected_decisions:
        try:
            if (root/name).is_symlink() or sha(root/name)!=m.get('decision_hashes',{}).get(name):errors.append('Decision hash mismatch '+name)
        except OSError:errors.append('Missing accepted decision '+name)
    if m.get('phase')=='complete':
        expected_artifacts={'steps/'+n+'.json' for n in NAMES}|{'review-report.md'}
        if set(m.get('artifact_hashes',{}))!=expected_artifacts:errors.append('Artifact inventory/hash coverage differs from contract')
        for name in expected_artifacts:
            try:
                if (root/name).is_symlink() or sha(root/name)!=m.get('artifact_hashes',{}).get(name):errors.append('Artifact hash mismatch '+name)
            except OSError:errors.append('Missing artifact '+name)
    if len(objects)!=6:return errors
    final=objects['40_final_review']['data'];report=objects['50_decision_report']['data']
    if final.get('recommendation')!=report.get('recommendation'):errors.append('Report decision diverges')
    if report.get('published') is not False:errors.append('Publishing is not allowed')
    risk=objects['10_query_profile']['data'].get('risk')
    if risk!='L1' and final.get('recommendation')=='submit_for_human_review':errors.append('Risk gating bypass')
    pairs=set()
    try:evidence=read_json(root/'evidence/candidates.json')
    except Exception as exc:return errors+['Candidate evidence unreadable: '+str(exc)]
    allowed={(x['term_id'],x['candidate_id']) for x in evidence.get('candidates',[])}
    for item in final.get('items',[]):
        pair=(item.get('term_id'),item.get('candidate_id'))
        if pair in pairs or pair not in allowed:errors.append('Invalid or duplicate candidate reference')
        pairs.add(pair)
    if pairs!=allowed:errors.append('Missing candidate checks')
    if final.get('recommendation')=='submit_for_human_review':
        if not any(x.get('relevance')=='pass' and x.get('eligibility')=='pass' for x in final.get('items',[])):errors.append('Submission without confirmed supply')
        hist=read_json(root/'evidence/history.json')
        if not hist.get('complete') or not hist.get('verified_gap_count'):errors.append('Submission without verified history')
    # Artifact list must point to this run, not arbitrary filesystem paths.
    expected=['steps/'+n+'.json' for n in NAMES[:-1]]
    if report.get('artifact_index')!=expected:errors.append('Artifact index differs from contract')
    try:
        from check_constraints import check
        task=read_json(root/'evidence/task.json');history=read_json(root/'evidence/history.json')
        query=read_json(root/'query-decision.json');items=read_json(root/'item-decision.json')
        for decision in [query,items]:
            if decision.get('run_id')!=m.get('run_id'):errors.append('Accepted decision run mismatch')
        if query.get('phase')!='query' or items.get('phase')!='items':errors.append('Accepted decision phase mismatch')
        calculated=check(task,query,items,evidence,read_json(SKILL_ROOT/'references/eligibility-rules.yaml'),history=history)
        if calculated!=final:errors.append('Final review differs from deterministic recomputation')
        input_data=objects['00_input']['data']
        expected_history_ref={'path':'evidence/history.json','sha256':sha(root/'evidence/history.json')}
        if input_data.get('task')!=task or input_data.get('history_ref')!=expected_history_ref or 'history' in input_data:errors.append('Input artifact differs from evidence reference')
        if objects['10_query_profile']['data']!=query['profile']:errors.append('Profile differs from accepted decision')
        if objects['20_rewrite_check']['data'].get('rewrites')!=calculated['rewrites']:errors.append('Rewrite artifact diverges')
        if objects['20_rewrite_check']['data'].get('model_rewrites')!=query['rewrites']:errors.append('Model rewrite artifact diverges')
        if objects['30_term_item_check']['data'].get('items')!=calculated['items']:errors.append('Item artifact diverges')
        if report.get('review_queue')!=calculated['review_queue']:errors.append('Report review queue diverges')
    except Exception as exc:errors.append('Cannot recompute deterministic review: '+str(exc))
    return errors

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True);a=p.parse_args()
    errors=validate(a.run_dir);print(json.dumps({'valid':not errors,'errors':errors},ensure_ascii=False))
    raise SystemExit(1 if errors else 0)
