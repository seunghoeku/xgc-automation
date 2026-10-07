"""Headless report_profiles for one or more run directories (local or remote copies).

usage: python xgc_reader_profiles_png.py <out_dir> <label>=<run_dir> [<label>=<run_dir> ...]
Needs units.m, xgc.mesh.bp, xgc.oneddiag.bp, xgc.volumes.bp (and xgc.units.bp) in each run_dir.
"""
import os, re, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
sys.path.insert(0, os.path.expanduser("~/Documents/git/XGC_reader")); sys.path.insert(0, os.path.expanduser("~/Documents/git/XGC-Analysis"))
from xgc_reader import xgc1

out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
for arg in sys.argv[2:]:
    label, run_dir = arg.split("=", 1)
    x = xgc1(run_dir)
    x.load_units(); x.load_oned(); x.load_volumes()
    x.report_profiles(edge_lim=[0.85, 1.12])
    for k, num in enumerate(plt.get_fignums()):
        fig = plt.figure(num); ax = fig.axes[0] if fig.axes else None
        title = (ax.get_title() if ax else "") or (ax.get_ylabel() if ax else "")
        if ax is not None and ax.get_xlim()[1] - ax.get_xlim()[0] < 0.5: title += "_edge"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:50] or "fig"
        fig.suptitle(label, fontsize=9); fig.savefig(f"{out}/{label}_{k:02d}_{slug}.png", dpi=110, bbox_inches="tight")
    plt.close("all")
    print(label, "oned steps", x.od.step[0], "...", x.od.step[-1], "->", len(plt.get_fignums()), "figs closed")
print("wrote", len(os.listdir(out)), "files to", out)
