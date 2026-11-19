<p align="center"><img src="docs/logo.svg" width="220" alt="Tracktor"></p>

# Tracktor

**One window for all your simulations** — the ones running on your own machine and the ones queued or running on
Slurm clusters you reach over SSH.

![Tracktor screenshot](docs/screenshot.png)

## Features

- **Local jobs**: launch anything with `track` and see it in the window with its elapsed time, RAM, CPU and GPU
  memory (summed over all its child processes), and its log updating live.  Jobs with the same command in the same
  folder are flagged as duplicates.  Right-click to terminate or kill a job (the real program, not just a wrapper).
- **Slurm clusters** (any cluster you can `ssh` into): running and queued jobs with partition, state (`R`/`PD` and
  the reason it waits) and estimated start time; job arrays grouped in one row with their progress; the `.out` and
  `.err` of each job; `scancel` from the menu; seff-like efficiency (peak memory vs requested, CPU usage, time vs
  limit) of finished jobs; on which node and GPU number(s) each job runs (can be turned off in Settings).
- **Cluster status**: a panel under the job tables; its *+* tab opens a cluster (one tab per cluster; also
  right-click a cluster > *Show cluster status*).  It shows free nodes, CPUs, GPUs and memory per partition, jobs of
  everyone and yours, your fairshare, and the queue of everyone (your jobs in bold) with the CPUs, memory and GPUs of
  each job (given, or asked for if it is queued).  Clusters that hide other users' jobs show "—" there.  Which columns are shown can be chosen per cluster in Settings.
- **Scripts and configs**: the *Scripts* tab of the status panel lists the files found in the folders of your recent
  jobs and in the folders you choose, grouped by category: Slurm (`.slurm`, or anything with `#SBATCH` lines, with
  its partition, time, CPUs, memory and GPUs; every column can be sorted), Bash and Config (`.yaml`, `.json`,
  `.toml`…).  The categories and their extensions can be changed, and new ones added (e.g. Python: `.py`), in *Folders and file types…*.  Click a file
  to open it in the viewer: edit it there and save it on the cluster with Ctrl+S (it refuses to overwrite a file
  that changed meanwhile).  Slurm scripts can be submitted (*sbatch…*) and bash scripts run on the login node
  (*Run…*, output shown when it ends); both ask first, and the job list is refreshed right away (also after *Cancel*).
- **Sorting**: click a column header of *Running* or *History* (name, time, RAM, end, duration, result…) to sort
  by it; again to reverse, a third time to go back to the usual order.  Each cluster keeps its own group.  The
  lists of the cluster status panel (partitions, queue, scripts) sort the same way.
- **History** of finished jobs with search and an errors-only filter; run a local job again with one click.  Hover a
  job and click **✓** to remove it from the list (its log is kept) or **🗑** to remove it and delete its log; on a
  *Local* / cluster header they act on that whole group, and *Clear history* / *Delete history* on everything.
- **Notifications** when jobs start (clusters) and finish (local and clusters).
- **Log viewer with tabs**, like a browser: click a job to show its log in the current tab, double-click to open it
  in a new one (Ctrl+W closes it).  Errors in red, warnings in orange, and search (Ctrl+F).
- **Text summary** for a terminal: `tracktor --list` prints your running jobs and the recently finished ones, local
  and on the clusters, without opening a window (e.g. over SSH from a phone).
- **System tray** icon with the number of running jobs (queued cluster jobs not counted), also shown on the taskbar
  icon where the desktop supports it (KDE Plasma, Ubuntu dock); Tracktor keeps watching when the window is closed.

## Requirements

Linux with Python 3 and PyQt6 (`sudo apt install python3-pyqt6`, `sudo dnf install python3-pyqt6`,
`sudo pacman -S python-pyqt6`, or `pip install --user PyQt6`).  Optional: `notify-send` for notifications,
`nvidia-smi` for GPU usage.  For clusters: `ssh` and Slurm on the cluster side.

macOS is not supported yet (the cluster part would work; local process monitoring and installation are Linux-only).

## Install

```bash
git clone https://github.com/ismacl03/tracktor.git
cd tracktor
./install.sh
```

This installs `track` and `tracktor` in `~/.local/bin` (the program itself goes to `~/.local/share/tracktor/lib`)
and a **Tracktor** launcher in your application menu (and on
your desktop).  To update: `git pull && ./install.sh`.  To remove: `./uninstall.sh` (add `--purge` to also delete
settings and history).

## Usage

### Local jobs

```bash
track my_run python train.py --epochs 100                 # temporary log
track -l results/run1.log my_run python train.py          # log kept where you say
```

- Without `-l` the output goes to a **temporary log** (`~/.local/state/tracktor/logs/`), deleted when you remove
  the job with 🗑 or press *Delete history*.  Good for tests and debugging.
- With `-l <file>` the log is **permanent**: Tracktor never deletes it.  Use it for results you want to keep.

`track` runs the command in the foreground; to leave it running in the background use e.g.
`nohup track my_run python train.py >/dev/null 2>&1 &`.

**Per-project folder.**  A project can keep its job registry in its own folder: set `TRACKTOR_DIR`
(e.g. `TRACKTOR_DIR=$PWD/.jobs track ...`) and add that folder once with `./install.sh /path/to/project/.jobs`
(it is stored in `~/.config/tracktor/dirs`).  Useful when jobs are launched from a sandbox that cannot write to your
home folder.

### Clusters

Press **+**, give the cluster a name and pick a `Host` from your `~/.ssh/config` (or type `user@server`).  Tracktor
uses your normal `ssh`, so ports, keys, `ProxyJump`, etc. come from `~/.ssh/config`.  It polls `squeue`/`sacct`
every 60 s by default (configurable per cluster), reusing one SSH connection.

If the cluster needs a password, a key passphrase or 2FA, right-click it and choose **Connect…**: a terminal opens,
you log in once, and the connection stays open (9 h of inactivity by default; see Settings).

If `squeue` is only available after something like `module load slurm`, put that in the cluster's *Setup commands*.

### From a terminal or over SSH

```bash
tracktor --list                  # running jobs + those finished in the last 24 h, local and on the clusters
tracktor --list --clusters       # only the clusters (or --local); --hours 48 for a longer history
```

It needs no display, so it works over a plain `ssh` (from a phone, too), and it reuses the SSH connections of the
window, so clusters with 2FA answer without logging in again while Tracktor is running.

To see the window itself from another computer, connect with X forwarding (`ssh -Y host`, e.g. from WSL on
Windows 11) and run `tracktor`: opened over SSH it starts its own window there instead of bringing up the one on the
desktop.  Without a display it tells you to use `--list`.

### Settings

The ⚙ button: refresh rates and the columns of the status panel (per cluster: pick it in the drop-down), days of
cluster history, notifications, tray behaviour and log font.

When cluster jobs are deleted from the history (🗑 / *Delete history*), only `.out`/`.err` files whose name contains
the job number are deleted on the cluster.

## Files

| Path | What |
|---|---|
| `~/.config/tracktor/settings.json` | settings |
| `~/.config/tracktor/clusters.json` | clusters |
| `~/.config/tracktor/dirs` | extra job folders to watch |
| `~/.local/state/tracktor/` | running jobs (`*.job`), `history.jsonl`, temporary logs |
| `~/.cache/tracktor/ssh/` | SSH master connection sockets |

## Code layout

`tracktor` is a small launcher; the program is the `src/tracktor` package (it can also be run with
`python3 -m tracktor` from `src/`).  Only `tracktor.ui` uses Qt, so `tracktor --list` runs without it.

| Module | What |
|---|---|
| `config.py` | paths, settings, per-cluster settings, file types of the Scripts tab |
| `local.py` | local jobs started with `track`: running ones, history, logs, CPU / RAM / GPU |
| `ssh.py` | SSH: master connections, non-interactive commands, log-in terminal |
| `slurm.py` | Slurm queries and their parsing: jobs, history, efficiency, log paths, cluster status |
| `scripts.py` | the Scripts tab: finding files on a cluster, `#SBATCH` options |
| `cli.py` | `tracktor --list` |
| `app.py` | entry point (options, single instance) |
| `ui/main_window.py` | the window; split in mixins: `tables.py` (Running / History), `logview.py` (log viewer and its tabs), `editor.py` (editing / submitting cluster files), `status_panel.py` (cluster status), `clusters.py` (background queries) |
| `ui/dialogs.py`, `ui/common.py` | dialogs; shared widgets, icons, notifications |

## License

Copyright 2026 The Tracktor authors.  Licensed under the [Apache License 2.0](LICENSE).
