"""Launch the actual EXE with empty PATH, verify its native window and close it."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from ctypes import wintypes

import psutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    (root / "build").mkdir(parents=True, exist_ok=True)
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    env = os.environ.copy()
    env.update(PATH="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    env.pop("QT_QPA_PLATFORM", None)
    executable = root / "LocalAIWorkspace.exe"
    existing = [p.pid for p in psutil.process_iter(["exe"]) if p.info["exe"] and Path(p.info["exe"]).resolve() == executable]
    if existing:
        raise RuntimeError("Close this directory's existing desktop EXE before the launch check")
    process = subprocess.Popen([str(executable)], cwd=root, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    children = []
    window = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not window:
            if process.poll() is not None:
                raise RuntimeError("EXE exited before its native window appeared")
            children = psutil.Process(process.pid).children(recursive=True)
            ids = {p.pid for p in children}
            found = []
            @callback_type
            def visit(hwnd, extra):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in ids and user32.IsWindowVisible(hwnd):
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value == "Local AI Workspace · 离线桌面":
                        found.append((hwnd, pid.value, title.value))
                return True
            user32.EnumWindows(visit, 0)
            if found:
                window = found[0]
                break
            time.sleep(.1)
        assert window, "Native Qt window was not found"
        # No FastAPI server or browser is part of startup.
        commands = [p.cmdline() for p in children if p.is_running()]
        assert any("app.launcher" in command for command in commands)
        listeners = [c.laddr.port for p in children if p.is_running() for c in p.net_connections(kind="tcp") if c.status == "LISTEN"]
        assert not listeners, listeners
        config = root / "data/workspace.json"
        before = config.read_bytes()
        second = subprocess.run([str(executable)], cwd=root, env=env, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert second.returncode == 0 and config.read_bytes() == before
        user32.PostMessageW(window[0], 0x0010, 0, 0)
        assert process.wait(timeout=60) == 0
        deadline = time.monotonic() + 5
        while any(p.is_running() for p in children) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not any(p.is_running() for p in children)
        report = {"ok": True, "exe": str(executable), "native_window": window[2], "PATH": "",
                  "startup_listeners": listeners, "duplicate_exits_cleanly": True,
                  "settings_preserved": True, "children_exit_on_window_close": True}
        (root / "build/exe-desktop-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        if process.poll() is None:
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
