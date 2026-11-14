"""Bounded subprocess execution, including descendant cleanup on Windows."""
import codecs
import os
import queue
import subprocess
import threading
import time
import stat

import psutil
from .windows_job import start_owned

HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def directory_bytes(folder):
    total = count = 0
    for base, directories, files in os.walk(folder, followlinks=False):
        directories[:] = [name for name in directories if not os.lstat(os.path.join(base, name)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT] if os.name == "nt" else directories
        for name in files:
            try:
                info = os.lstat(os.path.join(base, name))
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                total += info.st_size
                count += 1
                if count > 10000:
                    return float("inf")
            except FileNotFoundError:
                continue
    return total


def terminate_tree(process):
    # Do not resolve a stale PID after the owned process has already exited.
    if process.poll() is not None:
        process.wait(timeout=3)
        return
    children = []
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    for child in reversed(children):
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # A child can exit or become inaccessible during native crash
            # cleanup. This must not prevent reaping the owned root handle.
            pass
    try:
        process.kill()
    except OSError:
        if process.poll() is None:
            raise
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if children:
        try:
            psutil.wait_procs(children, timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def bounded_run(command, *, cwd, env=None, timeout=30, limit=1048576, directory_limit=None, cancelled=None, memory_limit=None,
                stdin_queue=None, on_output=None, working_set_limit=None):
    process, job = start_owned(command, cwd=cwd, env=env,
                               stdin=subprocess.PIPE if stdin_queue is not None else subprocess.DEVNULL, memory_limit=memory_limit,
                               working_set_limit=working_set_limit,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=HIDDEN)
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    guard = threading.Lock()

    def publish(stream, text):
        if not on_output or not text:
            return
        try:
            on_output({"stream": stream, "text": text})
        except Exception:
            # A UI callback must never stop draining the OS pipe: a full pipe
            # would otherwise stall the child until its timeout.
            pass

    def drain(pipe, target, stream):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while block := os.read(pipe.fileno(), 4096):
                with guard:
                    remaining = limit - sum(len(b) for b in buffers)
                    accepted = block[:max(0, remaining)]
                    target.extend(accepted)
                    if len(block) > remaining:
                        exceeded.set()
                if accepted:
                    publish(stream, decoder.decode(accepted))
            publish(stream, decoder.decode(b"", final=True))
        finally:
            pipe.close()

    def feed():
        try:
            while process.poll() is None:
                try:
                    value = stdin_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if value is None:
                    break
                process.stdin.write((str(value) + "\n").encode("utf-8"))
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=drain, args=(pipe, buffer, stream), daemon=True)
               for pipe, buffer, stream in zip((process.stdout, process.stderr), buffers, ("stdout", "stderr"))]
    for thread in threads:
        thread.start()
    feeder = threading.Thread(target=feed, daemon=True) if stdin_queue is not None else None
    if feeder:
        feeder.start()
    start = time.monotonic()
    reason = None
    descendants = {}
    while process.poll() is None:
        try:
            for child in psutil.Process(process.pid).children(recursive=True):
                descendants[child.pid] = child
        except psutil.NoSuchProcess:
            pass
        if cancelled and cancelled.is_set():
            reason = "cancelled"
        elif exceeded.is_set():
            reason = "output_limit"
        elif time.monotonic() - start > timeout:
            reason = "timeout"
        elif directory_limit is not None:
            size = directory_bytes(cwd)
            if size > directory_limit:
                reason = "disk_limit"
        if reason:
            terminate_tree(process)
            break
        time.sleep(0.05)
    job.close()
    memory_error = getattr(job, "memory_error", None)
    if memory_error:
        reason = "memory_limit"
    for child in descendants.values():
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    for thread in threads:
        thread.join(timeout=3)
    if feeder:
        feeder.join(timeout=1)
    if exceeded.is_set() and reason is None:
        reason = "output_limit"
    return {"stdout": buffers[0].decode("utf-8", errors="replace"),
            "stderr": buffers[1].decode("utf-8", errors="replace"),
            "returncode": process.returncode, "reason": reason,
            "seconds": round(time.monotonic() - start, 3), "memory_error": memory_error}
