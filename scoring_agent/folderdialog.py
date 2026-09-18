"""Native "pick a folder" dialog, straight from Windows (no GUI toolkit needed).

The app used to get this from pywebview. Now that the window is a Chrome app window, a browser
cannot hand back a real filesystem path, so we ask Windows ourselves through the same
IFileOpenDialog that File Explorer uses.

If anything at all goes wrong this returns None and the UI falls back to a plain box the user
can paste a path into - picking a folder must never be the thing that breaks.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int, c_uint, c_void_p, c_wchar_p
from ctypes.wintypes import HWND, LPCWSTR

# COM plumbing
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 0x2
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
SIGDN_FILESYSPATH = 0x80058000

CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    g = GUID()
    ole32 = ctypes.windll.ole32
    if ole32.CLSIDFromString(LPCWSTR(text), byref(g)) != 0:
        raise OSError("bad GUID")
    return g


def _vtbl_call(obj, index, *args, restype=ctypes.HRESULT, argtypes=()):
    """Call method `index` on a COM object's vtable."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _app_window() -> int:
    """The app's own window, so the dialog is OWNED by it and opens in front.

    Without an owner Windows is free to put the dialog behind the app, where people cannot see
    that anything opened at all. An exact title lookup usually finds it; if the window has been
    renamed for any reason, fall back to walking the visible top-level windows.
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Scoring Agent")
        if hwnd:
            return int(hwnd)

        found = []

        def cb(h, _):
            if not user32.IsWindowVisible(h):
                return True
            n = user32.GetWindowTextLengthW(h)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                if buf.value.strip().startswith("Scoring Agent"):
                    found.append(h)
            return True

        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
        user32.EnumWindows(proc, 0)
        return int(found[0]) if found else 0
    except Exception:
        return 0


def ask_folder(title: str = "Select the course folder") -> str | None:
    """Show the folder picker. Returns the chosen path, or None if cancelled/unavailable."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    dialog = c_void_p()
    try:
        hr = ole32.CoCreateInstance(byref(_guid(CLSID_FileOpenDialog)), None,
                                    CLSCTX_INPROC_SERVER, byref(_guid(IID_IFileOpenDialog)),
                                    byref(dialog))
        if hr != 0 or not dialog:
            return None

        # IFileDialog vtable: 0-2 IUnknown, 3 Show, 4 SetFileTypes ... 9 SetOptions,
        # 10 GetOptions ... 17 SetTitle ... 20 GetResult
        opts = c_uint()
        _vtbl_call(dialog, 10, byref(opts), argtypes=(POINTER(c_uint),))
        _vtbl_call(dialog, 9, c_uint(opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM),
                   argtypes=(c_uint,))
        _vtbl_call(dialog, 17, c_wchar_p(title), argtypes=(c_wchar_p,))

        owner = _app_window()
        if owner:
            # bring our window up first, so the dialog lands on top of a focused app
            try:
                ctypes.windll.user32.SetForegroundWindow(owner)
            except Exception:
                pass
        hr = _vtbl_call(dialog, 3, HWND(owner), argtypes=(HWND,))
        if hr != 0:
            return None                      # the user cancelled

        item = c_void_p()
        if _vtbl_call(dialog, 20, byref(item), argtypes=(POINTER(c_void_p),)) != 0 or not item:
            return None
        try:
            # IShellItem vtable: 0-2 IUnknown, 3 BindToHandler, 4 GetParent, 5 GetDisplayName
            name = c_wchar_p()
            if _vtbl_call(item, 5, c_int(SIGDN_FILESYSPATH), byref(name),
                          argtypes=(c_int, POINTER(c_wchar_p))) != 0:
                return None
            path = name.value
            ole32.CoTaskMemFree(name)
            return path
        finally:
            _vtbl_call(item, 2, restype=ctypes.c_ulong)          # Release
    except Exception:
        return None
    finally:
        if dialog:
            try:
                _vtbl_call(dialog, 2, restype=ctypes.c_ulong)    # Release
            except Exception:
                pass
        ole32.CoUninitialize()
