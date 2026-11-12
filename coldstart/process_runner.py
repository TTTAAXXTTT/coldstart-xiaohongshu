"""有界输出、超时和本程序子进程树回收，不操作其他应用进程。"""
from __future__ import annotations
import os
import signal
import subprocess
import threading
import time
from collections import deque


class WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype=wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
        self.kernel.TerminateJobObject.restype=wintypes.BOOL
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel.CloseHandle.restype=wintypes.BOOL
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle:raise RuntimeError('无法创建本次任务的进程隔离组')
        info=Extended();info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close();raise RuntimeError('无法配置任务结束时的子进程回收')
    def attach_and_resume(self,process):
        from ctypes import wintypes
        if not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise RuntimeError('无法将本次任务加入隔离组，外部脚本尚未运行')
        resume=self.ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes=[wintypes.HANDLE];resume.restype=wintypes.LONG
        if resume(int(process._handle))!=0:raise RuntimeError('无法启动已隔离的本次任务')
    def terminate(self):
        if self.handle and not self.kernel.TerminateJobObject(self.handle,1):
            raise RuntimeError('本次任务组未能正常停止，需要核对当前任务状态')
    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None


def stop_owned_process(process):
    if os.name=='nt':
        job=getattr(process,'_coldstart_job',None)
        if job:job.terminate()
        elif process.poll() is None:process.kill()
    else:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name!='nt':
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        else:process.kill()
        process.wait(timeout=3)


def run(args,cwd=None,timeout=120,cancel=None,env=None):
    job=WindowsJob() if os.name=='nt' else None
    options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW|0x00000004} if os.name=='nt' else {'start_new_session':True}
    try:
        process=subprocess.Popen(args,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,env={**os.environ,'PYTHONIOENCODING':'utf-8',**(env or {})},**options)
        if job:
            process._coldstart_job=job
            job.attach_and_resume(process)
    except Exception:
        if 'process' in locals():
            if process.poll() is None:process.kill();process.wait(timeout=3)
            process.stdout.close();process.stderr.close()
        if job:job.close()
        raise
    output={'stdout':deque(maxlen=16),'stderr':deque(maxlen=16)}
    def collect(stream,name):
        try:
            while True:
                block=stream.read(8192)
                if not block:break
                output[name].append(block)
        finally:stream.close()
    readers=[threading.Thread(target=collect,args=(getattr(process,name),name),daemon=True) for name in ('stdout','stderr')]
    for reader in readers:reader.start()
    deadline=time.monotonic()+timeout
    try:
        while process.poll() is None:
            if (cancel and cancel.is_set()) or time.monotonic()>=deadline:
                stop_owned_process(process)
                raise RuntimeError('外部任务已取消或超时，已停止本次子进程；发布结果仍需核对')
            time.sleep(0.05)
        if job:job.close()  # 先结束仍继承输出管道的本次子进程，再等待读取线程取完尾部输出。
        for reader in readers:reader.join(timeout=3)
        return subprocess.CompletedProcess(args,process.returncode,**{k:b''.join(v).decode('utf-8','replace') for k,v in output.items()})
    finally:
        if process.poll() is None:stop_owned_process(process)
        if job:job.close()

