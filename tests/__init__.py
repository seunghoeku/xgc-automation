"""Test package marker.

Tests write to temporary catalogs; they must never pull from or commit to the
developer's own catalog repository, which the synced library functions would
otherwise do under a real ~/.config/xgc-automation/config.yaml.
"""

from tools import catalog_sync

catalog_sync.disable_for_process()
