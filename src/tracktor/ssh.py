"""SSH to the clusters: one master connection per cluster, non-interactive commands, log-in terminal."""
import os, shlex, shutil, subprocess

from .config import CTRL_DIR, SSH, cset


def ssh_config_hosts():
    """Host aliases from ~/.ssh/config (without wildcards), offered when adding a cluster."""
    out = []
    try:
        for l in open(os.path.expanduser("~/.ssh/config")):
            t = l.strip().split()
            if len(t) >= 2 and t[0].lower() == "host":
                out += [h for h in t[1:] if not any(c in h for c in "*?!")]
    except OSError:
        pass
    return list(dict.fromkeys(out))


def ssh_opts(master="auto", persist="10m"):
    os.makedirs(CTRL_DIR, mode=0o700, exist_ok=True)
    return ["-o", f"ControlPath={CTRL_DIR}/%C", "-o", f"ControlMaster={master}", "-o", f"ControlPersist={persist}",
            "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR"]


MARK = "@@TRACKTOR@@"
# Remote environment: a plain non-interactive shell (no -l: on some clusters the login profile rebuilds PATH and
# loses /usr/bin/squeue).  If squeue is still missing, the profiles are sourced; the usual Slurm folders are also
# appended to PATH.
SLURM_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/slurm/bin:/usr/local/slurm/bin"
PRELUDE = (f'export PATH="$PATH:{SLURM_PATH}"; command -v squeue >/dev/null 2>&1 || '
           f'{{ . /etc/profile; . ~/.bash_profile || . ~/.profile; export PATH="$PATH:{SLURM_PATH}"; }} '
           f'</dev/null >/dev/null 2>&1; ')


def bad_host(host):
    """A host must not look like an ssh option (e.g. "-oProxyCommand=…")."""
    return not host or host.startswith("-") or any(ch.isspace() for ch in host)


def ssh_run(host, script, timeout=60, pre=""):
    """Run a bash script on host without interaction (pre: the cluster's setup commands, e.g. module load slurm).
    Returns (ok, output after the marker, error)."""
    if bad_host(host):
        return False, "", f"invalid SSH host: {host!r}"
    pre = f"{{ {pre.strip().rstrip(';')}; }} </dev/null >/dev/null 2>&1; " if pre.strip() else ""
    cmd = [SSH, *ssh_opts(), "-o", "BatchMode=yes", host,
           "bash -c " + shlex.quote(f"{PRELUDE}{pre}echo {MARK}; {script}")]
    try:
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    except OSError as e:
        return False, "", str(e)
    ACTIVE.add(pr)
    try:
        out, err = pr.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        pr.kill(); pr.communicate()
        return False, "", "no answer (timed out)"
    finally:
        ACTIVE.discard(pr)
    if MARK not in out:
        err = (err.strip().splitlines() or [f"ssh exited with code {pr.returncode}"])[-1]
        return False, "", err
    return True, out.split(MARK, 1)[1].lstrip("\n"), err.strip()


ACTIVE = set()     # ssh queries in progress (to cut them when quitting)


def kill_active():
    for pr in list(ACTIVE):
        try:
            pr.kill()
        except OSError:
            pass


def close_all_masters():
    """Close every Tracktor master connection (those in ~/.cache/tracktor/ssh), for current clusters or not."""
    try:
        socks = [os.path.join(CTRL_DIR, f) for f in os.listdir(CTRL_DIR)]
    except OSError:
        return
    for sock in socks:
        try:
            subprocess.run([SSH, "-o", f"ControlPath={sock}", "-O", "exit", "tracktor"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if os.path.exists(sock):        # leftover socket of a connection that no longer exists
            try:
                if subprocess.run([SSH, "-o", f"ControlPath={sock}", "-O", "check", "tracktor"],
                                  capture_output=True, timeout=5).returncode != 0:
                    os.remove(sock)
            except (OSError, subprocess.TimeoutExpired):
                pass


def terminal_cmd(shell_cmd):
    """Command that opens shell_cmd in an available terminal emulator (or None)."""
    term = os.environ.get("TERMINAL")
    cands = ([(term, ["-e"])] if term else []) + [("konsole", ["-e"]), ("gnome-terminal", ["--"]),
             ("xfce4-terminal", ["-x"]), ("kitty", []), ("alacritty", ["-e"]), ("x-terminal-emulator", ["-e"]),
             ("xterm", ["-e"])]
    for exe, flag in cands:
        if exe and shutil.which(exe):
            return [exe, *flag, "bash", "-c", shell_cmd]
    return None


def connect_in_terminal(c):
    """Open a terminal where the user logs in once; leaves a persistent master connection behind."""
    if bad_host(c["host"]):
        return False
    h = cset(c, "persist_hours")
    ssh = " ".join(shlex.quote(x) for x in [SSH, *ssh_opts("yes", f"{h}h"), c["host"], "true"])
    check = " ".join(shlex.quote(x) for x in [SSH, *ssh_opts(), "-O", "check", c["host"]])
    name = shlex.quote(c["name"])
    sh = (f"echo 'Connecting to '{name}' ({c['host']})…'; echo; "
          f"if {check} >/dev/null 2>&1; then echo 'Already connected (the connection is still open).'; elif {ssh}; then echo; "
          f"echo 'Connected. You can close this window (the connection stays open for {h} h).'; "
          f"else echo; echo 'Could not connect.'; fi; echo; read -r -p 'Press Enter to close.' _")
    cmd = terminal_cmd(sh)
    if cmd:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return cmd is not None


def disconnect(c):
    if bad_host(c["host"]):
        return
    subprocess.run([SSH, *ssh_opts(), "-O", "exit", c["host"]], capture_output=True, timeout=10)
