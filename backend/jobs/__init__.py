"""The functions that are not HTTP routes.

* ``timers`` — ``harvest_timer`` (the v1 harvest, until Phase 2 replaces it)
  and ``maintenance_timer`` (daily 02:15 UTC). Phase 2 adds ``cycle_timer``.
* ``queues`` — ``account_purge_worker`` and ``ops_jobs_worker``. Phase 3 adds
  ``suggestions_worker`` together with the code that runs it.

The queue contract itself (names, message shapes, the connection) is
core.jobs, which producers and workers both import. Not to be confused with
this package: ``core.jobs`` is the contract, ``jobs`` is where the functions
live.
"""
