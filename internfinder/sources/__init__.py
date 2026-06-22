"""Source fetchers.

Each module exposes a callable returning ``list[Listing]``. Fetchers must fail
soft: a dead board, 404, timeout, or parse error logs a warning and returns
whatever it has rather than aborting the run.
"""
