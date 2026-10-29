"""Actual isolated tmux screen/cursor fixtures for the AP footer matcher."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from frontend_contract import validate_frontend

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', type=Path, required=True)
p.add_argument('--binary', type=Path, required=True)
p.add_argument('--binary-source-commit', required=True, help='Source identity from the reviewed build receipt')
p.add_argument('--frontend-equivalence', type=Path, help='Root-reviewed receipt for known 3598-to-ecddea frontend reuse')
p.add_argument('--footer-contract',type=Path,required=True,help='Read-only reviewed footer contract identity')
p.add_argument('--evidence', type=Path, required=True)
p.add_argument('--setup-only',action='store_true')
p.add_argument('--baseline', action='store_true', help='OLD layout evidence only, never candidate acceptance')
p.add_argument('--development',action='store_true',help='Development pre-package probe, never final packaged acceptance')
a = p.parse_args()
a.source, a.binary, a.evidence = a.source.resolve(), a.binary.resolve(), a.evidence.resolve()
assert not a.baseline and not a.development and a.frontend_equivalence is None, 'candidate requires fresh final package captures'
a.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
sys.path[:0] = [str(a.source / 'tests'), str(a.source)]
from fixtures.native_server import AppServer, frame
from native_smoke import write_health, write_model_catalog
from test_ultracode_tui import helper_script

ROOT = '00000000-0000-0000-0000-000000000009'
SELECTION = ['model-with-reasoning','current-dir','compact-display','context-remaining','weekly-limit']
LOOK = '#{cursor_y} #{cursor_x} #{pane_height} #{pane_width} #{cursor_flag}'
TMUX = shutil.which('tmux')
FORBIDDEN = {'turn/start','turn/steer','thread/queue/add','thread/queue/start'}

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def source_identity():
    return {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.source,text=True).strip(),
            'patch_sha256':digest(a.source/CONFIG['patch_path'])}

class Server(AppServer):
    busy = False
    def __init__(self, home, work, effort):
        self.effort = effort
        socket = home/'app-server-control/app-server-control.sock';socket.parent.mkdir()
        assert len(os.fsencode(socket)) < 104, str(socket)
        super().__init__(ROOT,work,6,socket_path=socket);socket.chmod(0o600)
        self.shells = [self.shell(701,'cursor-native-shell')]
    def reply(self,method,params):
        result = super().reply(method,params)
        if method == 'config/read':
            result['result']['config'].update(model='gpt-6.1-sol',model_reasoning_effort=self.effort,
                                               tui={'theme':'morandi','status_line':SELECTION,'status_line_use_colors':True})
        if method in ('thread/start','thread/resume'):
            result['result'].update(model='gpt-6.1-sol',reasoningEffort=self.effort)
        return result
    def notify(self,method,params,request_id=None):
        message = {'method':method,'params':params}
        if request_id is not None:message['id']=request_id
        with self.lock:
            for connection in self.connections:
                connection.sendall(frame(json.dumps(message).encode()))
    def turn(self,busy):
        self.busy=busy
        self.notify('turn/started' if busy else 'turn/completed',{'threadId':ROOT,'turn':{
            'id':'cursor-busy','items':[],'status':'inProgress' if busy else 'completed','error':None}})

def seed(home,work,effort):
    catalog = write_model_catalog(home,astra=True)
    models = home/'fixture-models.json';value=json.loads(models.read_text())
    for model in value['models']:
        model['supported_reasoning_levels'].append({'effort':'ultra','description':'Ultra'})
    models.write_text(json.dumps(value))
    (home/'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="openai"\n'
        f'model_reasoning_effort="{effort}"\n'
        'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
        +catalog+'[tui]\ntheme="morandi"\nstatus_line_use_colors=true\nstatus_line='+json.dumps(SELECTION)+'\n'
        +f'[projects."{work}"]\ntrust_level="trusted"\n')
    (home/'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    write_health(home,ROOT,'idle')
    now=time.time()
    (home/'codex-plus'/f'{ROOT}.advisor.json').write_text(json.dumps(dict(
        version=1,parent=ROOT,model='gpt-6-astra',effort='xhigh',state='done',started_at=now-5,
        updated_at=now,finished_at=now,calls=1,last_duration_secs=5,error=None)))
    directory=home/'codex-plus/execution-status'/ROOT;directory.mkdir(parents=True)
    for i in range(2):
        uid=f'00000000-0000-4000-8000-00000000000{i}'
        (directory/f'{uid}.json').write_text(json.dumps(dict(schema=1,id=uid,root=ROOT,
            topic='cursor-detached-'+str(i),state='unknown' if i==0 else 'running',
            started_at=now-5,updated_at=now,error='fixture unresolved' if i==0 else None)))

manifest={'baseline_only':a.baseline,'target_release':CONFIG['release'],'capture_backend':'actual private tmux',
    'helper_metadata_projection':'private test_ultracode_tui.helper_script stub; geometry only, no runtime semantics claim',
    'fixture_tui':{'theme':'morandi','status_line':SELECTION,'status_line_use_colors':True},
    'bundle_version':2,'AP_policy':'cursor_flag=1 and positive bare composer on cursor row; whole-screen busy priority',
    'development_pre_package':a.development,'missing_states':[],
    'core_required':UPSTREAM['version'],'binary':str(a.binary),'binary_sha256':digest(a.binary),
    'binary_version':subprocess.check_output([str(a.binary),'--version'],text=True).strip(),
    'source_start':source_identity(),'reviewed_footer_contract':str(a.footer_contract.resolve()),
    'reviewed_footer_contract_sha256':digest(a.footer_contract),
    'captures':[],'violations':[],'model_calls':0,'input_submissions':0,'ap_matcher_modified':False}
manifest['binary_source_commit']=subprocess.check_output(['git','rev-parse',a.binary_source_commit],cwd=a.source,text=True).strip()
manifest['binary_source_patch_sha256']=hashlib.sha256(subprocess.check_output(['git','show',
    manifest['binary_source_commit']+':'+CONFIG['patch_path']],cwd=a.source)).hexdigest()
manifest['native_core_version']=re.search(r'\b(\d+\.\d+\.\d+)\b',manifest['binary_version'])[1]
assert TMUX and manifest['native_core_version']==UPSTREAM['version']
host=a.binary.with_name('codex-code-mode-host')
manifest['code_mode_host']={'path':str(host),'sha256':digest(host)} if host.exists() else None
if not a.baseline:
    receipt_bytes=a.frontend_equivalence.read_bytes() if a.frontend_equivalence else None
    package_manifest=a.binary.parent.parent/'codex-plus.json'
    package_bytes=package_manifest.read_bytes() if receipt_bytes is not None else None
    validate_frontend(manifest['binary_source_commit'],manifest['source_start']['commit'],
        manifest['binary_source_patch_sha256'],manifest['source_start']['patch_sha256'],
        manifest['binary_sha256'],manifest['code_mode_host']['sha256'] if host.exists() else None,
        receipt_bytes=receipt_bytes,manifest_bytes=package_bytes,architecture=platform.machine())
    if receipt_bytes is not None:
        (a.evidence/'frontend-equivalence.json').write_bytes(receipt_bytes)
        (a.evidence/'frontend-package-manifest.json').write_bytes(package_bytes)
        manifest['frontend_equivalence']={'path':'frontend-equivalence.json',
            'sha256':hashlib.sha256(receipt_bytes).hexdigest(),'architecture':platform.machine(),
            'package_manifest':{'path':'frontend-package-manifest.json','sha256':hashlib.sha256(package_bytes).hexdigest()}}
(a.evidence/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')

def run_fixture(effort,form=None):
    with tempfile.TemporaryDirectory(prefix='f-') as temporary:
        root=Path(temporary);home=root/'h';work=root/'w';package=root/'p'
        for path in (home,work,package):path.mkdir()
        seed(home,work,effort)
        shutil.copy2(a.binary,package/'codex')
        host=a.binary.with_name('codex-code-mode-host')
        if host.exists():(package/host.name).symlink_to(host)
        helper=package/'codex-plus-runtime';helper.write_text(helper_script((a.source/'docs/ultracode-bootstrap.txt').read_text()));helper.chmod(0o700)
        server=Server(home,work,effort)
        socket=root/'t.sock'
        clean={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE','STY')
               and not any(part in k.upper() for part in ('API_KEY','AUTH_TOKEN','BASE_URL'))}
        clean.update(CODEX_HOME=str(home),OPENAI_API_KEY='isolated-fixture-only',TERM='screen-256color',TERM_PROGRAM='tmux',LANG='C.UTF-8')
        def tmux(*arguments,check=True):
            return subprocess.run([TMUX,'-S',str(socket),'-f','/dev/null',*arguments],
                env=clean,capture_output=True,check=check,timeout=10).stdout
        def screen():return tmux('capture-pane','-p','-t',pane).decode()
        def wait(predicate,reason,seconds=20):
            until=time.monotonic()+seconds
            while time.monotonic()<until:
                text=screen()
                if predicate(text):return text
                time.sleep(.05)
            raise AssertionError(reason+'\n'+text)
        def keys(*items):tmux('send-keys','-t',pane,*items)
        def literal(text):tmux('send-keys','-l','-t',pane,text)
        def clear():
            keys('Escape','C-e')
            for _ in range(6):keys('C-u')
            time.sleep(.1)
        def capture(name,expected,*,full=True,dialog=False):
            folder=a.evidence/name;folder.mkdir()
            for attempt in range(20):
                before=tmux('display-message','-p','-t',pane,LOOK)
                at=datetime.now(timezone.utc).isoformat()
                raw=tmux('capture-pane','-p','-t',pane)
                after=tmux('display-message','-p','-t',pane,LOOK)
                if before==after:break
                time.sleep(.025)
            else:raise AssertionError('unstable cursor/dimensions: '+name)
            (folder/'screen.txt').write_bytes(raw)
            (folder/'look.txt').write_bytes(after)
            (folder/'look-before.txt').write_bytes(before)
            (folder/'look-after.txt').write_bytes(after)
            y,x,height,width,cursor_flag=map(int,after.split());rows=raw.decode().splitlines()
            assert len(rows)==height and 0<=y<height and 0<=x<=width,(name,after,len(rows))
            assert cursor_flag in (0,1)
            prompts=[i for i,line in enumerate(rows) if line.lstrip().startswith(('›','»'))]
            composer=max(prompts) if prompts and not dialog else None
            if expected=='overlay':expected='idle' if composer==y and cursor_flag==1 and x<width else 'unknown'
            if 'two-row-draft' in name:expected='unknown'
            text=raw.decode();below=rows[y+1:]
            checks={'coherent_cursor_dimensions':True,'expected_busy_marker':expected!='busy' or 'esc to interrupt' in text,
                'expected_idle_has_no_busy_marker':expected!='idle' or 'esc to interrupt' not in text,
                'positive_idle_cursor':expected!='idle' or (cursor_flag==1 and x<width and composer==y),
                'ultra_prompt':effort!='ultra' or any(line.lstrip().startswith('»') for line in rows)}
            known=re.compile(r'^(?:GPT-|gpt-|Background|Advisor|Workers|Context|\+\d+ more|Weekly|\d+% left)')
            impostors=[i for i,line in enumerate(rows[:y+1]) if re.match(r'^\s*[›»]\s*',line)
                       and known.match(re.sub(r'^\s*[›»]\s*','',line))]
            checks['no_status_or_HUD_prompt_at_or_above_cursor']=not impostors
            if full and expected!='unknown':
                compact_rows=[line for line in below if re.search(r'\bcompact\b',line)]
                checks.update(full_HUD_worker=any('worker' in line.lower() for line in below),
                    compact_first_status_row=len(compact_rows)==1 and compact_rows[0].lstrip().startswith('gpt-'),
                    full_HUD_native_shell=any('cursor-native-shell' in line for line in below),
                    full_HUD_detached=any('cursor-detached-' in line for line in below),
                    full_HUD_advisor=any(line.strip().startswith('Advisor ·') for line in below),
                    full_HUD_overflow=any(re.fullmatch(r'\+\d+ more',line.strip()) for line in below),
                    affected_unknown_job_visible=any(line.strip()=='Background · cursor-detached-0 · needs review · ?'
                        for line in below),
                    full_HUD_six_activity_rows=sum(line.strip().startswith(('GPT-','Background ·','Advisor ·'))
                        or bool(re.fullmatch(r'\+\d+ more',line.strip())) for line in below)==6,
                    aggregate_unavailable_banner_removed='Background status unavailable' not in text,
                    automatic_queue_hint_hidden='tab to queue message' not in '\n'.join(below),
                    footer_bounded=len([line for line in below if line.strip()])<=(9 if 'idle-help' in name else 8))
                shown=sum(line.strip().startswith(('GPT-','Background ·')) for line in below)
                checks['overflow_exact']=any(line.strip()==f'+{9-shown} more' for line in below)
            record={'name':name,'at':at,'expected':expected,'cursor_y':y,'cursor_x':x,'pane_height':height,
                'cursor_flag':cursor_flag,
                'pane_width':width,'composer_row':composer,'checks':checks,'impostor_rows':impostors,
                'cursor_within_visible_grid':x<width,
                'capture_argv':[TMUX,'-S',str(socket),'-f','/dev/null','capture-pane','-p','-t',pane],
                'look_argv':[TMUX,'-S',str(socket),'-f','/dev/null','display-message','-p','-t',pane,LOOK],
                'screen_sha256':hashlib.sha256(raw).hexdigest(),'look_sha256':hashlib.sha256(after).hexdigest()}
            for label,passed in checks.items():
                if not passed:manifest['violations'].append(name+': '+label)
            manifest['captures'].append(record)
            (folder/'capture.json').write_text(json.dumps(record,indent=2)+'\n')
            return record,rows
        pane=None;other=None
        try:
            command=shlex.join([str(package/'codex'),'--yolo','-C',str(work)])
            pane=tmux('new-session','-d','-P','-F','#{pane_id}','-x','100','-y','40','-s','fixture',command).decode().strip()
            tmux('set-option','-g','status','off');tmux('resize-window','-t','fixture:0','-x','100','-y','40')
            wait(lambda s:any(line.lstrip().startswith(('›','»')) for line in s.splitlines()) and 'Advisor' in s,
                 'private native footer did not settle',30)
            time.sleep(.4)
            if form:
                if form=='request-user-input-notes':
                    method='item/tool/requestUserInput'
                    params={'threadId':ROOT,'turnId':'cursor-form','itemId':'notes-form','isBlocking':True,
                        'questions':[{'id':'notes','header':'Native notes','question':'Choose the fixture option and add notes.',
                            'isOther':False,'isSecret':False,'options':[{'label':'Alpha','description':'First fixture option'},
                                                                     {'label':'Beta','description':'Second fixture option'}]}]}
                    marker='Add notes'
                else:
                    method='mcpServer/elicitation/request'
                    params={'threadId':ROOT,'turnId':None,'serverName':'cursor-fixture','mode':'form',
                        'message':'Native MCP text elicitation fixture','requestedSchema':{'type':'object',
                            'properties':{'answer':{'type':'string','title':'Fixture answer'}},'required':['answer']}}
                    marker='Type your answer'
                (a.evidence/(form+'-server-request.json')).write_text(json.dumps({'id':472,'method':method,'params':params},indent=2)+'\n')
                server.notify(method,params,request_id=472)
                if form=='request-user-input-notes':
                    wait(lambda s:'Choose the fixture option' in s,'native user-input options did not appear')
                    time.sleep(.2)
                    (a.evidence/'notes-options-screen.txt').write_bytes(tmux('capture-pane','-p','-t',pane))
                    keys('Tab')
                wait(lambda s:any(line.lstrip().startswith('› '+marker) for line in s.splitlines()),
                     'native '+form+' prompt did not appear')
                for size in ('100x40','80-split'):
                    if size=='80-split':
                        tmux('resize-window','-t','fixture:0','-x','161','-y','40')
                        other=tmux('split-window','-h','-d','-l','80','-P','-F','#{pane_id}','-t',pane,'/bin/sleep 180').decode().strip()
                        time.sleep(.4)
                    wait(lambda s:any(line.lstrip().startswith('› '+marker) for line in s.splitlines()),'form missing after resize')
                    capture('form-'+size+'-'+form,'unknown',full=False,dialog=True)
                assert not any(request.get('method') in FORBIDDEN for request in server.requests)
                assert not server.errors,server.errors
                return
            sizes=['100x40','80-split']
            for size in sizes:
                if size=='80-split':
                    tmux('resize-window','-t','fixture:0','-x','161','-y','40')
                    other=tmux('split-window','-h','-d','-l','80','-P','-F','#{pane_id}','-t',pane,'/bin/sleep 180').decode().strip()
                    time.sleep(.4)
                prefix=effort+'-'+size+'-'
                clear();time.sleep(.2)
                capture(prefix+'idle-empty','idle')
                if effort=='ultra':continue
                literal('?');wait(lambda s:'Keyboard shortcuts' in s,'explicit help did not appear')
                capture(prefix+'idle-help','overlay')
                clear();server.turn(True)
                wait(lambda s:'esc to interrupt' in s,'busy notification not shown')
                first,rows1=capture(prefix+'busy-midturn-t0','busy')
                time.sleep(5)
                second,rows2=capture(prefix+'busy-midturn-t5','busy')
                changed=[]
                for i,(old,new) in enumerate(zip(rows1,rows2)):
                    if old!=new:changed.append({'row':i,'before':old,'after':new,
                        'relative_before':'above' if i<first['cursor_y'] else 'at' if i==first['cursor_y'] else 'below',
                        'relative_after':'above' if i<second['cursor_y'] else 'at' if i==second['cursor_y'] else 'below',
                        'working_or_interrupt':'Working' in old+new or 'esc to interrupt' in old+new})
                (a.evidence/(prefix+'busy-changes.json')).write_text(json.dumps({'before_at':first['at'],'after_at':second['at'],
                    'cursor_before':[first['cursor_y'],first['cursor_x']],'cursor_after':[second['cursor_y'],second['cursor_x']],
                    'changed_rows':changed},indent=2)+'\n')
                literal('cursor-fixture-busy-draft');wait(lambda s:'cursor-fixture-busy-draft' in s,'busy draft unavailable')
                capture(prefix+'busy-typed','busy')
                server.turn(False);wait(lambda s:'esc to interrupt' not in s,'busy marker did not clear')
                clear();literal('\x1b[200~cursor-draft-first\ncursor-draft-second\x1b[201~')
                wait(lambda s:'cursor-draft-first' in s and 'cursor-draft-second' in s,'two-row draft unavailable')
                time.sleep(.5);capture(prefix+'idle-two-row-draft','idle')
            if effort=='high':
                clear();server.turn(True);wait(lambda s:'esc to interrupt' in s,'busy before approval absent')
                server.notify('item/commandExecution/requestApproval',{'threadId':ROOT,'turnId':'cursor-busy','itemId':'approval-item',
                    'startedAtMs':int(time.time()*1000),'command':'echo cursor-fixture-approval','cwd':str(work),
                    'reason':'Private fixture approval dialog only; command never runs.'},request_id=471)
                wait(lambda s:'cursor-fixture-approval' in s and ('approve' in s.lower() or 'proceed' in s.lower()),
                     'private approval dialog not shown')
                capture('high-80-split-approval-dialog','unknown',full=False,dialog=True)
                tmux('kill-pane','-t',other);other=None
                tmux('resize-window','-t','fixture:0','-x','100','-y','40');time.sleep(.3)
                capture('high-100x40-approval-dialog','unknown',full=False,dialog=True)
            assert not any(request.get('method') in FORBIDDEN for request in server.requests),server.requests
            assert not server.errors,server.errors
        finally:
            tmux('kill-server',check=False)
            server.close()
            label=form or effort
            (a.evidence/(label+'-rpc.json')).write_text(json.dumps(server.requests,indent=2)+'\n')
            (a.evidence/(label+'-fixture-errors.json')).write_text(json.dumps(server.errors)+'\n')

if a.setup_only:
    with tempfile.TemporaryDirectory(prefix='f-') as temporary:
        root=Path(temporary);home=root/'h';work=root/'w';home.mkdir();work.mkdir()
        server=Server(home,work,'high')
        try:
            socket=home/'app-server-control/app-server-control.sock'
            assert socket.is_socket() and not server.requests
            report=dict(passed=True,setup_only=True,socket=str(socket),socket_bytes=len(os.fsencode(socket)),
                        source=manifest['source_start'],binary_sha256=manifest['binary_sha256'],model_calls=0)
        finally:server.close()
    assert not root.exists()
    (a.evidence/'setup.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report));raise SystemExit(0)

try:
    run_fixture('high');run_fixture('ultra')
    for form in ('request-user-input-notes','mcp-text-elicitation'):
        try:run_fixture('high',form=form)
        except Exception as error:manifest['missing_states'].append({'form':form,'reason':str(error)})
    manifest['source_end']=source_identity()
    manifest['fixture_source_stable']=manifest['source_start']==manifest['source_end']
    if not a.baseline:assert manifest['fixture_source_stable'],'fixture source identity changed during captures'
    manifest['capture_complete']=not manifest['missing_states'] and len(manifest['captures'])==20
    manifest['binary_end_sha256']=digest(a.binary)
    assert manifest['binary_end_sha256']==manifest['binary_sha256']
    assert digest(a.footer_contract)==manifest['reviewed_footer_contract_sha256'],'reviewed footer contract changed'
    if manifest.get('frontend_equivalence'):
        assert digest(a.frontend_equivalence)==manifest['frontend_equivalence']['sha256'],'frontend receipt changed'
        assert digest(package_manifest)==manifest['frontend_equivalence']['package_manifest']['sha256'],'package manifest changed'
        assert digest(host)==manifest['code_mode_host']['sha256'],'Code Mode host changed'
    manifest['passed']=not manifest['violations'] and manifest['capture_complete']
    manifest['candidate_acceptance']=manifest['passed'] and not a.baseline and not a.development
except BaseException as error:
    manifest['failure']=str(error)
    raise
finally:
    (a.evidence/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'captures':len(manifest['captures']),'baseline_only':a.baseline,
        'violations':manifest['violations'],'candidate_acceptance':manifest.get('candidate_acceptance',False),
        'missing_states':manifest['missing_states'],
        'manifest':str(a.evidence/'manifest.json')},indent=2))
if not a.baseline:assert manifest['passed'],{'violations':manifest['violations'],'missing_states':manifest['missing_states']}
