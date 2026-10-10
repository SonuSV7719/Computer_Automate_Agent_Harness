"""COM threading setup shared by the daemon and the Windows driver."""
from __future__ import annotations

import sys


def init_thread() -> None:
    """Put the calling thread in the multithreaded apartment, so UI Automation
    objects can be used from any of our worker threads."""
    if sys.platform != "win32":
        return
    sys.coinit_flags = 0  # comtypes initialises the importing thread with this value
    import comtypes

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # this thread already has an apartment
