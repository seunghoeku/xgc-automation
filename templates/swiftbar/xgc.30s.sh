#!/bin/bash
# <swiftbar.title>XGC queues</swiftbar.title>
# <swiftbar.desc>Cached HPC job status, including recent finished jobs.</swiftbar.desc>
# <swiftbar.version>1.0</swiftbar.version>
# <swiftbar.author>XGC Automation</swiftbar.author>
# <swiftbar.runInBash>false</swiftbar.runInBash>

# Use this directory as the SwiftBar plugin folder. If copying the plugin,
# set XGC_AUTOMATION_ROOT to the repository in your plugin environment.
ROOT="${XGC_AUTOMATION_ROOT:-$(cd -- "$(dirname -- "$0")/../.." && pwd)}"
if ! cd -- "$ROOT" || [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo 'HPC ⚠'
    echo '---'
    echo 'Repository or .venv unavailable. Set XGC_AUTOMATION_ROOT.'
    exit 0
fi
exec "$ROOT/.venv/bin/python" -m tools.queue_watch --swiftbar --history-limit 5
