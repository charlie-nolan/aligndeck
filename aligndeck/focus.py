"""
Full-compute mode: find the programs using CPU, GPU or NPU time and PAUSE the ones you tick (SIGSTOP), so the
alignment gets the whole machine. Nothing is paused without your OK, nothing is killed, and everything paused is
resumed (SIGCONT) when you press RESUME or close the app. If the app ever dies with programs still paused, the next
start resumes them, or run:  python -m aligndeck --resume

Only your own user's programs are listed (others need root). The desktop, audio, network, ssh, Tailscale and this
app itself are never offered.
"""
import json
import os
import shutil
import signal
import subprocess
import time

KEEP = {"systemd", "dbus-daemon", "dbus-broker", "sshd", "sshd-session", "ssh-agent", "gpg-agent", "Xorg", "Xwayland",
        "kwin_x11", "kwin_wayland", "plasmashell", "ksmserver", "kded5", "kded6", "kglobalaccel", "krunner",
        "gnome-shell", "gnome-session-b", "mutter", "lxsession", "lxpanel", "openbox", "xfwm4", "xfce4-panel",
        "pipewire", "pipewire-pulse", "wireplumber", "pulseaudio", "tailscaled", "tailscale", "NetworkManager",
        "nm-applet", "at-spi-bus-laun", "at-spi2-registr", "xdg-desktop-por", "xdg-document-po", "xdg-permission-",
        "sddm", "lightdm", "gdm", "login", "agetty", "tmux: server", "tmux", "screen", "konsole", "gnome-terminal-",
        "xterm", "alacritty", "kitty", "zsh", "bash", "sh", "fish", "gphoto2", "sudo", "polkit-kde-auth",
        "polkitd", "upowerd", "udisksd", "bluetoothd", "Xvnc", "x11vnc", "wayvnc"}
HEAVY_CMD = ("rkllm",
             "ollama", "llama-server", "llama-cli")
HEAVY_COMM = ("ollama", "llama-server", "llama-cli", "tesseract", "ffmpeg", "baloo_file", "tracker-miner",
              "akonadi", "steam", "rustc", "cc1plus", "make", "ninja", "code")
BROWSERS = ("chromium", "chrome", "firefox", "brave", "falkon", "epiphany", "qutebrowser")


def _uid():
    return os.getuid()


def _proc_info(pid):
    """(comm, cmdline, uid, ppid, cpu ticks, starttime, rss MB) or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            st = f.read()
        comm = st[st.index("(") + 1:st.rindex(")")]
        rest = st[st.rindex(")") + 2:].split()
        ppid, ticks, start = int(rest[1]), int(rest[11]) + int(rest[12]), int(rest[19])
        rss = int(rest[21]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        uid = os.stat(f"/proc/{pid}").st_uid
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        return comm, cmd, uid, ppid, ticks, start, rss
    except (OSError, ValueError, IndexError):
        return None


def _pids():
    return [int(p) for p in os.listdir("/proc") if p.isdigit()]


def _protected_pids():
    """This app, its parents (terminal, ssh) and its children (gphoto2)."""
    keep, p = set(), os.getpid()
    while p > 1:
        keep.add(p)
        info = _proc_info(p)
        p = info[3] if info else 1
    for pid in _pids():
        info = _proc_info(pid)
        if info and info[3] == os.getpid():
            keep.add(pid)
    return keep


def gpu_pids():
    """{pid: MB} of programs on an NVIDIA GPU (PC), empty elsewhere."""
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return {int(a): float(b) for a, b in (l.split(",") for l in r.stdout.splitlines() if "," in l)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def npu_busy():
    """RK3588 NPU load line, or ''."""
    for p in ("/sys/kernel/debug/rknpu/load", "/proc/rknpu/load"):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


def scan(seconds=1.5, min_cpu=3.0):
    """Programs worth pausing: [{pid, name, cmd, cpu %, rss MB, gpu MB, tick (suggested), why, browser}]."""
    me, prot = _uid(), _protected_pids()
    a = {p: _proc_info(p) for p in _pids()}
    time.sleep(seconds)
    hz = os.sysconf("SC_CLK_TCK")
    gpu = gpu_pids()
    out = []
    for pid, i0 in a.items():
        i1 = _proc_info(pid)
        if not i0 or not i1 or i1[5] != i0[5] or pid in prot or i1[2] != me:
            continue
        comm, cmd = i1[0], i1[1]
        if pid == 1 or not cmd or comm in KEEP or comm.startswith(("kworker", "systemd")):
            continue
        cpu = 100.0 * (i1[4] - i0[4]) / hz / seconds
        heavy = any(h in cmd for h in HEAVY_CMD) or comm.lower().startswith(HEAVY_COMM)
        browser = any(b in comm.lower() for b in BROWSERS)
        if cpu < min_cpu and pid not in gpu and not heavy:
            continue
        why = []
        if cpu >= min_cpu:
            why.append(f"{cpu:.0f}% CPU")
        if pid in gpu:
            why.append(f"{gpu[pid]:.0f} MB on the GPU")
        if heavy and not why:
            why.append("known heavy program (idle right now)")
        out.append({"pid": pid, "name": comm, "cmd": cmd[:160], "cpu": round(cpu, 1), "rss": round(i1[6]),
                    "gpu": gpu.get(pid, 0), "why": ", ".join(why), "browser": browser,
                    "tick": (heavy or cpu >= 25 or pid in gpu) and not browser, "start": i1[5]})
    out.sort(key=lambda r: (-r["tick"], -r["cpu"], -r["gpu"]))
    return out


class Focus:
    def __init__(self, cache_dir):
        self.path = os.path.join(cache_dir, "paused.json")
        self.paused = []
        self.governor = {}
        self.notes = []
        self._load()
        if self.paused:
            n = self.resume()
            self.notes.append(f"resumed {n} program(s) a previous run had left paused")

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.paused, self.governor = d.get("paused", []), d.get("governor", {})
        except (OSError, ValueError):
            self.paused, self.governor = [], {}

    def _save(self):
        if self.paused or self.governor:
            with open(self.path, "w") as f:
                json.dump({"paused": self.paused, "governor": self.governor}, f)
        elif os.path.exists(self.path):
            os.remove(self.path)

    def pause(self, procs):
        """procs: [{pid, start}] from scan() that the user ticked."""
        done = []
        for p in procs:
            info = _proc_info(int(p["pid"]))
            if not info or info[5] != int(p["start"]) or info[2] != _uid():
                continue
            try:
                os.kill(int(p["pid"]), signal.SIGSTOP)
                self.paused.append({"pid": int(p["pid"]), "name": info[0], "start": info[5]})
                done.append(info[0])
            except OSError as e:
                self.notes.append(f"could not pause {info[0]}: {e}")
        self._save()
        return done

    def resume(self):
        n = 0
        for p in self.paused:
            info = _proc_info(p["pid"])
            if info and info[5] == p["start"]:
                try:
                    os.kill(p["pid"], signal.SIGCONT)
                    n += 1
                except OSError:
                    pass
        self.paused = []
        self._restore_governor()
        self._save()
        return n

    def performance_governor(self):
        """Set every CPU to the 'performance' governor if we may write it; otherwise return the command to run."""
        pols = sorted(p for p in (os.path.join("/sys/devices/system/cpu/cpufreq", d)
                                  for d in (os.listdir("/sys/devices/system/cpu/cpufreq")
                                            if os.path.isdir("/sys/devices/system/cpu/cpufreq") else []))
                      if os.path.exists(os.path.join(p, "scaling_governor")))
        if not pols:
            return "no CPU frequency control here"
        try:
            for p in pols:
                g = os.path.join(p, "scaling_governor")
                with open(g) as f:
                    cur = f.read().strip()
                if cur != "performance":
                    with open(g, "w") as f:
                        f.write("performance")
                    self.governor.setdefault(g, cur)
            self._save()
            return "CPU governor set to performance (put back when you resume)"
        except PermissionError:
            return ("CPU governor needs root. Optional, in a terminal: echo performance | sudo tee "
                    "/sys/devices/system/cpu/cpufreq/policy*/scaling_governor")

    def _restore_governor(self):
        for g, cur in list(self.governor.items()):
            try:
                with open(g, "w") as f:
                    f.write(cur)
            except OSError:
                pass
        self.governor = {}

    def status(self):
        return {"paused": [f"{p['name']} ({p['pid']})" for p in self.paused], "notes": self.notes[-5:],
                "npu": npu_busy()}
