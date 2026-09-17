"""Keep Windows awake for the duration of a run (screen may sleep, system won't).

Uses SetThreadExecutionState so a long batch keeps running even if the laptop would otherwise
sleep. Safe no-op on non-Windows.
"""
from __future__ import annotations
import ctypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


class KeepAwake:
    def __enter__(self):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
