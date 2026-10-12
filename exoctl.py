#!/usr/bin/env python3
"""
exoctl - a zero-dependency local control panel for the exo distributed-AI
platform (https://github.com/exo-explore/exo).

Goals
-----
* One utility that works on every Mac you own even though each was installed
  differently. It auto-detects, per host, HOW exo can be launched here:
  uv-managed env, a plain .venv, system-python `exo`, or the brew-cask app.
* Configure and run exo without Docker.
* Make exo use models you already downloaded locally instead of pulling from
  Hugging Face every time (EXO_OFFLINE + EXO_MODELS_* env vars).
* A troubleshooter that reads error output (captured or pasted) and tells you
  the exact command to run next.

Only the Python 3 standard library is used, so this runs with the stock
/usr/bin/python3 on macOS regardless of how exo itself is installed.

Usage
-----
    python3 exoctl.py            # start the web control panel (default)
    python3 exoctl.py ui         # same, explicit
    python3 exoctl.py detect     # print environment detection as JSON
    python3 exoctl.py start      # start exo headless using saved config
    python3 exoctl.py stop       # stop a running exo started by exoctl
    python3 exoctl.py status     # print status as JSON
    python3 exoctl.py diagnose <file>   # run the troubleshooter on a log file

The web panel listens on http://127.0.0.1:8577 by default (override with
--panel-port or EXOCTL_PANEL_PORT). exo itself listens on 52415.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants / paths
# --------------------------------------------------------------------------- #

PANEL_PORT = int(os.environ.get("EXOCTL_PANEL_PORT", "8577"))
EXO_PORT = 52415  # exo's dashboard + OpenAI-compatible API port

CONFIG_DIR = Path(os.path.expanduser("~/.config/exoctl"))
CONFIG_PATH = CONFIG_DIR / "config.json"
HOSTNAME = socket.gethostname().split(".")[0]

# Common places an exo checkout or models might live. Order = search priority.
REPO_CANDIDATES = [
    "~/exo",
    "~/code/exo",
    "~/Developer/exo",
    "~/dev/exo",
    "~/projects/exo",
    "~/src/exo",
    "~/Documents/exo",
]
MODEL_DIR_CANDIDATES = [
    "~/.exo/models",                      # exo default on macOS
    "~/.local/share/exo/models",          # exo default on Linux
    "~/.cache/exo/downloads",             # legacy layout
    "~/.cache/huggingface/hub",           # huggingface-cli downloads
    "~/.lmstudio/models",                 # LM Studio downloads (if reused)
]

LOG_CAP = 4000  # max log lines kept in memory

# Effective bind address of this agent's panel (filled in at startup). Other
# nodes reach this machine here, so to be controllable it must be the
# Thunderbolt/LAN IP rather than loopback.
BIND_HOST = "127.0.0.1"
BIND_PORT = PANEL_PORT


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def expand(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p)) if p else p


def local_ips() -> set[str]:
    """Every IP address that belongs to this machine (so we can tell 'self'
    apart from a peer node calling in over the Thunderbolt bridge)."""
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(res[4][0])
    except socket.gaierror:
        pass
    # macOS/BSD: scrape ifconfig for every inet / inet6 address.
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=4).stdout
        for m in re.finditer(r"\binet6?\s+([0-9a-fA-F:.]+)", out):
            ips.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


SELF_IPS = local_ips()


def which(name: str) -> str | None:
    return shutil.which(name)


def is_port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get_json(url: str, timeout: float = 3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def default_config() -> dict:
    return {
        "hosts": {},       # keyed by hostname -> per-machine settings
        "cluster": {       # shared view of all Macs in the mesh
            "token": "",   # optional shared secret for node-to-node control
            "nodes": [],   # [{"name","host","panel_port"}]
        },
    }


def get_cluster(cfg: dict) -> dict:
    c = cfg.setdefault("cluster", {})
    c.setdefault("token", "")
    c.setdefault("nodes", [])
    return c


def host_defaults() -> dict:
    return {
        "repo_path": "",
        "launch_mode": "",          # auto|uv|venv|system|app
        "offline": False,
        "default_models_dir": "",   # EXO_DEFAULT_MODELS_DIR
        "models_dirs": [],          # EXO_MODELS_DIRS (writable)
        "read_only_dirs": [],       # EXO_MODELS_READ_ONLY_DIRS (pre-downloaded)
        "enable_image_models": False,
        "extra_flags": "",          # e.g. --no-worker
        "extra_env": {},            # arbitrary EXO_*/HF_* overrides
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if "hosts" not in cfg:
                cfg = default_config()
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return default_config()


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_host_config(cfg: dict) -> dict:
    h = cfg["hosts"].get(HOSTNAME)
    merged = host_defaults()
    if h:
        merged.update(h)
    return merged


def set_host_config(cfg: dict, host_cfg: dict) -> None:
    cfg["hosts"][HOSTNAME] = host_cfg


# --------------------------------------------------------------------------- #
# Environment detection  (the "understand how exo runs on THIS machine" part)
# --------------------------------------------------------------------------- #

def find_repo(explicit: str = "") -> str | None:
    """Locate an exo source checkout: a dir with pyproject.toml + a dashboard."""
    cands = []
    if explicit:
        cands.append(explicit)
    cands.append(os.getcwd())
    cands.extend(REPO_CANDIDATES)
    for c in cands:
        p = Path(expand(c))
        if not p.is_dir():
            continue
        pyproj = p / "pyproject.toml"
        if pyproj.is_file():
            try:
                txt = pyproj.read_text(errors="replace").lower()
            except OSError:
                txt = ""
            # exo's pyproject names the project "exo"; dashboard folder present
            if "exo" in txt and (p / "dashboard").exists():
                return str(p)
            if "exo" in txt and (p / "exo").exists():
                return str(p)
    return None


def detect(explicit_repo: str = "") -> dict:
    """Figure out every viable way to launch exo on this host."""
    info: dict = {
        "hostname": HOSTNAME,
        "platform": sys.platform,
        "python": sys.executable,
        "tools": {},
        "repo": None,
        "launch_options": [],   # ordered best-first
        "recommended": None,
        "notes": [],
    }

    for tool in ("uv", "node", "npm", "brew", "git", "rustup", "macmon"):
        info["tools"][tool] = which(tool)

    repo = find_repo(explicit_repo)
    info["repo"] = repo

    opts: list[dict] = []

    if repo:
        rp = Path(repo)
        has_uv_lock = (rp / "uv.lock").exists()
        has_venv = (rp / ".venv" / "bin" / "python").exists()
        venv_exo = (rp / ".venv" / "bin" / "exo")
        dashboard_built = any(
            (rp / "dashboard" / d).exists() for d in ("dist", "build", ".output", "out")
        )
        info["dashboard_built"] = dashboard_built

        if info["tools"]["uv"]:
            opts.append({
                "mode": "uv",
                "label": "uv run (uv-managed virtual env)",
                "cmd": [info["tools"]["uv"], "run", "exo"],
                "cwd": repo,
                "ready": True,
                "why": "uv is installed and an exo checkout was found.",
            })
        if has_venv:
            exe = str(venv_exo) if venv_exo.exists() else str(rp / ".venv" / "bin" / "python")
            cmd = [exe] if venv_exo.exists() else [exe, "-m", "exo"]
            opts.append({
                "mode": "venv",
                "label": "repo .venv (plain Python virtual env)",
                "cmd": cmd,
                "cwd": repo,
                "ready": True,
                "why": "A .venv exists inside the checkout.",
            })

    # system python has exo importable / an `exo` entrypoint on PATH
    sys_exo = which("exo")
    if sys_exo:
        opts.append({
            "mode": "system",
            "label": "system `exo` on PATH",
            "cmd": [sys_exo],
            "cwd": repo or os.path.expanduser("~"),
            "ready": True,
            "why": f"`exo` found on PATH at {sys_exo}.",
        })
    else:
        # is exo importable by the current python even without a console script?
        try:
            r = subprocess.run(
                [sys.executable, "-c", "import importlib.util,sys;"
                 "sys.exit(0 if importlib.util.find_spec('exo') else 1)"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                opts.append({
                    "mode": "system",
                    "label": "system python -m exo",
                    "cmd": [sys.executable, "-m", "exo"],
                    "cwd": repo or os.path.expanduser("~"),
                    "ready": True,
                    "why": "exo is importable by the current Python interpreter.",
                })
        except (subprocess.SubprocessError, OSError):
            pass

    # brew cask app
    app_candidates = ["/Applications/exo.app", os.path.expanduser("~/Applications/exo.app")]
    for app in app_candidates:
        if Path(app).exists():
            # launch the bundled binary if present, else `open` the app
            bin_guess = Path(app) / "Contents" / "MacOS" / "exo"
            cmd = [str(bin_guess)] if bin_guess.exists() else ["open", app]
            opts.append({
                "mode": "app",
                "label": "exo.app (brew cask / DMG install)",
                "cmd": cmd,
                "cwd": os.path.expanduser("~"),
                "ready": True,
                "why": f"Found installed application at {app}.",
            })
            break

    info["launch_options"] = opts
    info["recommended"] = opts[0]["mode"] if opts else None

    # Notes / gentle warnings
    if not repo and not sys_exo and not opts:
        info["notes"].append(
            "No exo checkout, `exo` command, or exo.app found. "
            "Install from source: brew install uv node, git clone the repo, "
            "build the dashboard, then `uv sync --extra mlx`."
        )
    if repo and not info["tools"]["uv"] and not any(o["mode"] == "venv" for o in opts):
        info["notes"].append(
            "A checkout exists but uv is not installed and there is no .venv. "
            "Install uv (brew install uv) then run `uv sync --extra mlx` in the repo."
        )
    if repo and not info.get("dashboard_built", True):
        info["notes"].append(
            "The web dashboard does not look built. Run: "
            "cd dashboard && npm install && npm run build"
        )
    return info


def pick_launch_option(detection: dict, preferred_mode: str = "auto") -> dict | None:
    opts = detection.get("launch_options") or []
    if not opts:
        return None
    if preferred_mode and preferred_mode != "auto":
        for o in opts:
            if o["mode"] == preferred_mode:
                return o
    return opts[0]


# --------------------------------------------------------------------------- #
# Building the environment for a run (local-models wiring)
# --------------------------------------------------------------------------- #

def build_run_env(host_cfg: dict) -> dict:
    env = dict(os.environ)
    if host_cfg.get("offline"):
        env["EXO_OFFLINE"] = "true"
        # Belt and suspenders: also tell HF libraries not to call home.
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
    if host_cfg.get("default_models_dir"):
        env["EXO_DEFAULT_MODELS_DIR"] = expand(host_cfg["default_models_dir"])
    if host_cfg.get("models_dirs"):
        env["EXO_MODELS_DIRS"] = ":".join(expand(d) for d in host_cfg["models_dirs"] if d)
    if host_cfg.get("read_only_dirs"):
        env["EXO_MODELS_READ_ONLY_DIRS"] = ":".join(
            expand(d) for d in host_cfg["read_only_dirs"] if d
        )
    if host_cfg.get("enable_image_models"):
        env["EXO_ENABLE_IMAGE_MODELS"] = "true"
    for k, v in (host_cfg.get("extra_env") or {}).items():
        if k:
            env[str(k)] = str(v)
    return env


def preview_command(host_cfg: dict, option: dict) -> str:
    """Human-readable command string, with exo-specific env vars shown."""
    shown = {}
    if host_cfg.get("offline"):
        shown["EXO_OFFLINE"] = "true"
    if host_cfg.get("default_models_dir"):
        shown["EXO_DEFAULT_MODELS_DIR"] = expand(host_cfg["default_models_dir"])
    if host_cfg.get("models_dirs"):
        shown["EXO_MODELS_DIRS"] = ":".join(expand(d) for d in host_cfg["models_dirs"] if d)
    if host_cfg.get("read_only_dirs"):
        shown["EXO_MODELS_READ_ONLY_DIRS"] = ":".join(
            expand(d) for d in host_cfg["read_only_dirs"] if d)
    if host_cfg.get("enable_image_models"):
        shown["EXO_ENABLE_IMAGE_MODELS"] = "true"
    for k, v in (host_cfg.get("extra_env") or {}).items():
        shown[str(k)] = str(v)

    parts = []
    cwd = option.get("cwd")
    if cwd:
        parts.append(f"cd {cwd} &&")
    for k, v in shown.items():
        parts.append(f"{k}={v}")
    parts.extend(option["cmd"])
    extra = (host_cfg.get("extra_flags") or "").strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Process supervision
# --------------------------------------------------------------------------- #

class ExoProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.log: list[str] = []
        self.started_at: float | None = None
        self.last_cmd: str = ""
        self.last_mode: str = ""

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _append(self, line: str):
        with self.lock:
            self.log.append(line.rstrip("\n"))
            if len(self.log) > LOG_CAP:
                del self.log[: len(self.log) - LOG_CAP]

    def _reader(self, stream):
        for raw in iter(stream.readline, ""):
            if raw == "":
                break
            self._append(raw)
        try:
            stream.close()
        except OSError:
            pass

    def start(self, option: dict, env: dict, extra_flags: str, pretty_cmd: str) -> tuple[bool, str]:
        with self.lock:
            if self.is_running():
                return False, "exo is already running (started by exoctl)."
        if is_port_open("127.0.0.1", EXO_PORT):
            return False, (f"Something is already listening on port {EXO_PORT}. "
                           "Stop it first, or it may be another exo instance.")
        cmd = list(option["cmd"])
        if extra_flags.strip():
            cmd.extend(extra_flags.split())
        self._append(f"[exoctl] launching: {pretty_cmd}")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=option.get("cwd") or None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                start_new_session=True,  # own process group so we can kill the tree
            )
        except (OSError, ValueError) as e:
            self._append(f"[exoctl] failed to launch: {e}")
            self.proc = None
            return False, f"Failed to launch: {e}"
        self.started_at = time.time()
        self.last_cmd = pretty_cmd
        self.last_mode = option["mode"]
        threading.Thread(target=self._reader, args=(self.proc.stdout,), daemon=True).start()
        return True, "Starting exo. Watch the log; the dashboard comes up on port 52415."

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if not self.is_running():
                self.proc = None
                return False, "exo is not running (not one started by exoctl)."
            p = self.proc
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                p.terminate()
            except OSError:
                pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self._append("[exoctl] stopped exo.")
        self.started_at = None
        return True, "Stopped exo."

    def snapshot(self) -> dict:
        running = self.is_running()
        return {
            "running": running,
            "pid": self.proc.pid if running else None,
            "exit_code": (self.proc.poll() if self.proc else None),
            "uptime_s": int(time.time() - self.started_at) if (running and self.started_at) else 0,
            "last_cmd": self.last_cmd,
            "last_mode": self.last_mode,
            "port_open": is_port_open("127.0.0.1", EXO_PORT),
        }

    def logs(self, since: int = 0) -> tuple[int, list[str]]:
        with self.lock:
            total = len(self.log)
            if since < 0:
                since = 0
            if since > total:
                since = total
            return total, self.log[since:]


EXO = ExoProcess()


# --------------------------------------------------------------------------- #
# Troubleshooter  (reads output, suggests the exact next command)
# --------------------------------------------------------------------------- #

# Each rule: (name, compiled regex, advice dict with cause + commands)
TROUBLE_RULES = [
    (
        "uv-missing",
        re.compile(r"(command not found:\s*uv|uv: command not found|No such file.*\buv\b)", re.I),
        {
            "cause": "The `uv` tool is not installed or not on PATH.",
            "commands": ["brew install uv",
                         "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc && source ~/.zshrc"],
        },
    ),
    (
        "node-missing",
        re.compile(r"(command not found:\s*n(ode|pm)|\bnpm: command not found|\bnode: command not found)", re.I),
        {
            "cause": "Node/npm is missing — needed to build exo's dashboard.",
            "commands": ["brew install node"],
        },
    ),
    (
        "exo-missing",
        re.compile(r"(command not found:\s*exo|No module named ['\"]?exo['\"]?|ModuleNotFoundError:.*\bexo\b)", re.I),
        {
            "cause": "exo isn't installed in the interpreter being used. Run it from the checkout via uv, or sync the env.",
            "commands": ["cd <exo-repo> && uv sync --extra mlx", "cd <exo-repo> && uv run exo"],
        },
    ),
    (
        "mlx-missing",
        re.compile(r"No module named ['\"]?mlx", re.I),
        {
            "cause": "The MLX backend (Apple-silicon inference) isn't installed.",
            "commands": ["cd <exo-repo> && uv sync --extra mlx"],
        },
    ),
    (
        "port-in-use",
        re.compile(r"(address already in use|errno 48|port\s*52415.*in use)", re.I),
        {
            "cause": "Port 52415 is already taken (likely another exo instance).",
            "commands": ["lsof -i :52415", "kill $(lsof -t -i :52415)"],
        },
    ),
    (
        "dashboard-missing",
        re.compile(r"(dashboard.*(not found|missing|dist)|ENOENT.*dashboard|Cannot find.*dashboard)", re.I),
        {
            "cause": "The web dashboard hasn't been built.",
            "commands": ["cd <exo-repo>/dashboard && npm install && npm run build"],
        },
    ),
    (
        "macmon",
        re.compile(r"macmon", re.I),
        {
            "cause": "exo wants its pinned macmon fork; Homebrew's macmon 0.6.1 is not recommended.",
            "commands": ["# Install the macmon version exo's README pins (not `brew install macmon`)",
                         "cat <exo-repo>/README.md | grep -i macmon"],
        },
    ),
    (
        "rust-nightly",
        re.compile(r"(nightly|rustup|cargo).*(not|error|fail)", re.I),
        {
            "cause": "A Rust nightly toolchain is required for part of the build.",
            "commands": ["rustup toolchain install nightly", "rustup default nightly"],
        },
    ),
    (
        "hf-offline",
        re.compile(r"(huggingface|hf\.co|download.*model|connection.*(refused|timed out)|Max retries|Failed to (resolve|connect))", re.I),
        {
            "cause": "exo is trying to reach Hugging Face. To run only local models, enable offline mode and point exo at your downloaded model folders.",
            "commands": ["# In exoctl: turn ON 'Offline mode' and set your local model dir(s)",
                         "EXO_OFFLINE=true EXO_MODELS_READ_ONLY_DIRS=<your-models-dir> uv run exo",
                         "curl http://localhost:52415/models?status=downloaded"],
        },
    ),
    (
        "model-not-found",
        re.compile(r"(model.*(not found|unknown|unavailable)|no such model|could not find model)", re.I),
        {
            "cause": "exo doesn't see that model locally. Confirm what it has downloaded, and that your local dir is on EXO_MODELS_READ_ONLY_DIRS.",
            "commands": ["curl http://localhost:52415/models?status=downloaded",
                         "# Ensure the folder layout matches what exo expects (e.g. mlx-community/<model>)"],
        },
    ),
    (
        "permission",
        re.compile(r"(permission denied|operation not permitted|EACCES)", re.I),
        {
            "cause": "A path isn't readable/writable by your user.",
            "commands": ["ls -la <path>", "chmod -R u+rwX <path>"],
        },
    ),
    (
        "disk-space",
        re.compile(r"(no space left on device|ENOSPC|disk.*full)", re.I),
        {
            "cause": "The target disk is full. Point model storage at a bigger volume.",
            "commands": ["df -h", "# In exoctl set 'Default models dir' to a volume with room"],
        },
    ),
    (
        "xcode-clt",
        re.compile(r"(xcrun.*error|xcode-select|command line tools)", re.I),
        {
            "cause": "Xcode command line tools are missing.",
            "commands": ["xcode-select --install"],
        },
    ),
]


def diagnose(text: str, repo_hint: str = "") -> list[dict]:
    """Return matched troubleshooting advice for a blob of output text."""
    hits = []
    seen = set()
    for name, rx, advice in TROUBLE_RULES:
        m = rx.search(text)
        if m and name not in seen:
            seen.add(name)
            cmds = [c.replace("<exo-repo>", repo_hint or "<exo-repo>") for c in advice["commands"]]
            hits.append({
                "rule": name,
                "matched": m.group(0)[:200],
                "cause": advice["cause"],
                "commands": cmds,
            })
    return hits


# --------------------------------------------------------------------------- #
# Local model scanning
# --------------------------------------------------------------------------- #

def scan_local_models(dirs: list[str]) -> dict:
    """Scan given dirs + known caches for things that look like model repos."""
    result = {"scanned": [], "found": []}
    search = []
    for d in dirs:
        if d:
            search.append(expand(d))
    for c in MODEL_DIR_CANDIDATES:
        e = expand(c)
        if e not in search:
            search.append(e)

    for base in search:
        p = Path(base)
        entry = {"dir": base, "exists": p.is_dir(), "models": []}
        result["scanned"].append(entry)
        if not p.is_dir():
            continue
        # Heuristics: HF hub layout "models--org--name", or org/name dirs with
        # a config.json / *.safetensors somewhere underneath.
        try:
            for child in sorted(p.iterdir()):
                if not child.is_dir():
                    continue
                name = child.name
                if name.startswith("models--"):
                    pretty = name[len("models--"):].replace("--", "/")
                    entry["models"].append(pretty)
                    result["found"].append({"dir": base, "model": pretty})
                else:
                    # org/name style: look one level deeper
                    for sub in child.iterdir() if child.is_dir() else []:
                        if sub.is_dir() and (
                            any(sub.glob("*.safetensors")) or (sub / "config.json").exists()
                        ):
                            pretty = f"{name}/{sub.name}"
                            entry["models"].append(pretty)
                            result["found"].append({"dir": base, "model": pretty})
                    if any(child.glob("*.safetensors")) or (child / "config.json").exists():
                        entry["models"].append(name)
                        result["found"].append({"dir": base, "model": name})
        except OSError:
            pass
    return result


def exo_downloaded_models() -> dict:
    """Ask exo itself what it considers downloaded (source of truth)."""
    if not is_port_open("127.0.0.1", EXO_PORT):
        return {"ok": False, "reason": "exo API not reachable on 52415"}
    try:
        data = http_get_json(f"http://127.0.0.1:{EXO_PORT}/models?status=downloaded")
        return {"ok": True, "data": data}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "reason": str(e)}


# --------------------------------------------------------------------------- #
# Cluster: multi-Mac mesh  (one panel managing every machine)
# --------------------------------------------------------------------------- #

def is_self_host(host: str) -> bool:
    if not host:
        return False
    if host in SELF_IPS or host in ("127.0.0.1", "localhost", "::1"):
        return True
    if host == BIND_HOST:
        return True
    try:
        for res in socket.getaddrinfo(host, None):
            if res[4][0] in SELF_IPS:
                return True
    except socket.gaierror:
        pass
    return False


def count_downloaded_models() -> int | None:
    r = exo_downloaded_models()
    if not r.get("ok"):
        return None
    data = r["data"]
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("models", "data", "downloaded"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def node_card() -> dict:
    """A compact status card for THIS machine, consumed by any hub."""
    cfg = load_config()
    hc = get_host_config(cfg)
    det = detect(hc.get("repo_path", ""))
    snap = EXO.snapshot()
    return {
        "hostname": HOSTNAME,
        "platform": sys.platform,
        "repo": det.get("repo"),
        "recommended_mode": det.get("recommended"),
        "saved_mode": hc.get("launch_mode") or "auto",
        "launch_options": [o["mode"] for o in det.get("launch_options", [])],
        "offline": bool(hc.get("offline")),
        "tools_ok": {k: bool(v) for k, v in det.get("tools", {}).items()},
        "exo_running": snap["running"],
        "exo_pid": snap["pid"],
        "exo_uptime_s": snap["uptime_s"],
        "exo_api_up": snap["port_open"],
        "models_downloaded": count_downloaded_models(),
        "panel_port": BIND_PORT,
        "bind_host": BIND_HOST,
        "notes": det.get("notes", []),
    }


def proxy_to_node(node: dict, sub_path: str, method: str = "GET",
                  body: bytes | None = None, token: str = "", timeout: float = 6.0):
    """Forward a request to another node's exoctl agent over the mesh."""
    url = f"http://{node['host']}:{node.get('panel_port', PANEL_PORT)}{sub_path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Exoctl-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def cluster_overview() -> dict:
    """Aggregate every registered node into one view (self handled locally)."""
    cfg = load_config()
    cl = get_cluster(cfg)
    token = cl.get("token", "")
    out = {"nodes": [], "token_set": bool(token)}
    for n in cl.get("nodes", []):
        row = {"name": n.get("name"), "host": n.get("host"),
               "panel_port": n.get("panel_port", PANEL_PORT),
               "is_self": is_self_host(n.get("host", "")),
               "reachable": False, "card": None, "error": None}
        try:
            if row["is_self"]:
                row["card"] = node_card()
                row["reachable"] = True
            else:
                status, raw = proxy_to_node(n, "/api/node", "GET", None, token, timeout=4.0)
                row["reachable"] = status == 200
                row["card"] = json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError, socket.timeout) as e:
            row["error"] = str(e)
        out["nodes"].append(row)
    return out


def suggested_self_node() -> dict:
    """Best guess for registering THIS machine (first non-loopback IP)."""
    ip = BIND_HOST
    if ip in ("127.0.0.1", "0.0.0.0", "localhost"):
        for cand in sorted(SELF_IPS):
            if "." in cand and not cand.startswith("127.") and not cand.startswith("169.254"):
                ip = cand
                break
    return {"name": HOSTNAME, "host": ip, "panel_port": BIND_PORT}


# Portable config fields that make sense to push to every machine. Deliberately
# excludes repo_path and launch_mode, which are per-machine (different installs).
PUSHABLE_FIELDS = ["offline", "default_models_dir", "models_dirs",
                   "read_only_dirs", "enable_image_models", "extra_flags", "extra_env"]


def push_config_to_nodes(portable: dict) -> dict:
    """Merge the portable settings into every OTHER reachable node's config."""
    cfg = load_config()
    cl = get_cluster(cfg)
    token = cl.get("token", "")
    results = []
    for n in cl.get("nodes", []):
        name, host = n.get("name"), n.get("host", "")
        if is_self_host(host):
            # apply locally, preserving repo_path/launch_mode
            hc = get_host_config(cfg)
            for k in PUSHABLE_FIELDS:
                if k in portable:
                    hc[k] = portable[k]
            set_host_config(cfg, hc)
            save_config(cfg)
            results.append({"node": name, "ok": True, "where": "local"})
            continue
        try:
            # fetch remote config, overlay portable fields, push back
            _, raw = proxy_to_node(n, "/api/config", "GET", None, token, timeout=5)
            remote = json.loads(raw.decode("utf-8", "replace"))
            for k in PUSHABLE_FIELDS:
                if k in portable:
                    remote[k] = portable[k]
            body = json.dumps(remote).encode()
            status, _ = proxy_to_node(n, "/api/config", "POST", body, token, timeout=6)
            results.append({"node": name, "ok": status == 200})
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError, socket.timeout) as e:
            results.append({"node": name, "ok": False, "error": str(e)})
    return {"results": results}


# --------------------------------------------------------------------------- #
# Thunderbolt / peer discovery
# --------------------------------------------------------------------------- #

def _mask_bits(hexmask: str) -> int:
    try:
        val = int(hexmask, 16)
        return bin(val).count("1")
    except ValueError:
        return 24


def bridge_interfaces() -> list[dict]:
    """Parse ifconfig for bridge* interfaces that carry an IPv4 address
    (this is the Thunderbolt/Ethernet bridge on macOS, e.g. bridge0 = 10.0.0.1)."""
    out = []
    try:
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    iface = None
    for line in txt.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":")[0]
        elif iface and iface.startswith("bridge") and "inet " in line:
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)(?: broadcast (\d+\.\d+\.\d+\.\d+))?", line)
            if m:
                out.append({"iface": iface, "ip": m.group(1),
                            "bits": _mask_bits(m.group(2)),
                            "broadcast": m.group(3) or ""})
    return out


def thunderbolt_devices() -> list[str]:
    """Names of devices reported on the Thunderbolt bus (informational)."""
    names = []
    try:
        txt = subprocess.run(["system_profiler", "SPThunderboltDataType"],
                             capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return names
    # Capture "Device Name: X" lines that are paired with a connected status.
    blocks = re.split(r"\n\s*\n", txt)
    for b in blocks:
        if "Device Name:" in b and "No device connected" not in b:
            for m in re.finditer(r"Device Name:\s*(.+)", b):
                nm = m.group(1).strip()
                if nm and nm not in names:
                    names.append(nm)
    return names


def _ping(ip: str) -> tuple[str, bool]:
    try:
        rc = subprocess.run(["ping", "-c1", "-t1", ip],
                            capture_output=True, timeout=2).returncode
        return ip, rc == 0
    except (OSError, subprocess.SubprocessError):
        return ip, False


def _arp_resolved(iface: str) -> list[str]:
    """ARP entries on this interface that have a REAL MAC (host actually seen).
    Excludes '(incomplete)' entries, which a sweep leaves behind for dead IPs."""
    ips = []
    try:
        txt = subprocess.run(["arp", "-a", "-i", iface], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ips
    for line in txt.splitlines():
        if "incomplete" in line:
            continue
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)", line)
        if m and ":" in m.group(2) and m.group(1) not in ips:
            ips.append(m.group(1))
    return ips


def discover_peers() -> dict:
    """Find Macs on the Thunderbolt bridge. A host counts only if it actually
    answered a ping, or already has a resolved ARP entry (real MAC)."""
    import concurrent.futures
    bridges = bridge_interfaces()
    result = {"bridges": bridges, "peers": [],
              "self_ips": sorted(i for i in SELF_IPS if "." in i)}
    seen = set()
    for br in bridges:
        base = ".".join(br["ip"].split(".")[:3])
        hosts = [f"{base}.{i}" for i in range(1, 255)]
        responders = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
            for ip, ok in ex.map(_ping, hosts):
                if ok:
                    responders.add(ip)
        responders.update(_arp_resolved(br["iface"]))  # hosts that block ICMP
        for nip in sorted(responders, key=lambda x: tuple(int(p) for p in x.split("."))):
            if nip in SELF_IPS or nip in seen or nip.endswith(".255") or nip.endswith(".0"):
                continue
            seen.add(nip)
            agent = is_port_open(nip, PANEL_PORT, timeout=0.3)
            exo_up = is_port_open(nip, EXO_PORT, timeout=0.3)
            name = ""
            if agent:
                try:
                    name = http_get_json(f"http://{nip}:{PANEL_PORT}/api/whoami", timeout=2).get("hostname", "")
                except (urllib.error.URLError, OSError, ValueError):
                    pass
            if not name:
                try:
                    name = socket.gethostbyaddr(nip)[0].split(".")[0]
                except (socket.herror, OSError):
                    name = ""
            result["peers"].append({
                "ip": nip, "iface": br["iface"], "hostname": name,
                "has_agent": agent, "exo_running": exo_up, "panel_port": PANEL_PORT,
            })
    return result


# --------------------------------------------------------------------------- #
# exo installer (runs the steps it safely can; guides the rest)
# --------------------------------------------------------------------------- #

def install_env() -> dict:
    """PATH augmented with the usual Homebrew / uv / pip locations so brew,
    node, npm, uv and git are found even under a GUI-launched Python."""
    env = dict(os.environ)
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             os.path.expanduser("~/.local/bin"),
             os.path.expanduser("~/Library/Python/3.9/bin"),
             os.path.expanduser("~/.cargo/bin")]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    return env


def _safe_repo(repo: str) -> str:
    repo = expand(repo or "~/exo")
    return repo


EXO_REPO_URL = "https://github.com/exo-explore/exo"


def install_steps(repo: str) -> list[dict]:
    """The ordered install plan for macOS. 'auto' steps the agent can run;
    'assisted' steps need a GUI/sudo/manual action and are shown, not run."""
    repo = _safe_repo(repo)
    parent = os.path.dirname(repo) or os.path.expanduser("~")
    dash = os.path.join(repo, "dashboard")

    def has(*names):
        return all(which(n) for n in names)

    def xcode_ok():
        try:
            return subprocess.run(["xcode-select", "-p"], capture_output=True,
                                  timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def repo_ok():
        return os.path.isfile(os.path.join(repo, "pyproject.toml"))

    def dash_ok():
        return any(os.path.isdir(os.path.join(dash, d)) for d in ("dist", "build", ".output", "out"))

    def sync_ok():
        return os.path.isdir(os.path.join(repo, ".venv"))

    brew_script = ('curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh '
                   '-o /tmp/exoctl_brew_install.sh && NONINTERACTIVE=1 /bin/bash /tmp/exoctl_brew_install.sh')
    uv_curl = "curl -LsSf https://astral.sh/uv/install.sh | sh"

    return [
        {"id": "xcode", "title": "Xcode Command Line Tools", "kind": "auto",
         "why": "Provides git and the compilers exo's build needs.",
         "satisfied": xcode_ok(),
         "run": [{"shell": "xcode-select --install || true",
                  "label": "xcode-select --install"}],
         "manual_cmds": ["xcode-select --install"],
         "note": "Triggers Apple's installer. If a dialog appears, click Install and "
                 "wait for it to finish, then re-check. (A headless re-run is a no-op.)"},
        {"id": "brew", "title": "Homebrew", "kind": "auto",
         "why": "Package manager used to install node (and optionally uv).",
         "satisfied": bool(which("brew")),
         "run": [{"shell": brew_script, "label": "install Homebrew (NONINTERACTIVE)"}],
         "manual_cmds": ['/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'],
         "note": "Runs Homebrew's official installer non-interactively. If it needs your "
                 "admin password it will stop — then run the shown command in Terminal once."},
        {"id": "uv", "title": "Install uv", "kind": "auto",
         "why": "uv runs exo and manages its Python environment.",
         "satisfied": bool(which("uv")),
         "run": ([{"argv": ["brew", "install", "uv"], "cwd": parent, "label": "brew install uv"}]
                 if which("brew") else
                 [{"shell": uv_curl, "label": "curl -LsSf https://astral.sh/uv/install.sh | sh"}]),
         "manual_cmds": ["brew install uv", uv_curl],
         "note": "Uses Homebrew if present, otherwise Astral's official install script."},
        {"id": "node", "title": "Install node", "kind": "auto",
         "why": "node builds exo's web dashboard.",
         "satisfied": bool(which("node")),
         "run": [{"argv": ["brew", "install", "node"], "cwd": parent, "label": "brew install node"}],
         "manual_cmds": ["brew install node"]},
        {"id": "clone", "title": "Clone the exo repository", "kind": "auto",
         "why": f"Source checkout at {repo}.",
         "satisfied": repo_ok(),
         "run": [{"argv": ["git", "clone", EXO_REPO_URL, repo], "cwd": parent,
                  "label": f"git clone → {repo}"}]},
        {"id": "dashboard", "title": "Build the web dashboard", "kind": "auto",
         "why": "exo serves its UI from the built dashboard.",
         "satisfied": dash_ok(),
         "run": [{"argv": ["npm", "install"], "cwd": dash, "label": "npm install (dashboard)"},
                 {"argv": ["npm", "run", "build"], "cwd": dash, "label": "npm run build (dashboard)"}]},
        {"id": "uv-sync", "title": "Install Python deps + MLX backend", "kind": "auto",
         "why": "uv sync --extra mlx sets up exo and Apple-silicon inference.",
         "satisfied": sync_ok(),
         "run": [{"argv": ["uv", "sync", "--extra", "mlx"], "cwd": repo,
                  "label": "uv sync --extra mlx"}]},
        {"id": "macmon", "title": "macmon (pinned fork)", "kind": "assisted",
         "why": "exo pins a specific macmon; Homebrew's 0.6.1 is not recommended.",
         "satisfied": False,
         "manual_cmds": [f"grep -i macmon {repo}/README.md"],
         "note": "Check the repo README for the exact macmon the current exo pins."},
    ]


def install_plan(repo: str) -> list[dict]:
    """Serializable plan (no callables) for the UI."""
    plan = []
    for s in install_steps(repo):
        plan.append({
            "id": s["id"], "title": s["title"], "kind": s["kind"], "why": s["why"],
            "satisfied": s["satisfied"], "note": s.get("note", ""),
            "manual_cmds": s.get("manual_cmds", []),
            "run_preview": [r["label"] for r in s.get("run", [])],
        })
    return plan


class InstallRunner:
    """Runs one install step's commands sequentially, streaming output."""
    def __init__(self):
        self.lock = threading.Lock()
        self.log: list[str] = []
        self.running = False
        self.current = ""
        self.results: dict[str, int] = {}

    def _append(self, line: str):
        with self.lock:
            self.log.append(line.rstrip("\n"))
            if len(self.log) > LOG_CAP:
                del self.log[: len(self.log) - LOG_CAP]

    def logs(self, since: int = 0):
        with self.lock:
            total = len(self.log)
            since = max(0, min(since, total))
            return total, self.log[since:], self.running, self.current, dict(self.results)

    def run_step(self, repo: str, step_id: str) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "An install step is already running."
            self.running = True
            self.current = step_id
        step = next((s for s in install_steps(repo) if s["id"] == step_id), None)
        if not step or step.get("kind") != "auto":
            self.running = False
            return False, "That step is manual — run the shown command yourself."
        threading.Thread(target=self._exec, args=(step,), daemon=True).start()
        return True, f"Running step '{step_id}'."

    def _exec(self, step: dict):
        env = install_env()
        rc = 0
        self._append(f"[exoctl] ── step: {step['title']} ──")
        for cmd in step.get("run", []):
            self._append(f"[exoctl] $ {cmd['label']}")
            try:
                cwd = cmd.get("cwd") or os.path.expanduser("~")
                os.makedirs(cwd, exist_ok=True)
                if "shell" in cmd:          # fixed, server-defined installer script
                    argv = ["/bin/bash", "-lc", cmd["shell"]]
                else:                       # argv with a user-controlled path (no shell)
                    argv = cmd["argv"]
                p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
                for line in iter(p.stdout.readline, ""):
                    if line == "":
                        break
                    self._append(line)
                p.wait()
                rc = p.returncode
            except (OSError, ValueError) as e:
                self._append(f"[exoctl] error: {e}")
                rc = 1
            if rc != 0:
                self._append(f"[exoctl] step failed (exit {rc}).")
                break
        if rc == 0:
            self._append(f"[exoctl] ✓ {step['title']} complete.")
        with self.lock:
            self.results[step["id"]] = rc
            self.running = False
            self.current = ""


INSTALL = InstallRunner()


# --------------------------------------------------------------------------- #
# Web panel
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>exoctl</title>
<style>
  :root{
    --bg:#0e1116; --panel:#171b22; --panel2:#1e242d; --line:#2a313c;
    --fg:#e6e9ee; --mut:#9aa4b2; --acc:#5b9dff; --ok:#49c17a; --warn:#e0a63b;
    --err:#e5604d; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;
    border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
  header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
  header .host{color:var(--mut);font-family:var(--mono);font-size:12px}
  .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
  .pill.on{background:rgba(73,193,122,.15);color:var(--ok)}
  .pill.off{background:rgba(229,96,77,.12);color:var(--err)}
  .wrap{max-width:1040px;margin:0 auto;padding:20px;display:grid;
    grid-template-columns:1fr 1fr;gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);
    margin:0 0 12px}
  .card.full{grid-column:1 / -1}
  label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
  input[type=text],textarea,select{width:100%;background:var(--panel2);color:var(--fg);
    border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px;
    font-family:var(--mono)}
  textarea{resize:vertical;min-height:70px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .chk{display:flex;align-items:center;gap:8px;margin-top:10px}
  .chk input{width:auto}
  button{background:var(--acc);color:#07101f;border:0;border-radius:8px;padding:9px 14px;
    font-weight:600;cursor:pointer;font-size:13px}
  button.sec{background:var(--panel2);color:var(--fg);border:1px solid var(--line)}
  button.danger{background:var(--err);color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .mut{color:var(--mut)}
  .mono{font-family:var(--mono)}
  pre{background:#0a0d12;border:1px solid var(--line);border-radius:8px;padding:12px;
    overflow:auto;max-height:360px;font-family:var(--mono);font-size:12px;white-space:pre-wrap;
    word-break:break-word}
  .cmd{background:#0a0d12;border:1px solid var(--line);border-radius:8px;padding:8px 10px;
    font-family:var(--mono);font-size:12px;margin:6px 0;display:flex;justify-content:space-between;gap:10px}
  .cmd code{color:#cfe3ff}
  .copy{background:transparent;border:1px solid var(--line);color:var(--mut);padding:2px 8px;font-size:11px}
  .opt{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer}
  .opt.sel{border-color:var(--acc);background:rgba(91,157,255,.08)}
  .opt .m{font-weight:600}
  .opt .w{color:var(--mut);font-size:12px}
  .adv{border-left:3px solid var(--warn);padding:8px 12px;margin:8px 0;background:var(--panel2);
    border-radius:0 8px 8px 0}
  .adv .c{color:var(--warn);font-weight:600;margin-bottom:4px}
  .tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:6px;
    padding:1px 7px;font-size:11px;margin:2px 4px 2px 0;font-family:var(--mono)}
  .toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel2);
    border:1px solid var(--line);padding:10px 16px;border-radius:10px;opacity:0;transition:.2s;z-index:9}
  .toast.show{opacity:1}
  .small{font-size:12px}
  a{color:var(--acc)}
  table.tbl{width:100%;border-collapse:collapse;font-size:13px}
  table.tbl th{text-align:left;color:var(--mut);font-weight:600;font-size:11px;
    text-transform:uppercase;letter-spacing:.4px;padding:6px 8px;border-bottom:1px solid var(--line)}
  table.tbl td{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle}
  table.tbl tr.self td{background:rgba(91,157,255,.05)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot.up{background:var(--ok)} .dot.down{background:var(--err)} .dot.idle{background:var(--mut)}
  .target-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;
    padding:10px 12px;background:var(--panel2);border:1px solid var(--line);border-radius:10px}
  .target-bar select{width:auto;min-width:200px}
  .mini{padding:5px 9px;font-size:12px}
  .nodeform{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:12px}
  .nodeform .f{display:flex;flex-direction:column}
  .nodeform input{width:auto}
  .badge{font-size:11px;padding:1px 7px;border-radius:6px;background:var(--panel2);border:1px solid var(--line)}
</style>
</head>
<body>
<header>
  <h1>exoctl</h1>
  <span class="host" id="host">—</span>
  <span class="pill off" id="statepill">checking…</span>
  <span class="host" id="portstate"></span>
  <span style="flex:1"></span>
  <a class="small" href="http://localhost:52415" target="_blank">open exo dashboard →</a>
</header>

<div class="wrap">

  <div class="card full">
    <h2>Cluster · all machines</h2>
    <div id="cluster">loading cluster…</div>
    <div class="nodeform">
      <div class="f"><label style="margin:0 0 4px">Name</label><input type="text" id="n_name" placeholder="macbook-air"></div>
      <div class="f"><label style="margin:0 0 4px">Host / IP</label><input type="text" id="n_host" placeholder="10.0.0.2"></div>
      <div class="f"><label style="margin:0 0 4px">Panel port</label><input type="text" id="n_port" placeholder="8577" style="width:90px"></div>
      <button class="mini" onclick="addNode()">+ Add machine</button>
      <button class="mini sec" onclick="addThisMachine()">Register this Mac</button>
    </div>
    <div class="row" style="margin-top:12px;align-items:flex-end">
      <div class="f" style="display:flex;flex-direction:column">
        <label style="margin:0 0 4px">Shared cluster token (optional — secures node-to-node control)</label>
        <input type="text" id="cl_token" placeholder="leave blank on a private Thunderbolt link" style="min-width:320px">
      </div>
      <button class="mini sec" onclick="saveToken()">Save token</button>
      <span class="small mut" id="tokstate"></span>
    </div>
  </div>

  <div class="card full">
    <h2>Discover · Macs on the Thunderbolt bridge</h2>
    <div class="row">
      <button onclick="discover()">🔍 Scan Thunderbolt link</button>
      <span id="discinfo" class="small mut"></span>
    </div>
    <div id="disc" style="margin-top:10px"></div>
  </div>

  <div class="card full">
    <div class="target-bar">
      <strong>Managing:</strong>
      <select id="target" onchange="switchTarget()"></select>
      <span id="targetinfo" class="small mut"></span>
      <span style="flex:1"></span>
      <span id="targetstate" class="pill off">—</span>
    </div>
  </div>

  <div class="card">
    <h2>1 · Selected machine</h2>
    <div id="detect" class="small mut">detecting…</div>
    <label>Launch method</label>
    <div id="opts"></div>
    <label>exo checkout path</label>
    <input type="text" id="repo_path" placeholder="(auto-detected)">
    <div class="row" style="margin-top:10px">
      <button class="sec" onclick="redetect()">Re-detect</button>
    </div>
  </div>

  <div class="card">
    <h2>2 · Local models (no Hugging Face)</h2>
    <div class="chk"><input type="checkbox" id="offline"><label for="offline" style="margin:0">Offline mode — run only local models (EXO_OFFLINE=true)</label></div>
    <label>Pre-downloaded model dir(s) — read-only (EXO_MODELS_READ_ONLY_DIRS)</label>
    <textarea id="read_only_dirs" placeholder="one path per line, e.g.&#10;~/.cache/huggingface/hub&#10;/Volumes/SSD/models"></textarea>
    <label>Default models dir (EXO_DEFAULT_MODELS_DIR)</label>
    <input type="text" id="default_models_dir" placeholder="~/.exo/models">
    <label>Extra writable dirs (EXO_MODELS_DIRS)</label>
    <textarea id="models_dirs" placeholder="one path per line (optional)"></textarea>
    <div class="row" style="margin-top:10px">
      <button class="sec" onclick="scanModels()">Scan disk for models</button>
      <button class="sec" onclick="exoModels()">Ask exo what's downloaded</button>
    </div>
    <div id="models" class="small" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <h2>3 · Options</h2>
    <label>Extra flags</label>
    <input type="text" id="extra_flags" placeholder="e.g. --no-worker">
    <div class="chk"><input type="checkbox" id="enable_image_models"><label for="enable_image_models" style="margin:0">Enable image models (EXO_ENABLE_IMAGE_MODELS)</label></div>
    <label>Extra env vars (KEY=VALUE per line)</label>
    <textarea id="extra_env" placeholder="HF_HUB_OFFLINE=1"></textarea>
    <div class="row" style="margin-top:12px">
      <button onclick="saveCfg()">Save config</button>
      <button class="sec" onclick="pushConfig()" title="Copy offline + model dirs + options to every registered machine (keeps each machine's own repo path & launch method)">⇪ Push to all machines</button>
    </div>
    <div id="pushmsg" class="small mut" style="margin-top:8px"></div>
  </div>

  <div class="card">
    <h2>4 · Run</h2>
    <label>Command preview</label>
    <pre id="preview" style="max-height:120px">—</pre>
    <div class="row">
      <button id="startbtn" onclick="startExo()">▶ Start exo</button>
      <button class="danger" onclick="stopExo()">■ Stop</button>
      <button class="sec" onclick="refreshStatus()">↻ Status</button>
    </div>
    <div id="runmsg" class="small mut" style="margin-top:10px"></div>
  </div>

  <div class="card full">
    <h2>Install exo · on the selected machine</h2>
    <p class="small mut">Targets <strong id="inst_target">this machine</strong>. Auto steps run here (including downloading Xcode CLT, Homebrew &amp; uv if missing); a couple may stop for your password and show the exact command.</p>
    <div class="row" style="margin-bottom:10px">
      <button onclick="loadInstall()" class="sec">↻ Check prerequisites</button>
      <button onclick="installAll()">⚙ Install everything (auto)</button>
      <span id="inst_status" class="small mut"></span>
    </div>
    <div id="installsteps"></div>
    <pre id="instlog" style="max-height:260px;margin-top:10px">(install output appears here)</pre>
  </div>

  <div class="card full">
    <h2>5 · Live log</h2>
    <pre id="log">(no output yet)</pre>
    <div class="row">
      <button class="sec" onclick="runAdvisor()">Diagnose current log</button>
      <span class="mut small">Auto-diagnoses on errors while running.</span>
    </div>
    <div id="advice"></div>
  </div>

  <div class="card full">
    <h2>6 · Troubleshoot pasted terminal output</h2>
    <p class="small mut">Hit an error in your own terminal? Paste it here and I'll tell you the exact command to run.</p>
    <textarea id="paste" placeholder="paste terminal output / error here"></textarea>
    <div class="row" style="margin-top:8px"><button onclick="pasteAdvisor()">Analyze</button></div>
    <div id="pasteadvice"></div>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
let LOGPOS = 0, SELOPT = "auto", DETECTION = null;

function $(id){return document.getElementById(id)}
function toast(m){const t=$("toast");t.textContent=m;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),1800)}
async function api(path, opts){const r=await fetch(path,opts);return r.json()}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function copy(t){navigator.clipboard.writeText(t);toast("copied")}

function linesToArr(v){return v.split("\n").map(s=>s.trim()).filter(Boolean)}
function arrToLines(a){return (a||[]).join("\n")}

function renderAdvice(hits, el){
  if(!hits || !hits.length){el.innerHTML='<p class="small mut">No known issues matched.</p>';return}
  el.innerHTML = hits.map(h=>`
    <div class="adv">
      <div class="c">${esc(h.cause)}</div>
      <div class="small mut mono">matched: ${esc(h.matched)}</div>
      ${h.commands.map(c=>`<div class="cmd"><code>${esc(c)}</code>
        <button class="copy" onclick='copy(${JSON.stringify(c)})'>copy</button></div>`).join("")}
    </div>`).join("");
}

function renderOpts(d){
  DETECTION = d;
  $("host").textContent = d.hostname + "  ·  " + d.platform;
  const dd=$("detect");
  let html = `repo: <span class="mono">${esc(d.repo||"(none found)")}</span><br>`;
  html += "tools: " + Object.entries(d.tools).map(([k,v])=>
    `<span class="tag" style="color:${v?'var(--ok)':'var(--mut)'}">${k}${v?'✓':'✗'}</span>`).join("");
  if(d.notes && d.notes.length) html += d.notes.map(n=>`<div class="adv small">${esc(n)}</div>`).join("");
  dd.innerHTML = html;

  const opts=$("opts");
  if(!d.launch_options.length){opts.innerHTML='<p class="small mut">No launch method found on this machine.</p>';return}
  opts.innerHTML = d.launch_options.map(o=>`
    <div class="opt ${o.mode===SELOPT?'sel':''}" onclick="selOpt('${o.mode}')">
      <div class="m">${esc(o.label)}</div>
      <div class="w">${esc(o.why)}</div>
      <div class="w mono">${esc(o.cmd.join(' '))}</div>
    </div>`).join("");
}
function selOpt(m){SELOPT=m;renderOpts(DETECTION);updatePreview()}

// ---- target routing: every scoped call hits the selected machine ----------
let TARGET="local";            // "local" or a registered node name
let WHOAMI=null;
function proxyNode(name, path, opts={}){
  const method=(opts.method||'GET').toUpperCase();
  const purl='/api/proxy?node='+encodeURIComponent(name)+'&path='+encodeURIComponent(path)+'&method='+method;
  return fetch(purl,{method:'POST',headers:{'Content-Type':'application/json'},body:opts.body||null}).then(r=>r.json());
}
function nodeApi(path, opts={}){
  if(TARGET==="local") return fetch(path,opts).then(r=>r.json());
  return proxyNode(TARGET, path, opts);
}

async function redetect(){
  const repo = $("repo_path").value.trim();
  const d = await nodeApi("/api/detect"+(repo?("?repo="+encodeURIComponent(repo)):""));
  if(SELOPT==="auto" && d.recommended) SELOPT=d.recommended;
  renderOpts(d); updatePreview();
}

// ---- cluster overview -----------------------------------------------------
function modeOf(c){return (c&&(c.saved_mode&&c.saved_mode!=="auto"?c.saved_mode:c.recommended))||"—"}
async function loadCluster(){
  let r; try{ r=await api("/api/cluster"); }catch(e){ return; }
  const el=$("cluster");
  const nodes=r.nodes||[];
  // keep the target dropdown in sync
  const sel=$("target"); const prev=sel.value||"local";
  sel.innerHTML='<option value="local">This machine (local)'+(WHOAMI?(" · "+WHOAMI.hostname):"")+'</option>'+
    nodes.map(n=>`<option value="${esc(n.name)}">${esc(n.name)} · ${esc(n.host)}</option>`).join("");
  sel.value=[...sel.options].some(o=>o.value===prev)?prev:"local";
  if(!nodes.length){el.innerHTML='<p class="small mut">No machines registered yet. Add one below (e.g. your MacBook Air at 10.0.0.2), then click “Register this Mac”.</p>';return}
  el.innerHTML=`<table class="tbl"><thead><tr>
    <th>Status</th><th>Machine</th><th>Address</th><th>Launch</th><th>Models</th><th>exo</th><th></th></tr></thead><tbody>`+
    nodes.map(n=>{
      const c=n.card||{};
      let dot='down',stat='offline';
      if(n.reachable){ if(c.exo_api_up){dot='up';stat='API up';}
        else if(c.exo_running){dot='idle';stat='booting';}
        else {dot='idle';stat='agent up';} }
      const md=(c.models_downloaded==null?'—':c.models_downloaded);
      const up=c.exo_running?`pid ${c.exo_pid} · ${c.exo_uptime_s}s`:'stopped';
      return `<tr class="${n.is_self?'self':''}">
        <td><span class="dot ${dot}"></span>${stat}${n.error?` <span class="mut small">(${esc(n.error).slice(0,40)})</span>`:''}</td>
        <td><strong>${esc(n.name)}</strong>${n.is_self?' <span class="badge">this Mac</span>':''}<br>
            <span class="mut small">${esc(c.hostname||'')} ${esc(c.platform||'')}</span></td>
        <td class="mono small">${esc(n.host)}:${n.panel_port}</td>
        <td class="small">${esc(modeOf(c))}${c.offline?' <span class="badge">offline</span>':''}</td>
        <td class="small">${md}</td>
        <td class="small">${up}</td>
        <td class="row" style="gap:6px">
          <button class="mini sec" onclick="manage('${esc(n.name)}')">Manage</button>
          <button class="mini" onclick="rowStart('${esc(n.name)}')" ${n.reachable?'':'disabled'}>Start</button>
          <button class="mini danger" onclick="rowStop('${esc(n.name)}')" ${(n.reachable&&c.exo_running)?'':'disabled'}>Stop</button>
          <button class="mini sec" onclick="rmNode('${esc(n.name)}')">✕</button>
        </td></tr>`;
    }).join("")+`</tbody></table>`;
}
async function rowStart(name){ const r=await proxyNode(name,'/api/start',{method:'POST',body:'{}'});
  toast(r.ok?name+": starting…":(r.error||"failed")); setTimeout(loadCluster,800); }
async function rowStop(name){ const r=await proxyNode(name,'/api/stop',{method:'POST'});
  toast(r.message||r.error||""); setTimeout(loadCluster,600); }
function manage(name){ $("target").value=name; switchTarget(); window.scrollTo({top:$("target").getBoundingClientRect().top+window.scrollY-60,behavior:'smooth'}); }

async function addNode(){
  const name=$("n_name").value.trim(), host=$("n_host").value.trim(),
        port=parseInt($("n_port").value.trim()||"8577",10);
  if(!name||!host){toast("name and host required");return}
  const r=await api("/api/nodes",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:"add",name,host,panel_port:port})});
  if(r.ok){$("n_name").value="";$("n_host").value="";$("n_port").value="";toast("added "+name);loadCluster();}
}
async function addThisMachine(){
  const w=WHOAMI||await api("/api/whoami"); const s=w.suggested_node;
  const r=await api("/api/nodes",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:"add",name:s.name,host:s.host,panel_port:s.panel_port})});
  if(r.ok){toast("registered "+s.name+" @ "+s.host);loadCluster();}
}
async function rmNode(name){
  const r=await api("/api/nodes",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:"remove",name})});
  if(r.ok){toast("removed "+name);loadCluster();}
}
async function saveToken(){
  const r=await api("/api/nodes",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:"token",token:$("cl_token").value.trim()})});
  $("tokstate").textContent=r.token_set?"token set":"no token (open on private link)";
  toast("token saved");
}

function switchTarget(){
  TARGET=$("target").value;
  SELOPT="auto"; LOGPOS=0; $("log").textContent="(no output yet)";
  $("advice").innerHTML=""; INSTLOGPOS=0; $("instlog").textContent="(install output appears here)";
  $("targetinfo").textContent = TARGET==="local" ? "direct to this machine" : "via mesh → "+TARGET;
  $("inst_target").textContent = TARGET==="local" ? "this machine" : TARGET;
  loadTargetView();
}
async function loadTargetView(){
  const cfg=await nodeApi("/api/config"); fillCfg(cfg);
  const d=await nodeApi("/api/detect");
  if((!SELOPT||SELOPT==="auto") && d.recommended) SELOPT=d.recommended;
  renderOpts(d); updatePreview(); refreshStatus(); loadInstall();
}

// ---- Thunderbolt discovery -----------------------------------------------
async function discover(){
  $("discinfo").textContent="scanning the bridge subnet (a few seconds)…";
  $("disc").innerHTML="";
  let r; try{ r=await api("/api/discover"); }catch(e){ $("discinfo").textContent="scan failed"; return; }
  const br=(r.bridges||[]).map(b=>b.iface+" "+b.ip+"/"+b.bits).join(", ")||"no bridge interface found";
  $("discinfo").textContent="bridge: "+br;
  const peers=r.peers||[];
  if(!peers.length){$("disc").innerHTML='<p class="small mut">No peer Macs found on the Thunderbolt link. Make sure the other Mac is on and the bridge is active.</p>';return}
  $("disc").innerHTML='<table class="tbl"><thead><tr><th>IP</th><th>Name</th><th>exoctl agent</th><th>exo</th><th></th></tr></thead><tbody>'+
    peers.map(p=>{
      const boot='python3 &lt;(curl -fsSL http://'+esc(r.bridges[0].ip)+':'+(WHOAMI?WHOAMI.panel_port:8577)+'/exoctl.py) ui --host '+esc(p.ip);
      return `<tr>
        <td class="mono">${esc(p.ip)}</td>
        <td>${esc(p.hostname||'—')}</td>
        <td>${p.has_agent?'<span class="dot up"></span>running':'<span class="dot down"></span>not running'}</td>
        <td>${p.exo_running?'up':'—'}</td>
        <td class="row" style="gap:6px">
          <button class="mini" onclick="addPeer('${esc(p.hostname||p.ip)}','${esc(p.ip)}')">+ Add as machine</button>
          ${p.has_agent?'':`<button class="mini sec" onclick='showBoot(${JSON.stringify(boot)})'>Bootstrap cmd</button>`}
        </td></tr>`;
    }).join("")+'</tbody></table><div id="bootcmd"></div>';
}
function showBoot(cmd){
  $("bootcmd").innerHTML='<p class="small mut" style="margin-top:10px">Run this on that Mac once (installs nothing, just starts the agent so you can manage &amp; install remotely):</p>'+
    `<div class="cmd"><code>${esc(cmd)}</code><button class="copy" onclick='copy(${JSON.stringify(cmd)})'>copy</button></div>`;
}
async function addPeer(name, ip){
  name=(name||ip).replace(/[^A-Za-z0-9_.-]/g,"-");
  const r=await api("/api/nodes",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:"add",name,host:ip,panel_port:(WHOAMI?WHOAMI.panel_port:8577)})});
  if(r.ok){toast("added "+name+" @ "+ip);loadCluster();}
}

// ---- push config to all machines -----------------------------------------
async function pushConfig(){
  const c=collectCfg();
  const portable={offline:c.offline,default_models_dir:c.default_models_dir,models_dirs:c.models_dirs,
    read_only_dirs:c.read_only_dirs,enable_image_models:c.enable_image_models,
    extra_flags:c.extra_flags,extra_env:c.extra_env};
  $("pushmsg").textContent="pushing…";
  const r=await api("/api/push-config",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify(portable)});
  $("pushmsg").innerHTML=(r.results||[]).map(x=>
    `${x.ok?'✓':'✗'} ${esc(x.node)}${x.where?' ('+x.where+')':''}${x.error?' — '+esc(x.error).slice(0,50):''}`).join('<br>')||"no other machines";
  toast("config pushed");
}

// ---- install wizard (targets the selected machine) ------------------------
let INSTLOGPOS=0, INSTALLING=false;
async function loadInstall(){
  const repo=$("repo_path").value.trim();
  let r; try{ r=await nodeApi("/api/install/plan"+(repo?("?repo="+encodeURIComponent(repo)):"")); }catch(e){ return; }
  const el=$("installsteps");
  el.innerHTML=(r.steps||[]).map(s=>{
    const badge=s.satisfied?'<span class="badge" style="color:var(--ok)">installed</span>':
      (s.kind==="auto"?'<span class="badge" style="color:var(--warn)">needed</span>':'<span class="badge">manual</span>');
    const cmds=(s.satisfied?[]:(s.kind==="auto"?s.run_preview:s.manual_cmds)).map(c=>
      `<div class="cmd"><code>${esc(c)}</code><button class="copy" onclick='copy(${JSON.stringify(c)})'>copy</button></div>`).join("");
    const btn=(!s.satisfied && s.kind==="auto")?`<button class="mini" onclick="runStep('${s.id}')">Run</button>`:'';
    return `<div class="opt" style="cursor:default">
      <div class="row" style="justify-content:space-between">
        <div class="m">${esc(s.title)} ${badge}</div>${btn}</div>
      <div class="w">${esc(s.why)}</div>
      ${s.note?`<div class="w mut">${esc(s.note)}</div>`:''}
      ${cmds}</div>`;
  }).join("");
}
async function runStep(id){
  const repo=$("repo_path").value.trim();
  const r=await nodeApi("/api/install/run",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({step:id,repo})});
  if(!r.ok){toast(r.message||"cannot run");return false}
  toast("running "+id); return await waitInstall();
}
async function waitInstall(){
  // poll install log until the running step finishes
  return new Promise(resolve=>{
    const iv=setInterval(async ()=>{
      let r; try{ r=await nodeApi("/api/install/log?since="+INSTLOGPOS);}catch(e){return}
      if(r.lines&&r.lines.length){INSTLOGPOS=r.total;const el=$("instlog");
        el.textContent=(el.textContent==="(install output appears here)"?"":el.textContent)+r.lines.join("\n")+"\n";
        el.scrollTop=el.scrollHeight;}
      $("inst_status").textContent=r.running?("running "+r.current+"…"):"";
      if(!r.running){clearInterval(iv);loadInstall();resolve(true);}
    },1000);
  });
}
async function installAll(){
  if(INSTALLING)return; INSTALLING=true; $("inst_status").textContent="starting…";
  const repo=$("repo_path").value.trim();
  const plan=await nodeApi("/api/install/plan"+(repo?("?repo="+encodeURIComponent(repo)):""));
  for(const s of (plan.steps||[])){
    if(s.satisfied||s.kind!=="auto") continue;
    $("inst_status").textContent="installing "+s.id+"…";
    const ok=await runStep(s.id);
    // re-check this step; if still not satisfied, stop and let the user act
    const re=await nodeApi("/api/install/plan"+(repo?("?repo="+encodeURIComponent(repo)):""));
    const st=(re.steps||[]).find(x=>x.id===s.id);
    if(st && !st.satisfied){ $("inst_status").textContent="stopped at "+s.id+" — see output / command above"; INSTALLING=false; loadInstall(); return; }
  }
  $("inst_status").textContent="all auto steps done ✓"; INSTALLING=false; loadInstall();
}

function collectCfg(){
  return {
    repo_path: $("repo_path").value.trim(),
    launch_mode: SELOPT,
    offline: $("offline").checked,
    read_only_dirs: linesToArr($("read_only_dirs").value),
    default_models_dir: $("default_models_dir").value.trim(),
    models_dirs: linesToArr($("models_dirs").value),
    enable_image_models: $("enable_image_models").checked,
    extra_flags: $("extra_flags").value.trim(),
    extra_env: Object.fromEntries(linesToArr($("extra_env").value)
      .map(l=>{const i=l.indexOf("=");return i<0?null:[l.slice(0,i).trim(),l.slice(i+1).trim()]})
      .filter(Boolean))
  };
}
function fillCfg(c){
  $("repo_path").value=c.repo_path||"";
  $("offline").checked=!!c.offline;
  $("read_only_dirs").value=arrToLines(c.read_only_dirs);
  $("default_models_dir").value=c.default_models_dir||"";
  $("models_dirs").value=arrToLines(c.models_dirs);
  $("enable_image_models").checked=!!c.enable_image_models;
  $("extra_flags").value=c.extra_flags||"";
  $("extra_env").value=Object.entries(c.extra_env||{}).map(([k,v])=>k+"="+v).join("\n");
  if(c.launch_mode) SELOPT=c.launch_mode;
}

let previewTimer=null;
function updatePreview(){
  clearTimeout(previewTimer);
  previewTimer=setTimeout(async ()=>{
    const r = await nodeApi("/api/preview",{method:"POST",headers:{'Content-Type':'application/json'},
      body:JSON.stringify(collectCfg())});
    $("preview").textContent = r.command || r.error || "—";
  },150);
}
["repo_path","read_only_dirs","default_models_dir","models_dirs","extra_flags","extra_env"].forEach(id=>
  $(id).addEventListener("input",updatePreview));
["offline","enable_image_models"].forEach(id=>$(id).addEventListener("change",updatePreview));

async function saveCfg(){
  const r=await nodeApi("/api/config",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify(collectCfg())});
  toast(r.ok?"config saved":"save failed");
}
async function startExo(){
  await saveCfg();
  const r=await nodeApi("/api/start",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify(collectCfg())});
  $("runmsg").textContent=r.message||r.error||"";
  toast(r.ok?"starting…":"could not start");
  setTimeout(loadCluster,900);
}
async function stopExo(){
  const r=await nodeApi("/api/stop",{method:"POST"});
  $("runmsg").textContent=r.message||r.error||"";
  setTimeout(loadCluster,600);
}
async function refreshStatus(){
  let s; try{ s=await nodeApi("/api/status"); }catch(e){ return; }
  const label=(TARGET==="local"?"this machine":TARGET);
  [["statepill"],["targetstate"]].forEach(([id])=>{
    const pill=$(id); if(!pill) return;
    pill.className="pill "+(s.running?"on":"off");
    pill.textContent=(s.running?("running · pid "+s.pid+" · "+s.uptime_s+"s"):"stopped");
  });
  $("portstate").textContent = s.port_open? "exo API up (52415)" : (s.running? "booting…" : "");
  $("targetinfo").textContent = (TARGET==="local"?"direct to this machine":"via mesh → "+TARGET)+
    (s.port_open?" · exo API up":"");
  $("startbtn").disabled = s.running;
}
async function pollLog(){
  let r; try{ r=await nodeApi("/api/logs?since="+LOGPOS); }catch(e){ return; }
  if(r.lines && r.lines.length){
    LOGPOS=r.total;
    const el=$("log");
    const atBottom = el.scrollHeight-el.scrollTop-el.clientHeight < 40;
    el.textContent = (el.textContent==="(no output yet)"?"":el.textContent)+r.lines.join("\n")+"\n";
    if(atBottom) el.scrollTop=el.scrollHeight;
    if(r.auto_advice && r.auto_advice.length) renderAdvice(r.auto_advice,$("advice"));
  }
}
async function runAdvisor(){
  const r=await nodeApi("/api/advice");
  renderAdvice(r.hits,$("advice"));
}
async function pasteAdvisor(){
  const r=await nodeApi("/api/diagnose",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:$("paste").value})});
  renderAdvice(r.hits,$("pasteadvice"));
}
async function scanModels(){
  $("models").innerHTML="scanning…";
  const r=await nodeApi("/api/scan",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dirs:linesToArr($("read_only_dirs").value).concat(linesToArr($("models_dirs").value))})});
  let html="";
  (r.scanned||[]).forEach(s=>{
    html+=`<div class="mono small" style="margin:4px 0">${s.exists?'📁':'🚫'} ${esc(s.dir)} `+
      `<span class="mut">(${s.models.length})</span></div>`;
    if(s.models.length) html+='<div>'+s.models.map(m=>`<span class="tag">${esc(m)}</span>`).join("")+'</div>';
  });
  $("models").innerHTML=html||"nothing found";
}
async function exoModels(){
  $("models").innerHTML="asking exo…";
  const r=await nodeApi("/api/exomodels");
  if(!r.ok){$("models").innerHTML='<span class="mut">'+esc(r.reason||"unavailable")+'</span>';return}
  $("models").innerHTML='<pre class="small">'+esc(JSON.stringify(r.data,null,2)).slice(0,4000)+'</pre>';
}

async function boot(){
  try{ WHOAMI=await api("/api/whoami"); }catch(e){}
  if(WHOAMI){ $("tokstate").textContent=WHOAMI.token_set?"token set":"no token (open on private link)";
    $("n_port").placeholder=String(WHOAMI.panel_port||8577); }
  await loadCluster();
  await loadTargetView();          // defaults to local
  setInterval(pollLog,1200);
  setInterval(refreshStatus,2500);
  setInterval(loadCluster,4000);
}
boot();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "exoctl/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _trusted(self) -> bool:
        """Local browser / loopback is always trusted. A peer node calling in
        over the mesh must present the shared token when one is configured."""
        ip = self._client_ip()
        if ip in SELF_IPS or ip.startswith("127.") or ip in ("::1", "localhost"):
            return True
        token = get_cluster(load_config()).get("token", "")
        if not token:
            return True  # no token set: private-link trust (user's choice)
        return self.headers.get("X-Exoctl-Token", "") == token

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/exoctl.py":
            # Bootstrap: a peer can `curl http://<hub>:8577/exoctl.py -o exoctl.py`.
            try:
                src = Path(__file__).read_text()
            except OSError:
                src = "# source unavailable"
            self._send(200, src.encode("utf-8"), "text/x-python; charset=utf-8")
            return

        if path.startswith("/api/") and not self._trusted():
            self._json({"error": "forbidden: bad or missing cluster token"}, 403)
            return

        if path == "/api/whoami":
            cl = get_cluster(load_config())
            self._json({"hostname": HOSTNAME, "bind_host": BIND_HOST,
                        "panel_port": BIND_PORT, "self_ips": sorted(SELF_IPS),
                        "token_set": bool(cl.get("token")),
                        "suggested_node": suggested_self_node()})
            return
        if path == "/api/node":
            self._json(node_card())
            return
        if path == "/api/cluster":
            self._json(cluster_overview())
            return
        if path == "/api/nodes":
            self._json({"nodes": get_cluster(load_config()).get("nodes", []),
                        "token_set": bool(get_cluster(load_config()).get("token"))})
            return
        if path == "/api/detect":
            repo = (qs.get("repo") or [""])[0]
            self._json(detect(repo))
            return
        if path == "/api/config":
            cfg = load_config()
            self._json(get_host_config(cfg))
            return
        if path == "/api/status":
            self._json(EXO.snapshot())
            return
        if path == "/api/logs":
            since = int((qs.get("since") or ["0"])[0])
            total, lines = EXO.logs(since)
            out = {"total": total, "lines": lines}
            # auto-diagnose only when the process is running and produced lines
            if lines:
                cfg = get_host_config(load_config())
                hits = diagnose("\n".join(lines[-200:]), cfg.get("repo_path", ""))
                if hits:
                    out["auto_advice"] = hits
            self._json(out)
            return
        if path == "/api/advice":
            cfg = get_host_config(load_config())
            _, lines = EXO.logs(0)
            hits = diagnose("\n".join(lines[-500:]), cfg.get("repo_path", ""))
            self._json({"hits": hits})
            return
        if path == "/api/exomodels":
            self._json(exo_downloaded_models())
            return
        if path == "/api/discover":
            self._json(discover_peers())
            return
        if path == "/api/install/plan":
            repo = (qs.get("repo") or [""])[0]
            self._json({"repo": _safe_repo(repo), "steps": install_plan(repo)})
            return
        if path == "/api/install/log":
            since = int((qs.get("since") or ["0"])[0])
            total, lines, running, current, results = INSTALL.logs(since)
            self._json({"total": total, "lines": lines, "running": running,
                        "current": current, "results": results})
            return
        self._json({"error": "not found"}, 404)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        from urllib.parse import urlparse, unquote
        u = urlparse(self.path)
        path = u.path

        if path.startswith("/api/") and not self._trusted():
            self._json({"error": "forbidden: bad or missing cluster token"}, 403)
            return

        # Proxy: forward a sub-request to another node's agent over the mesh.
        # The browser only ever talks to its own agent; the agent fans out.
        if path == "/api/proxy":
            from urllib.parse import parse_qs
            q = parse_qs(u.query)
            node_name = (q.get("node") or [""])[0]
            sub_path = unquote((q.get("path") or [""])[0])
            method = (q.get("method") or ["GET"])[0].upper()
            cfg = load_config(); cl = get_cluster(cfg)
            node = next((n for n in cl.get("nodes", []) if n.get("name") == node_name), None)
            if not node:
                self._json({"error": f"unknown node '{node_name}'"}, 404)
                return
            raw = None
            n = int(self.headers.get("Content-Length", "0"))
            if n > 0:
                raw = self.rfile.read(n)
            try:
                if is_self_host(node.get("host", "")):
                    # Loopback shortcut: re-enter our own handlers over HTTP-local.
                    status, data = proxy_to_node(
                        {"host": "127.0.0.1", "panel_port": BIND_PORT},
                        sub_path, method, raw, cl.get("token", ""))
                else:
                    status, data = proxy_to_node(node, sub_path, method, raw, cl.get("token", ""))
                self._send(status, data, "application/json")
            except (urllib.error.URLError, OSError, socket.timeout) as e:
                self._json({"error": f"node '{node_name}' unreachable: {e}"}, 502)
            return

        body = self._read_body()

        if path == "/api/nodes":
            cfg = load_config(); cl = get_cluster(cfg)
            action = body.get("action")
            if action == "add":
                name = (body.get("name") or "").strip()
                host = (body.get("host") or "").strip()
                port = int(body.get("panel_port") or PANEL_PORT)
                if not name or not host:
                    self._json({"ok": False, "error": "name and host are required"})
                    return
                cl["nodes"] = [n for n in cl["nodes"] if n.get("name") != name]
                cl["nodes"].append({"name": name, "host": host, "panel_port": port})
                save_config(cfg)
                self._json({"ok": True, "nodes": cl["nodes"]})
                return
            if action == "remove":
                name = (body.get("name") or "").strip()
                cl["nodes"] = [n for n in cl["nodes"] if n.get("name") != name]
                save_config(cfg)
                self._json({"ok": True, "nodes": cl["nodes"]})
                return
            if action == "token":
                cl["token"] = (body.get("token") or "").strip()
                save_config(cfg)
                self._json({"ok": True, "token_set": bool(cl["token"])})
                return
            self._json({"ok": False, "error": "unknown action"})
            return

        if path == "/api/config":
            cfg = load_config()
            hc = host_defaults()
            hc.update(body)
            set_host_config(cfg, hc)
            save_config(cfg)
            self._json({"ok": True})
            return

        if path == "/api/preview":
            det = detect(body.get("repo_path", ""))
            opt = pick_launch_option(det, body.get("launch_mode", "auto"))
            if not opt:
                self._json({"error": "No launch method detected on this machine. "
                                     "See 'This machine' for how to install."})
                return
            self._json({"command": preview_command(body, opt), "mode": opt["mode"]})
            return

        if path == "/api/start":
            cfg = load_config()
            hc = get_host_config(cfg)  # start from this machine's saved config
            hc.update(body)            # panel sends a full body; row-start sends {}
            set_host_config(cfg, hc); save_config(cfg)
            det = detect(hc.get("repo_path", ""))
            opt = pick_launch_option(det, hc.get("launch_mode", "auto"))
            if not opt:
                self._json({"ok": False,
                            "error": "No way to launch exo found on this machine."})
                return
            env = build_run_env(hc)
            pretty = preview_command(hc, opt)
            ok, msg = EXO.start(opt, env, hc.get("extra_flags", ""), pretty)
            self._json({"ok": ok, "message": msg if ok else None, "error": None if ok else msg})
            return

        if path == "/api/stop":
            ok, msg = EXO.stop()
            self._json({"ok": ok, "message": msg if ok else None, "error": None if ok else msg})
            return

        if path == "/api/scan":
            self._json(scan_local_models(body.get("dirs", [])))
            return

        if path == "/api/diagnose":
            cfg = get_host_config(load_config())
            hits = diagnose(body.get("text", ""), cfg.get("repo_path", ""))
            self._json({"hits": hits})
            return

        if path == "/api/push-config":
            # body = the portable settings to replicate to every node
            portable = {k: body[k] for k in PUSHABLE_FIELDS if k in body}
            self._json(push_config_to_nodes(portable))
            return

        if path == "/api/install/run":
            ok, msg = INSTALL.run_step(body.get("repo", ""), body.get("step", ""))
            self._json({"ok": ok, "message": msg})
            return

        self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# CLI entrypoints
# --------------------------------------------------------------------------- #

def cmd_ui(argv):
    global BIND_HOST, BIND_PORT
    port = PANEL_PORT
    host = os.environ.get("EXOCTL_BIND_HOST", "127.0.0.1")
    if "--panel-port" in argv:
        port = int(argv[argv.index("--panel-port") + 1])
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    no_open = "--no-open" in argv
    BIND_HOST, BIND_PORT = host, port
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}"
    print(f"exoctl control panel → {url}")
    print(f"(this host: {HOSTNAME}; bound to {host}:{port}; exo API expected on {EXO_PORT})")
    if host in ("127.0.0.1", "localhost"):
        print("note: bound to loopback — other Macs can't reach this agent. "
              "Use --host <thunderbolt-ip> (e.g. 10.0.0.1) to join the cluster.")
    try:
        if sys.platform == "darwin" and not no_open:
            subprocess.Popen(["open", url])
    except OSError:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down panel.")
        if EXO.is_running():
            print("note: exo is still running (started by exoctl). Use `stop` to end it.")


def cmd_detect(argv):
    repo = argv[0] if argv else ""
    print(json.dumps(detect(repo), indent=2))


def cmd_start(argv):
    cfg = load_config()
    hc = get_host_config(cfg)
    det = detect(hc.get("repo_path", ""))
    opt = pick_launch_option(det, hc.get("launch_mode", "auto"))
    if not opt:
        print("No launch method detected. Run `python3 exoctl.py detect`.")
        sys.exit(1)
    env = build_run_env(hc)
    pretty = preview_command(hc, opt)
    print("launching:", pretty)
    cmd = list(opt["cmd"])
    if hc.get("extra_flags", "").strip():
        cmd.extend(hc["extra_flags"].split())
    os.execvpe(cmd[0], cmd, env) if opt.get("cwd") in (None, os.getcwd()) else _run_in(opt, cmd, env)


def _run_in(opt, cmd, env):
    os.chdir(opt["cwd"])
    os.execvpe(cmd[0], cmd, env)


def cmd_stop(argv):
    print("Note: `stop` only works within the running panel process. "
          "To stop exo manually: kill $(lsof -t -i :52415)")


def cmd_status(argv):
    print(json.dumps({
        "exo_api_up": is_port_open("127.0.0.1", EXO_PORT),
        "process": EXO.snapshot(),
    }, indent=2))


def cmd_diagnose(argv):
    if not argv:
        print("usage: exoctl.py diagnose <logfile>")
        sys.exit(1)
    text = Path(argv[0]).read_text(errors="replace")
    hc = get_host_config(load_config())
    for h in diagnose(text, hc.get("repo_path", "")):
        print(f"\n● {h['cause']}")
        print(f"  matched: {h['matched']}")
        for c in h["commands"]:
            print(f"    $ {c}")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "ui"
    rest = args[1:] if (args and not args[0].startswith("-")) else args
    table = {
        "ui": cmd_ui, "detect": cmd_detect, "start": cmd_start,
        "stop": cmd_stop, "status": cmd_status, "diagnose": cmd_diagnose,
    }
    fn = table.get(cmd)
    if not fn:
        print(__doc__)
        sys.exit(1)
    fn(rest)


if __name__ == "__main__":
    main()
