"""Windows Job Object: all descendants die when the owning job is closed."""
import ctypes
import os
import subprocess
import threading
from ctypes import wintypes

import psutil


class WindowsJob:
    def __init__(self, memory_limit=None, working_set_limit=None):
        self.handle = None
        self.memory_limit = memory_limit
        self.working_set_limit = working_set_limit
        self.memory_error = None
        self.memory_status = {}
        self._closed = threading.Event()
        self._guard_lock = threading.RLock()
        self._monitor = None
        if os.name != "nt":
            return
        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", Counters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        self.kernel.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit:
            limits.BasicLimitInformation.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = int(memory_limit)
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process):
        if self.handle:
            if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                process.kill()
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())
            if self.working_set_limit:
                try:
                    self._set_working_set(wintypes.HANDLE(int(process._handle)))
                except BaseException:
                    self.close()
                    process.wait(timeout=5)
                    raise
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = ctypes.c_long
            if resume(wintypes.HANDLE(int(process._handle))) != 0:
                self.close()
                raise RuntimeError("无法恢复隔离子进程")
        if self.working_set_limit:
            self._monitor = threading.Thread(target=self._watch_memory, args=(process,), daemon=True,
                                             name=f"memory-guard-{process.pid}")
            self._monitor.start()

    def _set_working_set(self, handle):
        # MAX_ENABLE is a hard physical working-set cap, including mmap pages.
        # MIN_DISABLE allows Windows to reclaim pages rather than pinning them.
        # Windows can temporarily count faulted/nonpageable pages above the
        # requested maximum. Trim below the user budget instead of raising
        # that budget or killing a healthy model for a few transient pages.
        headroom = min(64 * 1024**2, int(self.working_set_limit) // 16)
        if not self.kernel.SetProcessWorkingSetSizeEx(handle, 1024 * 1024,
                                                      int(self.working_set_limit) - headroom, 0x4 | 0x2):
            error = ctypes.get_last_error()
            raise OSError(error, "无法启用物理内存上限：" + ctypes.FormatError(error))

    def _watch_memory(self, process):
        try:
            parent = psutil.Process(process.pid)
            capped = set()
            while not self._closed.is_set() and process.poll() is None:
                rss = private = 0
                for member in [parent] + parent.children(recursive=True):
                    try:
                        # Descendants share the commit budget; also cap their RSS.
                        identity = (member.pid, member.create_time())
                        if self.handle and self.working_set_limit and member.pid != parent.pid and identity not in capped:
                            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                            self.kernel.OpenProcess.restype = wintypes.HANDLE
                            handle = self.kernel.OpenProcess(0x0100 | 0x0400, False, member.pid)
                            if not handle:
                                raise ctypes.WinError(ctypes.get_last_error())
                            try:
                                self._set_working_set(handle)
                            finally:
                                self.kernel.CloseHandle(handle)
                            capped.add(identity)
                        info = member.memory_info()
                        rss += info.rss
                        private += getattr(info, "private", 0)
                    except psutil.NoSuchProcess:
                        continue
                available = psutil.virtual_memory().available
                self.memory_status = {"rss": rss, "private": private, "available": available,
                                      "limit": self.working_set_limit}
                message = None
                if self.working_set_limit and rss > self.working_set_limit:
                    message = f"模型进程组物理内存 {rss / 1024**3:.2f} GiB 超过有效上限 {self.working_set_limit / 1024**3:.2f} GiB"
                elif self.memory_limit and private > self.memory_limit:
                    message = f"模型进程组提交内存超过 {self.memory_limit / 1024**3:.2f} GiB 上限"
                if message:
                    self._stop_for_memory(process, message)
                    return
                self._closed.wait(0.25)
        except psutil.NoSuchProcess:
            return
        except Exception as exc:
            if not self._closed.is_set() and process.poll() is None:
                self._stop_for_memory(process, f"无法继续监测内存：{exc}")

    def _stop_for_memory(self, process, message):
        with self._guard_lock:
            if self._closed.is_set():
                return
            self.memory_error = "内存保护已停止模型：" + message + "。可缩短上下文、关闭其他程序或选择较小模型。"
            if not self.handle:
                try:
                    parent = psutil.Process(process.pid)
                    for member in reversed(parent.children(recursive=True)):
                        member.kill()
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            self.close()

    def close(self):
        with self._guard_lock:
            self._closed.set()
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None

    def __del__(self):
        self.close()


def start_owned(command, **kwargs):
    job = WindowsJob(kwargs.pop("memory_limit", None), kwargs.pop("working_set_limit", None))
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | (0x00000004 if os.name == "nt" else 0)
    process = None
    try:
        process = subprocess.Popen(command, **kwargs)
        job.attach_and_resume(process)
    except BaseException:
        job.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        raise
    return process, job
