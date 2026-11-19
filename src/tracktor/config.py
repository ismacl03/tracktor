"""Paths, settings (⚙ dialog and per cluster), file types of the Scripts tab, clusters.json."""
import json, os, re


APP = "Tracktor"
CONF_DIR = os.path.expanduser("~/.config/tracktor")
SETTINGS_FILE = os.path.join(CONF_DIR, "settings.json")
CLUSTERS_FILE = os.path.join(CONF_DIR, "clusters.json")
STATE_DIR = os.environ.get("TRACKTOR_DIR", os.path.expanduser("~/.local/state/tracktor"))
CTRL_DIR = os.path.expanduser("~/.cache/tracktor/ssh")
SSH = os.environ.get("TRACKTOR_SSH", "ssh")

# Settings (⚙ button; saved in ~/.config/tracktor/settings.json).  key: (default, label, min, max, unit)
DEFAULTS = {
    "local_every": (2, "Local refresh interval", 1, 60, "s"),
    "tail_kb": (400, "Amount of log shown (end of file)", 50, 20000, "KB"),
    "log_font": (10, "Log font size", 6, 24, "pt"),
    "show_pending": (True, "Show queued cluster jobs (PENDING)"),
    "show_gpus": (True, "Show on which node and GPU(s) each cluster job runs (also in the queue of the status panel)"),
    "wrap_log": (False, "Wrap long log lines"),
    "notify_local": (True, "Notify when a local job finishes"),
    "notify_cluster": (True, "Notify when a cluster job finishes"),
    "notify_ok": (True, "Also notify jobs that finish fine (otherwise only errors)"),
    "notify_start": (True, "Notify when a cluster job starts (goes from PD to R)"),
    "tray": (True, "When the window is closed, keep running in the system tray (and keep notifying)"),
    "tray_notice": (True, "Every time the window is closed, show a reminder that Tracktor keeps running"),
    "close_on_exit": (False, "When quitting, also close the cluster connections (nothing is left in the background; "
                             "you will have to log in again)"),
}
S = {k: v[0] for k, v in DEFAULTS.items()}

# Settings of each cluster (saved in clusters.json).  key: (default, label, min, max, unit, tooltip)
CLUSTER_SETTINGS = {
    "every": (60, "Poll its jobs every", 10, 3600, "s",
              "How often squeue/sacct are queried over SSH for your jobs.  Very often loads the login node."),
    "cluster_log_every": (10, "Refresh the log of a running job every", 3, 600, "s",
                          "How often the log you are looking at is re-read while the job runs."),
    "hist_days": (7, "Days of history", 1, 90, "days", "How far back finished jobs are listed in History."),
    "persist_hours": (9, "Keep the connection open after «Connect…» for", 1, 168, "h",
                      "Only for clusters where you log in by hand (password / 2FA): how long the SSH connection "
                      "stays open without activity."),
    "info_every": (60, "Refresh its status panel every", 10, 3600, "s",
                   "How often the cluster status panel (right-click the cluster > Show cluster status) is "
                   "refreshed, only while it is open."),
}


def cset(c, k):
    """A setting of one cluster (old versions had some of them global: that value is used as the default)."""
    v = c.get(k) if c else None
    return int(v) if v is not None else int(S.get(k, CLUSTER_SETTINGS[k][0]))


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            S.update({k: v for k, v in json.load(f).items() if k in DEFAULTS and type(v) is type(DEFAULTS[k][0])})
    except (OSError, ValueError):
        pass
    for k, d in DEFAULTS.items():     # numeric values within their range
        if not isinstance(d[0], bool):
            S[k] = min(max(S[k], d[2]), d[3])


def save_settings():
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump(S, f, indent=1)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)


def tail_bytes():
    return S["tail_kb"] * 1000


# ───────────────────────────── clusters (Slurm over SSH) ─────────────────────────────
def load_clusters():
    try:
        with open(CLUSTERS_FILE) as f:
            return [c for c in json.load(f) if c.get("name") and c.get("host")]
    except (OSError, ValueError):
        return []


def save_clusters(clusters):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CLUSTERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(clusters, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CLUSTERS_FILE)


CLUSTER_EVERY_DEFAULT = 60           # s between queries to a cluster (can be changed per cluster)
CLUSTER_INFO_EVERY_DEFAULT = 60      # s between refreshes of the cluster status panel (per cluster)


# ───────────────────────────── batch scripts on the clusters (status panel > "Scripts") ─────────────────────────────
# File types listed, by category (editable in "Folders and file types…"; saved in ~/.config/tracktor/file_types.json).
# run: "sbatch" (submit it), "bash" (run it on the login node), "" (only open / edit).  Any listed file with #SBATCH
# lines goes to the "sbatch" category whatever its extension.
FILE_TYPES_FILE = os.path.join(CONF_DIR, "file_types.json")
FILE_TYPES_DEFAULT = [
    {"name": "Slurm", "exts": ".slurm .sbatch .job", "run": "sbatch"},
    {"name": "Bash", "exts": ".sh .bash", "run": "bash"},
    {"name": "Config", "exts": ".yaml .yml .toml .json .ini .cfg .conf", "run": ""},
]
RUN_ACTIONS = {"sbatch": "Submit (sbatch)", "bash": "Run (bash, login node)", "": "Only open / edit"}
BASH_TIMEOUT = 600          # s a bash script run from Tracktor may last
FILE_TYPES = [dict(t) for t in FILE_TYPES_DEFAULT]


def load_file_types():
    try:
        with open(FILE_TYPES_FILE) as f:
            got = [dict(name=str(t["name"]), exts=str(t["exts"]), run=t.get("run", "") if t.get("run") in RUN_ACTIONS
                        else "") for t in json.load(f) if t.get("name") and t.get("exts")]
        FILE_TYPES[:] = got or FILE_TYPES[:]
    except (OSError, ValueError, TypeError, KeyError):
        pass


def save_file_types(types):
    FILE_TYPES[:] = types
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(FILE_TYPES_FILE + ".tmp", "w") as f:
        json.dump(types, f, indent=1, ensure_ascii=False)
    os.replace(FILE_TYPES_FILE + ".tmp", FILE_TYPES_FILE)


def type_exts(t):
    """".yaml yml *.toml" -> [".yaml", ".yml", ".toml"] (only safe characters)."""
    out = []
    for e in t["exts"].replace(",", " ").split():
        e = "." + e.lstrip("*").lstrip(".")
        if re.fullmatch(r"\.[A-Za-z0-9_+-]+", e):
            out.append(e)
    return out


def classify(rel, has_sbatch):
    """-> the category (dict of FILE_TYPES) of a file, or None."""
    if has_sbatch:
        t = next((t for t in FILE_TYPES if t["run"] == "sbatch"), None)
        if t:
            return t
    low = rel.lower()
    return next((t for t in FILE_TYPES if any(low.endswith(e.lower()) for e in type_exts(t))), None)
SCRIPT_DEPTH = 3            # how deep the chosen folders are searched (the folders of your jobs: only that folder)
SCRIPT_EDIT_MAX = 64000     # bigger scripts open read-only (a save travels inside one SSH command line)
