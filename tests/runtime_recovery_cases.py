import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import agent_runtime as runtime


class ProcessRecoveryTests(unittest.TestCase):
    def test_new_request_and_resumed_thread_receive_the_current_profile(self):
        from contextlib import contextmanager
        @contextmanager
        def gateway(*args):yield 1234,'nonce'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'review').mkdir();(root/'review/key').write_text('test-key')
            (root/'agent').mkdir();(root/'agent/config.json').write_text(json.dumps({'profile_file':'AGENTS.md'}))
            profile=root/'agent/AGENTS.md';profile.write_text('Use short, complete sentences.')
            claim={'binding':['T','D','1'],'history':[],'partial':False,'targets':[],'local':False}
            operations=SimpleNamespace(counts={},receipts=[],services=lambda:[])
            payloads=[];commands=[]
            def model(command,**kwargs):
                payloads.append(kwargs['input']);commands.append(command)
                Path(command[command.index('-o')+1]).write_text('Checked the current sources.')
                kwargs['stdout'].write(json.dumps({'type':'thread.started','thread_id':'same-session'})+'\n')
                kwargs['stdout'].flush()
                return SimpleNamespace(returncode=0)
            def home(directory,*args):
                path=directory/'home';path.mkdir(exist_ok=True);return path
            with patch.object(runtime.review_evidence,'PRIVATE',root),patch.object(runtime,'ROOT',root/'runs'), \
                 patch.object(runtime.slack_intent,'verified',return_value=claim),patch.object(runtime,'AgentTools',return_value=operations), \
                 patch.object(runtime,'prepare_assignments',return_value={}),patch.object(runtime,'running_gateway',gateway), \
                 patch.object(runtime,'prepare_home',home),patch.object(runtime,'run_model',model),patch.object(runtime,'envelope',side_effect=lambda result,*args:result):
                runtime.execute({'ticket':'first','event':{'text':'Check this PR.'}})
                profile.write_text('Lead with a plain-language conclusion.')
                runtime.execute({'ticket':'second','event':{'text':'What changed?'}})
            self.assertIn('Use short, complete sentences.',payloads[0])
            self.assertIn('Lead with a plain-language conclusion.',payloads[1])
            self.assertNotIn('Use short, complete sentences.',payloads[1])
            self.assertIn('resume',commands[1])
            outcome=json.loads(next((root/'runs').glob('*/last-execution.json')).read_text())
            self.assertEqual(outcome['profile_sha256'],runtime.load_profile(root)['sha256'])

    def test_outer_timeout_preserves_runtime_cleanup_reply(self):
        import slack_intent
        code="""import signal,time,json,sys
def stop(*args):
 print(json.dumps({'reply':'signed recovery receipt'}),flush=True)
 sys.exit(0)
signal.signal(signal.SIGTERM,stop)
time.sleep(60)
"""
        result=slack_intent.run_native([sys.executable,'-c',code],'{}',timeout=.2,grace=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout)['reply'],'signed recovery receipt')

    def test_outer_timeout_kills_unresponsive_runtime_and_descendants(self):
        import slack_intent
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            code="""import pathlib,subprocess,sys,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
pathlib.Path(PID).write_text(str(child.pid))
time.sleep(60)
""".replace('PID',repr(str(root/'child.pid')))
            unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
            try:
                result=slack_intent.run_native([sys.executable,'-c',code],'{}',timeout=.3,grace=.1)
                self.assertTrue(result.timed_out)
                self.assertEqual(result.returncode,-signal.SIGKILL)
                self.assert_child_stopped(root)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate();unrelated.wait()

    def model(self,root,complete):
        script=root/'model.py'
        script.write_text('''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
pathlib.Path('child.pid').write_text(str(child.pid))
print(json.dumps({'type':'thread.started','thread_id':'saved-session'}),flush=True)
if COMPLETE:
 pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('verified result')
 print(json.dumps({'type':'turn.completed'}),flush=True)
time.sleep(60)
'''.replace('COMPLETE',repr(complete)))
        return [sys.executable,str(script),'-o',str(root/'final')]

    def assert_child_stopped(self,root):
        pid=int((root/'child.pid').read_text())
        for _ in range(40):
            state=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
            if not state or state.startswith('Z'):return
            time.sleep(.05)
        self.fail('model descendant survived cleanup')

    def test_completed_turn_does_not_wait_for_stuck_cli_background_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);command=self.model(root,True)
            with (root/'events').open('w') as out,(root/'errors').open('w') as err:
                started=time.monotonic()
                result=runtime.run_model(command,input='',text=True,stdout=out,stderr=err,cwd=root,env={},timeout=4)
            self.assertEqual(result.returncode,0)
            self.assertLess(time.monotonic()-started,3)
            self.assert_child_stopped(root)

    def test_timeout_terminates_descendants_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);command=self.model(root,False)
            with (root/'events').open('w') as out,(root/'errors').open('w') as err:
                with self.assertRaises(subprocess.TimeoutExpired):
                    runtime.run_model(command,input='',text=True,stdout=out,stderr=err,cwd=root,env={},timeout=.3)
            self.assert_child_stopped(root)

    def test_final_file_without_completed_turn_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            command=[sys.executable,'-c',"import pathlib,sys;pathlib.Path(sys.argv[2]).write_text('partial')",'-o',str(root/'final')]
            with (root/'events').open('w') as out,(root/'errors').open('w') as err:
                with self.assertRaisesRegex(RuntimeError,'without a completed turn'):
                    runtime.run_model(command,input='',text=True,stdout=out,stderr=err,cwd=root,env={},timeout=2)

    def test_sigterm_records_interruption_and_terminates_model_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);command=self.model(root,False)
            driver=root/'driver.py'
            driver.write_text('''import json,pathlib,sys
sys.path.insert(0,SCRIPTS)
import agent_runtime as runtime
root=pathlib.Path(ROOT)
try:
 with runtime.interruption_handlers(),(root/'events').open('w') as out,(root/'errors').open('w') as err:
  runtime.run_model(COMMAND,input='',text=True,stdout=out,stderr=err,cwd=root,env={},timeout=30)
except runtime.RuntimeInterrupted as e:
 runtime.save_execution(root/'outcome',{'status':'interrupted','reason':runtime.execution_failure(e)})
'''.replace('SCRIPTS',repr(str(Path(runtime.__file__).parent))).replace('ROOT',repr(str(root))).replace('COMMAND',repr(command)))
            process=subprocess.Popen([sys.executable,str(driver)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if (root/'child.pid').exists():break
                    time.sleep(.03)
                self.assertTrue((root/'child.pid').exists())
                process.send_signal(signal.SIGTERM)
                _,err=process.communicate(timeout=5)
                self.assertEqual(process.returncode,0,err)
                outcome=json.loads((root/'outcome').read_text())
                self.assertEqual(outcome['status'],'interrupted')
                self.assertIn('15',outcome['reason'])
                self.assert_child_stopped(root)
            finally:
                if process.poll() is None: process.kill();process.wait()
                if process.stderr:process.stderr.close()


class GatewayRecoveryTests(unittest.TestCase):
    def test_artifact_images_reach_mcp_client_as_images_and_text_stays_text(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client,create_mcp_http_client
        from mcp.types import ImageContent,TextContent
        encoded='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aYioAAAAASUVORK5CYII='
        image={'ok':True,'path':'flowGraphImage.png','image':{'mimeType':'image/png','data':encoded}}
        plain={'ok':True,'text':'query source','next_offset':None}
        operations=SimpleNamespace(claim={},artifact=Mock(side_effect=[image,plain]))
        async def read(port,nonce):
            async with create_mcp_http_client(headers={'Authorization':'Bearer '+nonce}) as http:
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http) as streams:
                    async with ClientSession(streams[0],streams[1]) as client:
                        await client.initialize()
                        picture=await client.call_tool('review_artifact',{'target':'team/repo/pull/1','path':'flowGraphImage.png'})
                        text=await client.call_tool('review_artifact',{'target':'team/repo/pull/1','path':'source.sql'})
                        return picture,text
        with tempfile.TemporaryDirectory() as folder:
            with runtime.running_gateway(operations,Path(folder)/'audit') as (port,nonce):
                picture,text=asyncio.run(read(port,nonce))
        self.assertFalse(picture.is_error)
        self.assertIsInstance(picture.content[0],TextContent)
        self.assertNotIn(encoded,picture.content[0].text)
        self.assertEqual(json.loads(picture.content[0].text)['path'],'flowGraphImage.png')
        self.assertIsInstance(picture.content[1],ImageContent)
        self.assertEqual(picture.content[1].mime_type,'image/png')
        self.assertEqual(picture.content[1].data,encoded)
        self.assertEqual(len(text.content),1)
        self.assertIsInstance(text.content[0],TextContent)
        self.assertEqual(json.loads(text.content[0].text),plain)

    def test_initialization_failure_persists_interruption_and_replays_signed_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'review').mkdir();(root/'review/key').write_text('test-key')
            claim={'binding':['T','D','1'],'history':[],'partial':False,'targets':[]}
            with patch.object(runtime.review_evidence,'PRIVATE',root),patch.object(runtime,'ROOT',root/'runs'), \
                 patch.object(runtime.slack_intent,'verified',return_value=claim), \
                 patch.object(runtime,'AgentTools',side_effect=RuntimeError('initialization failed')) as initialize:
                args={'ticket':'test-ticket','event':{'text':'continue'}}
                result=runtime.execute(args)
                self.assertEqual(result,runtime.execute(args))
                self.assertEqual(initialize.call_count,1)
            self.assertTrue(result['reply'].startswith('relay-agent/1 '))
            outcome=json.loads(next((root/'runs').glob('*/last-execution.json')).read_text())
            self.assertEqual(outcome['status'],'interrupted')
            self.assertEqual(outcome['phase'],'preparing')
            self.assertIn('RuntimeError',outcome['reason'])
            self.assertIn('finished',outcome)

    def call(self,operations,name,args):
        with tempfile.TemporaryDirectory() as folder:
            server=runtime.build_server(operations,Path(folder)/'audit')
            return asyncio.run(server.call_tool(name,args)).model_dump(mode='json',by_alias=True)

    def test_transient_discovery_failure_retries_once_and_preserves_leaf_error(self):
        operations=SimpleNamespace(claim={},discover=AsyncMock(side_effect=[ExceptionGroup('transport',[ConnectionResetError('connection reset')]),[]]))
        result=self.call(operations,'discover_tools',{'service':'fixture'})
        self.assertEqual(operations.discover.call_count,2)
        self.assertFalse(result.get('isError',False))
        error=runtime.tool_failure(ExceptionGroup('transport',[ConnectionResetError('connection reset')]))
        self.assertIn('ConnectionResetError',error['error'])
        self.assertTrue(error['retryable'])

    def test_repository_knowledge_is_available_to_colleague_requests(self):
        operations=SimpleNamespace(claim={'local':False},knowledge=Mock(return_value={'commit':'a'*40,'documents':[]}))
        result=self.call(operations,'repository_knowledge',{'repo':'team/repo','ref':'a'*40,'paths':['docs/joins.md']})
        self.assertNotIn('Error executing tool',json.dumps(result))
        operations.knowledge.assert_called_once_with('team/repo','a'*40,['docs/joins.md'])

    def test_permission_rejection_is_not_retried_and_writes_are_never_retried(self):
        operations=SimpleNamespace(claim={},discover=AsyncMock(side_effect=PermissionError('access denied')),
                                   submit=Mock(side_effect=TimeoutError('receipt unknown')))
        self.call(operations,'discover_tools',{'service':'fixture'})
        self.assertEqual(operations.discover.call_count,1)
        result=self.call(operations,'submit_review',{'target':'team/repo/pull/1','head':'abc','decision':'APPROVE','summary':'s','details':'d'})
        self.assertEqual(operations.submit.call_count,1)
        self.assertIn('receipt unknown',json.dumps(result))

    def test_failures_do_not_echo_tokens(self):
        value=runtime.tool_failure(TimeoutError('Authorization=Bearer test-secret token=another-secret xoxp-token-secret'))
        self.assertNotIn('test-secret',value['error'])
        self.assertNotIn('another-secret',value['error'])
        self.assertNotIn('xoxp-token-secret',value['error'])

    def test_gateway_stops_after_setup_exception(self):
        operations=SimpleNamespace(claim={})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError,'setup failure'):
                with runtime.running_gateway(operations,Path(folder)/'audit') as (port,_):
                    raise RuntimeError('setup failure')
            import socket
            connection=socket.socket();connection.settimeout(.2)
            try:self.assertNotEqual(connection.connect_ex(('127.0.0.1',port)),0)
            finally:connection.close()


if __name__=='__main__':unittest.main()
