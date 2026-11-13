"""配置与路径：安装根、目录布局、端口、Chrome 探测。"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

APP_NAME = "DouyinAutoUploadSkill"
APP_VERSION = "1.0.0"
SKILL_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS: dict[str, Any] = {
    "service_port": 8765,
    "cdp_port_start": 9300,
    "cdp_port_end": 9499,
    "chrome_path": "",
    "browser_visible": True,
    "log_level": "INFO",
    "create_desktop_shortcut": True,
    "auto_capture_login": True,
    "auto_check_interval_sec": 30,
    "chrome_window_monitor": True,
    "chrome_monitor_interval_sec": 2,
    "rotate_token_on_start": False,
    "local_session_recovery": True,
    "fresh_upload_page": True,
    "tab_guard": True,
}

if os.name == "nt":
    CHROME_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
elif sys.platform == "darwin":
    CHROME_CANDIDATES = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
else:
    CHROME_CANDIDATES = []


def detect_chrome() -> str:
    for cand in CHROME_CANDIDATES:
        if cand and Path(cand).is_file():
            return cand
    return ""


def is_non_c_path(path: str | Path) -> bool:
    """安装根/业务路径必须在非 C 盘。"""
    p = Path(path)
    drive = p.drive.upper() if p.drive else ""
    return not drive.startswith("C:")


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_port(start: int, end: int, host: str = "127.0.0.1") -> int | None:
    for port in range(start, end + 1):
        if is_port_free(port, host):
            return port
    return None


class Paths:
    """安装根下的目录结构（见 Phase 0 契约 §5）。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.app_dir = self.root / "app"
        self.runtime = self.root / "runtime"
        self.venv = self.runtime / "venv"
        self.browsers = self.runtime / "browsers"
        self.caches = self.runtime / "caches"
        self.temp = self.runtime / "temp"
        self.jobs = self.runtime / "jobs"
        self.logs = self.runtime / "logs"
        self.profiles = self.runtime / "profiles"
        self.data = self.root / "data"
        self.secrets = self.root / "secrets"
        self.cookies = self.secrets / "cookies"
        self.account_states = self.secrets / "account_state"
        self.shortcuts = self.root / "shortcuts"
        self.docs = self.root / "docs"
        self.config_file = self.root / "config.json"
        self.token_file = self.runtime / "token.txt"
        self.accounts_json = self.data / "accounts.json"
        self.packages = self.data / "packages"
        self.log_file = self.logs / "service.log"
        self.hard_bans = self.data / "hard_bans.json"
        self.content_spec_defaults = self.data / "content_spec_defaults.json"

    def profile_dir(self, slug: str) -> Path:
        return self.profiles / slug

    def cookie_snapshot(self, slug: str) -> Path:
        return self.cookies / f"{slug}.json"

    def account_state(self, slug: str) -> Path:
        return self.account_states / f"{slug}_full.json"

    def ensure(self) -> None:
        for d in (
            self.app_dir, self.runtime, self.browsers, self.caches, self.temp,
            self.jobs, self.logs, self.profiles, self.data, self.packages, self.secrets,
            self.cookies, self.account_states, self.shortcuts, self.docs,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "temp": str(self.temp),
            "jobs": str(self.jobs),
            "logs": str(self.logs),
            "profiles": str(self.profiles),
            "data": str(self.data),
            "secrets": str(self.secrets),
            "shortcuts": str(self.shortcuts),
        }


def skill_pointer_file() -> Path:
    return SKILL_ROOT / "config.local.json"


def default_root() -> Path:
    env = os.environ.get("DOUYIN_UPLOAD_DATA_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    pointer = skill_pointer_file()
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            root = str((data or {}).get("data_root") or "").strip()
            if root:
                return Path(root).expanduser().resolve()
        except Exception:
            pass
    # 未初始化时返回 skill 内的占位路径，绝不默认写 C 盘用户目录
    return (SKILL_ROOT / ".uninitialized-data").resolve()


class Config:
    def __init__(self, root: str | Path | None = None):
        self.paths = Paths(root or default_root())
        self.dev_mode = False
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> "Config":
        if self.paths.config_file.is_file():
            try:
                raw = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    merged = dict(DEFAULTS)
                    merged.update(raw)
                    self.data = merged
            except (OSError, ValueError):
                pass
        if not self.data.get("chrome_path"):
            detected = detect_chrome()
            if detected:
                self.data["chrome_path"] = detected
        return self

    def save(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        payload = dict(self.data)
        payload["install_root"] = str(self.paths.root)
        tmp = self.paths.config_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.paths.config_file)

    # ---------- 取值 ----------
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def service_port(self) -> int:
        return int(self.data.get("service_port") or 8765)

    @property
    def cdp_start(self) -> int:
        return int(self.data.get("cdp_port_start") or 9300)

    @property
    def cdp_end(self) -> int:
        return int(self.data.get("cdp_port_end") or 9499)

    def resolve_service_port(self) -> int:
        """服务端口被占用时自动 +1 并回写配置（C1）。"""
        port = self.service_port
        if is_port_free(port):
            return port
        new_port = find_free_port(port + 1, port + 30)
        if new_port is None:
            raise RuntimeError("no free service port")
        self.data["service_port"] = new_port
        self.save()
        return new_port
