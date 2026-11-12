"""Real isolated OS-process failure injection. No account or platform is accessed."""
import json,os,sqlite3,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parent

@unittest.skipUnless(os.name=='nt','Windows owned-process recovery')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cwd=self.root/'coldstart';self.cwd.mkdir()
        self.data=self.root/'data';self.data.mkdir();self.ready=self.root/'run/ready.json';self.flag=self.root/'stop.flag'
        self.db=self.data/'coldstart.sqlite3'
        with closing(sqlite3.connect(self.db)) as db,db:db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO meta VALUES('shutdown_requested','0')")
        self.processes=[];self.addCleanup(self.cleanup)
        self.child=self.root/'child.py'
        self.child.write_text('''import argparse,os,time,json,sqlite3
from pathlib import Path
from contextlib import closing
p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--ready-file');p.add_argument('--crash-first',action='store_true');a=p.parse_args()
data=Path(a.data_dir);counter=data/'starts.txt'
with counter.open('a') as f:f.write(str(os.getpid())+'\\n')
ready=Path(a.ready_file);ready.parent.mkdir(exist_ok=True)
with ready.open('x') as f:json.dump({'pid':os.getpid(),'data_dir':str(data)},f)
if a.crash_first and len(counter.read_text().splitlines())==1:os._exit(9)
while True:
 with closing(sqlite3.connect(data/'coldstart.sqlite3')) as db:
  if db.execute("SELECT value FROM meta WHERE key='shutdown_requested'").fetchone()[0]=='1':break
 time.sleep(.05)
''',encoding='utf-8')

    def cleanup(self):
        self.flag.write_text('stop',encoding='utf-8')
        with closing(sqlite3.connect(self.db)) as db,db:db.execute("UPDATE meta SET value='1'")
        for p in self.processes:
            try:p.wait(timeout=12)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)

    def start(self,extra=(),child_extra=()):
        command=[sys.executable,str(ROOT/'managed_host.py'),'--cwd',str(self.cwd),'--log',str(self.root/'host.log'),'--restart','--restart-delay','.1','--stop-file',str(self.flag),*extra,'--',sys.executable,str(self.child),'--data-dir',str(self.data),'--ready-file',str(self.ready),*child_extra]
        p=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW);self.processes.append(p);return p

    def wait_starts(self,n):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if (self.data/'starts.txt').exists() and len((self.data/'starts.txt').read_text().splitlines())>=n and self.ready.exists():return
            time.sleep(.05)
        self.fail('Owned process did not recover: '+(self.root/'host.log').read_text(encoding='utf-8',errors='replace'))

    def stop(self,p):
        self.flag.write_text('stop',encoding='utf-8');p.wait(timeout=12);self.assertEqual(p.returncode,0)

    def test_backend_crash_recovers_and_replaces_only_owned_ready(self):
        p=self.start(child_extra=['--crash-first']);self.wait_starts(2)
        self.stop(p)
        self.assertEqual(len((self.data/'starts.txt').read_text().splitlines()),2)
        self.assertFalse(self.ready.exists())

    def test_stop_latch_stops_retries_even_without_database(self):
        # Missing related executable fails before the backend opens any database.
        p=self.start(extra=['--related-exe',str(self.root/'missing.exe'),'--related-cwd',str(self.cwd),'--related-log',str(self.root/'related.log')])
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if (self.root/'host.log').exists() and 'Restarting' in (self.root/'host.log').read_text(encoding='utf-8',errors='replace'):break
            time.sleep(.05)
        self.db.unlink();self.flag.write_text('stop',encoding='utf-8')
        p.wait(timeout=8);self.assertEqual(p.returncode,0)
        # Restore a temporary schema for cleanup, never a user database.
        with closing(sqlite3.connect(self.db)) as db,db:db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO meta VALUES('shutdown_requested','1')")

    def test_related_process_crash_recovers_whole_owned_group(self):
        code='''using System;using System.IO;using System.Threading;using System.Diagnostics;class Related{static void Main(){string p=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"related.starts");File.AppendAllText(p,Process.GetCurrentProcess().Id+Environment.NewLine);if(File.ReadAllLines(p).Length==1){Thread.Sleep(2200);return;}Thread.Sleep(Timeout.Infinite);}}'''
        cs=self.root/'related.cs';cs.write_text(code,encoding='utf-8');exe=self.root/'related.exe'
        ps=self.root/'compile.ps1';ps.write_text("Add-Type -Path '"+str(cs).replace("'","''")+"' -OutputAssembly '"+str(exe).replace("'","''")+"' -OutputType ConsoleApplication\n",encoding='utf-8-sig')
        env={k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'}
        build=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ps)],env=env,capture_output=True,timeout=30)
        self.assertEqual(build.returncode,0,build.stderr.decode('utf-8',errors='replace'))
        p=self.start(extra=['--related-exe',str(exe),'--related-cwd',str(self.root),'--related-log',str(self.root/'related.log')]);self.wait_starts(2)
        self.stop(p);self.assertGreaterEqual(len((self.root/'related.starts').read_text().splitlines()),2)

    def test_normal_exit_is_not_restarted(self):
        marker=self.root/'once.txt'
        p=subprocess.Popen([sys.executable,str(ROOT/'managed_host.py'),'--cwd',str(self.cwd),'--log',str(self.root/'host.log'),'--restart','--',sys.executable,'-c',"from pathlib import Path;Path("+repr(str(marker))+").write_text('once')"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(p);p.wait(timeout=10);self.assertEqual(p.returncode,0);self.assertEqual(marker.read_text(),'once')

    def test_cleanup_preserves_another_live_process_ready_record(self):
        import managed_host
        from types import SimpleNamespace
        self.ready.parent.mkdir(exist_ok=True)
        record={'pid':os.getpid(),'data_dir':str(self.data)}
        self.ready.write_text(json.dumps(record),encoding='utf-8')
        args=SimpleNamespace(cwd=str(self.cwd))
        command=['backend','--data-dir',str(self.data),'--ready-file',str(self.ready)]
        managed_host.clear_owned_ready(args,command,SimpleNamespace(pid=-1))
        self.assertEqual(json.loads(self.ready.read_text(encoding='utf-8')),record)

if __name__=='__main__':unittest.main()
