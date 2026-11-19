"""
Tracktor: one window for your running jobs and their history, both local (launched with `track`) and on Slurm
clusters (over SSH).

  - "Running": every job with its elapsed time; click one to see its output (it follows the end of the log unless
    you scroll up to read).  Right-click: local -> "Terminate" (SIGTERM) / "Kill" (SIGKILL) the program started by
    track (and its children); cluster -> "Cancel" (scancel).  For cluster jobs, the "Output (.out)" / "Errors (.err)"
    buttons choose which file to show (if Slurm merges them, there is only output).
  - Local jobs show RAM / CPU / GPU usage (summed over their child processes; GPU from nvidia-smi) and total RAM at
    the top.  Two local jobs with the same command in the same folder are flagged as "⚠ duplicate".
  - "History": finished jobs; click one to see its log.  Hover a job and a ✕ appears on the right to remove just
    that one (its log is deleted too if it was a temporary one).  "Clear history" empties the list and deletes the
    temporary local logs (those of `track` without -l, in <tracktor dir>/logs); logs saved with -l and files on the
    clusters are never touched (finished cluster jobs are only hidden).  Search box and error filter.
    Right-click a local job > "Run again…" (with track, in its folder).
  - Clusters: estimated start time of queued jobs (squeue --start) and partition; job arrays are grouped in one
    expandable row with their progress; click a finished job to see its efficiency (memory/CPU/time, like seff).
  - Cluster status (panel under the job tables; its "+" tab opens a cluster): free nodes / CPUs / GPUs per partition, the queue, and "Scripts": the
    Slurm / bash / config files (and the types you add) in the folders of your recent jobs and in folders you choose, grouped by
    category (configurable); click one to edit it in the viewer, Ctrl+S saves it on the cluster, "sbatch…" submits
    a Slurm script, "Run…" runs a bash script on the login node.
  - Log: error lines in red, warnings in orange; Ctrl+F to search (Enter / Shift+Enter / Esc).
  - System tray (on by default): closing the window keeps Tracktor in the tray showing the number of running jobs,
    still watching and notifying; click the icon to show it, right-click > Quit to exit.  Single instance.
  - "⚙" button: settings (refresh rates, history days, notifications, log font, ...).
  - "+" button: add a Slurm cluster you can reach over SSH (a Host from ~/.ssh/config or user@server).  Each tab is
    then grouped as "Local", "<cluster>", ...  Clusters are polled with squeue / sacct over SSH reusing one master
    connection (ControlMaster).  If the cluster asks for a password, key passphrase or 2FA: right-click the cluster >
    "Connect…" opens a terminal to log in once.
Reads ~/.local/state/tracktor (or $TRACKTOR_DIR), the folders listed in ~/.config/tracktor/dirs and
~/.config/tracktor/clusters.json.
"""
__version__ = "0.2.0.dev0"
