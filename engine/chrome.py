"""Chrome 进程管理：账号实例唯一性、启动/复用/置前/关闭验证。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .accounts import ApiError
from .config import Config, detect_chrome

CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
CDP_BASE = "http://127.0.0.1:{port}"
MANAGED_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-features=SessionRestore",
    "--window-position=80,60",
    "--window-size=1400,900",
)


def chrome_exe(config: Config) -> str:
    path = str(config.get("chrome_path") or "").strip() or detect_chrome()
    if not path or not Path(path).is_file():
        raise ApiError(1006, "未找到 Chrome，请在 config.json 配置 chrome_path", 500)
    return path


def http_json(url: str, method: str = "GET", timeout: float = 1.5) -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cdp_version(port: int, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        return http_json(CDP_BASE.format(port=port) + "/json/version", timeout=timeout)
    except Exception:
        return None


def cdp_alive(port: int) -> bool:
    return cdp_version(port) is not None


def list_targets(port: int) -> list[dict[str, Any]]:
    try:
        data = http_json(CDP_BASE.format(port=port) + "/json/list")
    except Exception:
        return []
    return [t for t in data if isinstance(t, dict)]


def new_target(port: int, url: str) -> dict[str, Any] | None:
    base = CDP_BASE.format(port=port) + "/json/new?url=" + urllib.parse.quote(url, safe="")
    for method in ("PUT", "GET"):
        try:
            data = http_json(base, method=method, timeout=3.0)
            if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                return data
        except Exception:
            continue
    return None


def close_target(port: int, target_id: str) -> bool:
    """关闭一个 CDP 页面 target（/json/close 返回纯文本，不能走 http_json）。"""
    target = str(target_id or "").strip()
    if not target:
        return False
    url = CDP_BASE.format(port=port) + "/json/close/" + urllib.parse.quote(target, safe="")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return int(getattr(resp, "status", 200) or 200) == 200
    except Exception:
        return False


def wait_cdp(port: int, timeout: float = 30.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_alive(port):
            return True
        time.sleep(interval)
    return False


def _run_powershell(script: str, timeout: float = 20.0) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return (proc.stdout or "").strip()


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


_USER_DATA_DIR_RE = re.compile(r'--user-data-dir=(?:"([^"]*)"|(\S+))', re.IGNORECASE)


def profile_arg_from_command_line(command_line: str) -> str:
    """从 Chrome 命令行中精确提取 --user-data-dir（不做裸子串匹配）。"""
    match = _USER_DATA_DIR_RE.search(str(command_line or ""))
    if not match:
        return ""
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return str(raw or "").strip().strip('"')


def same_profile_path(left: str | Path, right: str | Path) -> bool:
    def norm(value: str | Path) -> str:
        try:
            return os.path.normcase(os.path.normpath(str(value))).rstrip("\\")
        except (OSError, ValueError):
            return ""
    return bool(norm(left)) and norm(left) == norm(right)


def _process_rows() -> list[dict[str, Any]]:
    if IS_WINDOWS:
        script = (
            "$out=@();"
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "ForEach-Object { if ($_.CommandLine) { $out += [pscustomobject]@{pid=$_.ProcessId;cmd=$_.CommandLine} } };"
            "if ($out.Count -eq 0) { '' } else { $out | ConvertTo-Json -Compress }"
        )
        out = _run_powershell(script)
        if not out:
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []
        if isinstance(data, dict):
            data = [data]
        return [{"pid": int(item.get("pid") or 0), "cmd": str(item.get("cmd") or "")}
                for item in (data or []) if isinstance(item, dict) and item.get("pid")]
    if IS_MACOS:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                                 timeout=10).stdout
        except Exception:
            return []
        rows = []
        for line in (out or "").splitlines():
            line = line.strip()
            if not line or "Google Chrome" not in line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                rows.append({"pid": int(parts[0]), "cmd": parts[1]})
            except ValueError:
                continue
        return rows
    return []


def profile_pids(profile: str | Path) -> list[int]:
    """以该 profile 为 user-data-dir 的 Chrome 根进程（不含 --type= 子进程）。"""
    pids: list[int] = []
    for item in _process_rows():
        command_line = str(item.get("cmd") or "")
        if "--type=" in command_line:
            continue
        if same_profile_path(profile_arg_from_command_line(command_line), profile):
            pids.append(int(item.get("pid") or 0))
    return sorted(set(pid for pid in pids if pid > 0))


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def kill_pids(pids: list[int]) -> int:
    killed = 0
    for pid in pids:
        try:
            if IS_WINDOWS:
                _run_powershell(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; 'ok'")
            else:
                os.kill(int(pid), signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    return killed


def normalize_exit_state(profile: str | Path) -> None:
    """启动前把 Chrome Preferences 的崩溃退出标记改成正常，避免“要恢复页面吗”气泡。"""
    pref = Path(profile) / "Default" / "Preferences"
    if not pref.is_file():
        return
    try:
        data = json.loads(pref.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        prof = data.get("profile")
        if not isinstance(prof, dict):
            prof = {}
            data["profile"] = prof
        changed = False
        if prof.get("exit_type") != "Normal":
            prof["exit_type"] = "Normal"
            changed = True
        if prof.get("exited_cleanly") is not True:
            prof["exited_cleanly"] = True
            changed = True
        if changed:
            tmp = pref.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, pref)
    except (OSError, ValueError):
        pass


def dismiss_restore_bubble(profile: str | Path) -> int:
    """Windows：关闭该 profile Chrome 的“要恢复页面吗”原生窗口，返回关闭数量。"""
    if not IS_WINDOWS:
        return 0
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        pids = set(profile_pids(profile))
        if not pids:
            return 0
        targets: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        @enum_proc
        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if "要恢复页面" not in title:
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in pids:
                targets.append(int(hwnd))
            return True

        user32.EnumWindows(callback, 0)
        for hwnd in targets:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return len(targets)
    except Exception:
        return 0


def clear_stale_state(profile: str | Path) -> None:
    p = Path(profile)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            (p / name).unlink()
        except OSError:
            pass
    sessions = p / "Default" / "Sessions"
    if sessions.is_dir():
        for f in sessions.iterdir():
            try:
                f.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def launch_lock(config: Config, slug: str, timeout: float = 20.0):
    """每账号启动锁：防连点/并发启动出两个窗口。"""
    lock_dir = config.paths.temp
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"launch-{slug}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode("utf-8"))
            os.close(fd)
            break
        except FileExistsError:
            stale = True
            try:
                payload = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(payload.get("pid") or 0)
                ts = float(payload.get("ts") or 0)
                stale = (time.time() - ts > 120) or (pid > 0 and not pid_alive(pid))
            except Exception:
                stale = True
            if stale:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise ApiError(1011, f"账号 {slug} 启动锁被占用（另一窗口正在打开）", 409)
            time.sleep(0.15)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def focus_pid(pid: int) -> bool:
    try:
        if IS_WINDOWS:
            _run_powershell(f"$ws = New-Object -ComObject WScript.Shell; $ws.AppActivate({int(pid)}) | Out-Null")
        elif IS_MACOS:
            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                           capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def launch(config: Config, slug: str, profile: str | Path, port: int,
           url: str, headless: bool = False) -> dict[str, Any]:
    """低层启动：不做并发判定（由 ensure_single_instance 包裹锁）。"""
    if cdp_alive(port):
        return {"ok": True, "already_running": True, "port": port, "profile": str(profile)}
    exe = chrome_exe(config)
    stale = profile_pids(profile)
    if stale:
        kill_pids(stale)
        time.sleep(0.6)
    normalize_exit_state(profile)
    clear_stale_state(profile)
    Path(profile).mkdir(parents=True, exist_ok=True)
    args = [exe, f"--user-data-dir={profile}", "--profile-directory=Default",
            f"--remote-debugging-port={port}"] + list(MANAGED_FLAGS)
    if headless:
        args.append("--headless=new")
    args.append(url)
    kwargs = {"close_fds": True}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    proc = subprocess.Popen(args, **kwargs)
    if not wait_cdp(port):
        raise ApiError(1007, f"Chrome 启动后 CDP 未就绪（端口 {port}）", 502)
    time.sleep(0.8)
    dismissed = dismiss_restore_bubble(profile)
    return {"ok": True, "already_running": False, "port": port,
            "profile": str(profile), "pid": proc.pid, "restore_bubble_closed": dismissed}


def ensure_single_instance(config: Config, slug: str, profile: str | Path, port: int,
                           url: str, headless: bool = False, display_name: str = "") -> dict[str, Any]:
    """跨平台：复用/启动一个账号 Chrome；一个 profile 一个根进程。"""
    with launch_lock(config, slug):
        pids = profile_pids(profile)
        alive = cdp_alive(port)
        if len(pids) >= 2:
            keep = pids[0]
            kill_pids(pids[1:])
            pids = [keep]
            time.sleep(0.5)
            alive = cdp_alive(port)
        if pids and alive:
            focused = focus_pid(pids[0])
            dismissed = dismiss_restore_bubble(profile)
            return {"ok": True, "already_running": True, "focused": focused,
                    "pid": pids[0], "port": port, "profile": str(profile),
                    "restore_bubble_closed": dismissed}
        if pids and not alive:
            kill_pids(pids)
            time.sleep(0.8)
            clear_stale_state(profile)
        elif not pids and alive:
            raise ApiError(1007, f"端口 {port} 已被其他进程占用（非本账号 Chrome）", 502)
        launched = launch(config, slug, profile, port, url, headless=headless)
        pids_after = profile_pids(profile)
        if len(pids_after) > 1:
            kill_pids(pids_after[1:])
            pids_after = pids_after[:1]
        if len(pids_after) != 1:
            raise ApiError(1007, f"启动后实例数异常（{len(pids_after)} 个），已终止多余实例", 502)
        if not cdp_alive(port):
            raise ApiError(1007, "启动后 CDP 不可用", 502)
        launched["pid"] = pids_after[0]
        launched["verified"] = True
        return launched


def close(profile: str | Path, port: int) -> dict[str, Any]:
    """关闭并验证：进程清零 + CDP 不可用；失败重试一次。"""
    killed = 0
    for _ in range(2):
        killed += kill_pids(profile_pids(profile))
        time.sleep(0.8)
        if not profile_pids(profile) and not cdp_alive(port):
            return {"ok": True, "killed": killed, "verified": True}
    return {"ok": False, "killed": killed, "verified": False,
            "error": "Chrome 未能完全关闭（仍有进程或 CDP 仍在）"}
