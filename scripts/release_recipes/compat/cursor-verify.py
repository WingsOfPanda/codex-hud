"""Verify raw tmux capture integrity and expected native states; no AP matcher edits."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from frontend_contract import validate_frontend

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--evidence',type=Path,required=True)
a=p.parse_args();m=json.loads((a.evidence/'manifest.json').read_text())
assert m.get('capture_complete') and 'failure' not in m
assert m['native_core_version']==UPSTREAM['version']
assert m['fixture_tui']=={'theme':'morandi','status_line_use_colors':True,
    'status_line':['model-with-reasoning','current-dir','compact-display','context-remaining','weekly-limit']}
if not m['baseline_only']:assert m['source_start']==m['source_end']
receipt=m.get('frontend_equivalence')
receipt_bytes=package_bytes=None
if receipt:
    assert receipt['path']=='frontend-equivalence.json'
    assert receipt['package_manifest']['path']=='frontend-package-manifest.json'
    receipt_bytes=(a.evidence/receipt['path']).read_bytes()
    package_bytes=(a.evidence/receipt['package_manifest']['path']).read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest()==receipt['sha256']
    assert hashlib.sha256(package_bytes).hexdigest()==receipt['package_manifest']['sha256']
if not m['baseline_only']:
    validate_frontend(m['binary_source_commit'],m['source_start']['commit'],
        m['binary_source_patch_sha256'],m['source_start']['patch_sha256'],
        m['binary_sha256'],m['code_mode_host']['sha256'] if m['code_mode_host'] else None,
        receipt_bytes=receipt_bytes,manifest_bytes=package_bytes,
        architecture=receipt['architecture'] if receipt else None)
assert m['binary_sha256']==m['binary_end_sha256']
assert m['bundle_version']==2
assert len(m['captures'])==20
expected={f'high-{size}-{state}' for size in ('100x40','80-split') for state in (
    'idle-empty','idle-help','busy-midturn-t0','busy-midturn-t5','busy-typed','idle-two-row-draft','approval-dialog')}
expected|={f'ultra-{size}-idle-empty' for size in ('100x40','80-split')}
expected|={f'form-{size}-{form}' for size in ('100x40','80-split') for form in ('request-user-input-notes','mcp-text-elicitation')}
assert {r['name'] for r in m['captures']}==expected
for record in m['captures']:
    root=a.evidence/record['name'];screen=(root/'screen.txt').read_bytes();look=(root/'look.txt').read_bytes()
    assert b'\x1b' not in screen,'ANSI style escapes in plain capture'
    assert re.fullmatch(rb'\d+ \d+ \d+ \d+ [01]\n',look)
    assert look==(root/'look-before.txt').read_bytes()==(root/'look-after.txt').read_bytes()
    assert hashlib.sha256(screen).hexdigest()==record['screen_sha256']
    assert hashlib.sha256(look).hexdigest()==record['look_sha256']
    assert record['capture_argv'][-4:]==['capture-pane','-p','-t',record['capture_argv'][-1]]
    assert '-e' not in record['capture_argv'] and '-J' not in record['capture_argv']
    assert record['look_argv'][-1]=='#{cursor_y} #{cursor_x} #{pane_height} #{pane_width} #{cursor_flag}'
    y,x,height,width,cursor_flag=map(int,look.split());rows=screen.decode().splitlines()
    assert len(rows)==height==40 and width==(80 if '80-split' in record['name'] else 100)
    assert [y,x,height,width]==[record[k] for k in ('cursor_y','cursor_x','pane_height','pane_width')]
    assert cursor_flag==record['cursor_flag']
    assert record['checks']['coherent_cursor_dimensions'] and not record['impostor_rows']
    if record['expected']=='busy':assert 'esc to interrupt' in screen.decode()
    if 'esc to interrupt' in screen.decode():assert record['expected']=='busy'
    if record['expected']=='idle':
        assert 'esc to interrupt' not in screen.decode() and cursor_flag==1 and x<width
        assert re.fullmatch(r'\s*[›»](?:\s+Ask Codex to do anything)?\s*',rows[y]),rows[y]
    if 'ultra-' in record['name']:assert any(line.lstrip().startswith('»') for line in rows)
    if 'two-row-draft' in record['name']:
        assert 'cursor-draft-first' in rows[y-1] and 'cursor-draft-second' in rows[y]
        assert record['expected']=='unknown'
    if record['name'].startswith('form-'):
        marker='Add notes' if record['name'].endswith('notes') else 'Type your answer'
        assert rows[y].lstrip().startswith('› '+marker),rows[y]
        assert cursor_flag==1 and x<width and record['expected']=='unknown'
        assert 'esc to interrupt' not in screen.decode()
    if 'approval-dialog' in record['name']:assert record['expected']=='unknown' and record['composer_row'] is None
    if not m['baseline_only']:assert all(record['checks'].values()),record
working_positions=[]
for size in ('100x40','80-split'):
    d=json.loads((a.evidence/f'high-{size}-busy-changes.json').read_text())
    elapsed=(datetime.fromisoformat(d['after_at'])-datetime.fromisoformat(d['before_at'])).total_seconds()
    assert 4.5<=elapsed<=15,elapsed
    before=(a.evidence/f'high-{size}-busy-midturn-t0/screen.txt').read_text().splitlines()
    after=(a.evidence/f'high-{size}-busy-midturn-t5/screen.txt').read_text().splitlines()
    assert [i for i,(left,right) in enumerate(zip(before,after)) if left!=right]==[r['row'] for r in d['changed_rows']]
    working=[r for r in d['changed_rows'] if r['working_or_interrupt']]
    assert working,d
    working_positions.extend({'size':size,'row':r['row'],'position':r['relative_before']} for r in working)
for effort in ('high','ultra','request-user-input-notes','mcp-text-elicitation'):
    requests=json.loads((a.evidence/f'{effort}-rpc.json').read_text())
    assert not any(r.get('method') in ('turn/start','turn/steer','thread/queue/add','thread/queue/start') for r in requests)
    assert not json.loads((a.evidence/f'{effort}-fixture-errors.json').read_text())
if not m['baseline_only']:assert m['passed'] and not m['violations']
if m['development_pre_package']:assert not m['candidate_acceptance']
report={'passed':True,'raw_tmux_pairs':20,'baseline_only':m['baseline_only'],
    'development_pre_package':m['development_pre_package'],
    'candidate_acceptance':m['candidate_acceptance'],'coherence':'cursor/dimensions before and after each pair',
    'busy_pairs_seconds':5,'busy_working_row_verified_above_cursor':all(r['position']=='above' for r in working_positions),
    'working_row_positions':working_positions,
    'fixture_tui':m['fixture_tui'],
    'model_calls':0,'AP_replay_performed':False}
if receipt:report['frontend_equivalence']=receipt
(a.evidence/'integrity.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
