"""Owned Windows child tree: assign suspended process to a kill-on-close job."""
import ctypes
from ctypes import wintypes as w
import os
import subprocess
import time


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR),
                ('title', w.LPWSTR), ('x', w.DWORD), ('y', w.DWORD), ('xsize', w.DWORD),
                ('ysize', w.DWORD), ('xchars', w.DWORD), ('ychars', w.DWORD),
                ('fill', w.DWORD), ('flags', w.DWORD), ('show', w.WORD),
                ('reserved2size', w.WORD), ('reserved2', ctypes.c_void_p),
                ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('active', w.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class BasicAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ('process_time', 'job_time', 'period_process_time', 'period_job_time')] + [
        (name, w.DWORD) for name in ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


def kernel():
    if os.name != 'nt':
        raise RuntimeError('Windows launcher only')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                            ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)], w.BOOL),
        'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
        'ResumeThread': ([w.HANDLE], w.DWORD),
        'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
        'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
        'GetExitCodeProcess': ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name); fn.argtypes = args; fn.restype = result
    return api


class OwnedChild:
    def __init__(self, args, *, env=None):
        self.api = kernel()
        self.process = None
        self.job = self.api.CreateJobObjectW(None, None)
        if not self.job:
            raise RuntimeError('Cannot create owned child job')
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        pi = ProcessInfo()
        try:
            if not self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise RuntimeError('Cannot configure owned child job')
            si = StartupInfo(); si.cb = ctypes.sizeof(si); si.flags = 1; si.show = 0
            environment = dict(os.environ if env is None else env)
            block = ctypes.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(environment.items(), key=lambda pair: pair[0].upper())) + '\0\0')
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
            # No shell, no inherited handles. Child cannot create descendants
            # before assignment, so venv redirectors cannot escape ownership.
            flags = 0x08000000 | 0x00000400 | 0x00000004
            if not self.api.CreateProcessW(args[0], command, None, None, False, flags,
                                           block, None, ctypes.byref(si), ctypes.byref(pi)):
                raise RuntimeError('Cannot create owned child process')
            self.process = pi.process; self.pid = int(pi.pid)
            if not self.api.AssignProcessToJobObject(self.job, self.process):
                self.api.TerminateProcess(self.process, 1)
                raise RuntimeError('Cannot assign owned child process')
            if self.api.ResumeThread(pi.thread) == 0xFFFFFFFF:
                raise RuntimeError('Cannot resume owned child process')
        except Exception:
            self.stop()
            raise
        finally:
            if pi.thread:
                self.api.CloseHandle(pi.thread)

    def poll(self):
        if not self.process:
            return 1
        state = self.api.WaitForSingleObject(self.process, 0)
        if state == 258:
            return None
        if state != 0:
            raise RuntimeError('Cannot inspect owned child')
        code = w.DWORD()
        if not self.api.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise RuntimeError('Cannot inspect owned child exit')
        return int(code.value)

    def stop(self):
        if self.job:
            self.api.TerminateJobObject(self.job, 0)
            # The venv wrapper can already be dead while its real interpreter
            # still owns the listening socket. Wait for the whole owned job,
            # not just that wrapper, before probing ports for the replacement.
            deadline = time.monotonic() + 5
            while True:
                accounting = BasicAccounting()
                if not self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    break
                if accounting.active_processes == 0 or time.monotonic() >= deadline:
                    break
                time.sleep(.05)
        if self.process:
            self.api.WaitForSingleObject(self.process, 5000)
            self.api.CloseHandle(self.process); self.process = None
        if self.job:
            self.api.CloseHandle(self.job); self.job = None
