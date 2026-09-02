"""Optional non-Maxwellian ``f_init`` procedure for XGC.

Kept apart from ``tools/`` on purpose: it is a specialised workflow that is
not part of the everyday submit/monitor loop, and nothing here is loaded
unless the ``xgc-finit`` CLI or the ``f-init`` skill is invoked. See
``f_init/README.md``.
"""
