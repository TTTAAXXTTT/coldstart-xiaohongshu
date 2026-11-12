"""Clean edition: isolated subprocesses check real defaults without test fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent


class CleanStartTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows startup shortcut integration')
    def test_startup_shortcut_has_process_policy_and_removal_is_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            startup=Path(directory)/'startup';startup.mkdir()
            unrelated=startup/'unrelated.txt';unrelated.write_text('fixture',encoding='utf-8')
            launcher=ROOT.parent/'tools/windows.ps1'
            env={k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'}
            def run(arguments):
                completed=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',*arguments],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
                return completed.stdout
            run(['-File',str(launcher),'-Action','EnableStartup','-StartupDirectory',str(startup)])
            links=list(startup.glob('Coldstart-*.lnk'));self.assertEqual(len(links),1)
            readback=Path(directory)/'read-shortcut.ps1'
            readback.write_text("$s=New-Object -ComObject WScript.Shell\n$s.CreateShortcut('"+str(links[0]).replace("'","''")+"').Arguments | ConvertTo-Json\n",encoding='utf-8-sig')
            args=json.loads(run(['-File',str(readback)]))
            self.assertIn('-ExecutionPolicy Bypass',args)
            self.assertIn('-WindowStyle Hidden',args)
            self.assertIn('-Action Start -NoBrowser',args)
            self.assertIn(str(launcher),args)
            run(['-File',str(launcher),'-Action','DisableStartup','-StartupDirectory',str(startup)])
            self.assertEqual(list(startup.glob('Coldstart-*.lnk')),[])
            self.assertTrue(unrelated.is_file())

    def isolated(self,script):
        env={k:v for k,v in os.environ.items() if not k.startswith('COLDSTART_') and not k.endswith('_API_KEY')}
        env['PYTHONIOENCODING']='utf-8'
        run=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(run.returncode,0,run.stderr)
        return json.loads(run.stdout)

    def test_actual_first_boot_has_no_personal_settings_or_records(self):
        result=self.isolated('''
import json,tempfile
from app import Workbench
with tempfile.TemporaryDirectory() as tmp:
 w=Workbench(tmp);s=w.state();c=w.config()
 assert all(c['business'][k]=='' for k in ('name','offer','audience','facts'))
 assert c['business']['keywords']==c['business']['topics']==[]
 assert all(c['ai'][k]=='' for k in ('base_url','model','api_key'))
 assert c['radar']['xhs_token']=='' and not s['xhs_bound_user']
 assert all(not c[k]['enabled'] for k in ('publishing','schedule','autonomy'))
 assert all(s[k]==[] for k in ('leads','drafts','jobs','events','inbox'))
 with w.db() as db:
  counts={r[0]:db.execute('SELECT COUNT(*) FROM '+r[0]).fetchone()[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'meta'")}
 assert not any(counts.values()),counts
 print(json.dumps({'empty':True}))
''')
        self.assertTrue(result['empty'])

    def test_installer_can_configure_before_business_is_filled(self):
        result=self.isolated('''
import json,tempfile
import ops
from app import Workbench
with tempfile.TemporaryDirectory() as tmp:
 status=ops.configure(tmp,['xiaohongshu'])
 w=Workbench(tmp);c=w.config()
 assert not status['model_configured']
 assert c['radar']['xhs_enabled'] and not w.meta('xhs_bound_user')
 assert c['business']['name']=='' and c['business']['keywords']==[]
 assert not c['ai']['api_key'] and not c['publishing']['enabled']
 print(json.dumps({'configured_empty_profile':True}))
''')
        self.assertTrue(result['configured_empty_profile'])

    def test_recipient_can_save_model_and_business_in_separate_steps(self):
        result=self.isolated('''
import json,tempfile
from app import Workbench
with tempfile.TemporaryDirectory() as tmp:
 w=Workbench(tmp)
 w.save_settings({'ai':{'base_url':'https://api.example.invalid/v1','model':'fixture-model','api_key':'fixture-local-only'}})
 assert w.config()['business']['name']==''
 w.save_settings({'business':{'name':'合成业务','offer':'合成服务','audience':'合成对象','keywords':['合成需求'],'topics':['合成主题']}})
 assert w.config()['business']['name']=='合成业务'
 assert not w.config()['autonomy']['enabled']
 print(json.dumps({'separate_configuration':True}))
''')
        self.assertTrue(result['separate_configuration'])


if __name__=='__main__':unittest.main()
