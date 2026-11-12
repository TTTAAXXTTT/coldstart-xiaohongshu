"""独立验收入口，生成可检查的报告；不连接真实平台和模型。"""
from __future__ import annotations
import argparse
import io
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from importlib.metadata import version

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))


class RecordedResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.cases=[];self.started={}
    def startTest(self,test):self.started[test.id()]=time.monotonic();super().startTest(test)
    def record(self,test,status,detail=''):
        self.cases.append({'name':test.id(),'status':status,'seconds':round(time.monotonic()-self.started.get(test.id(),time.monotonic()),3),'detail':detail.replace(str(ROOT),'<project>')})
    def addSuccess(self,test):super().addSuccess(test);self.record(test,'passed')
    def addFailure(self,test,error):super().addFailure(test,error);self.record(test,'failed',self._exc_info_to_string(error,test))
    def addError(self,test,error):super().addError(test,error);self.record(test,'error',self._exc_info_to_string(error,test))
    def addSkip(self,test,reason):super().addSkip(test,reason);self.record(test,'skipped',reason)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default=str(ROOT/'SELFTEST_RESULTS.json'))
    args=parser.parse_args()
    # Offline tests explicitly use synthetic business inputs, never recipient data.
    import app
    original=json.loads(json.dumps(app.DEFAULT))
    for section,values in json.loads((ROOT/'tests/fixtures/default-settings.json').read_text(encoding='utf-8')).items():
        app.DEFAULT[section].update(values)
    suite=unittest.defaultTestLoader.discover(str(ROOT),pattern='test_*.py',top_level_dir=str(ROOT))
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordedResult).run(suite)
    app.DEFAULT.clear();app.DEFAULT.update(original)
    frontend={'status':'not_run','reason':'Node.js未安装，前端逻辑测试没有执行'}
    node=shutil.which('node')
    if node:
        from app import Workbench
        qa=ROOT.parent/'qa';qa.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=qa) as directory:
            fixture=Path(directory)/'state.json'
            fixture.write_text(json.dumps(Workbench(Path(directory)/'data').state(),ensure_ascii=False),encoding='utf-8')
            run=subprocess.run([node,str(ROOT/'test_frontend.cjs'),str(fixture)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            if run.returncode==0:
                frontend={'status':'passed',**json.loads(run.stdout)}
            else:frontend={'status':'failed','detail':run.stderr[-5000:]}
    from app import VERSION
    report={'version':VERSION,'time':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),'environment':{'python':platform.python_version(),'system':platform.system(),'pillow':version('Pillow')},'python_tests':{'run':result.testsRun,'passed':sum(c['status']=='passed' for c in result.cases),'failed':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'cases':result.cases},'frontend_logic':frontend,'real_browser_e2e':'NOT_VERIFIED','real_provider_and_social_accounts':'NOT_VERIFIED','clean_network_install':'NOT_VERIFIED','production_ready':False}
    target=Path(args.output).resolve();target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(target),'python_tests':result.testsRun,'python_ok':result.wasSuccessful(),'frontend':frontend['status'],'real_platforms':'not_verified'},ensure_ascii=False))
    return 0 if result.wasSuccessful() and frontend['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
