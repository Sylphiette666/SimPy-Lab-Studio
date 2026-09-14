"""Windows-only QA for the owned Studio window and its native save dialog."""

import argparse
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

USER = ctypes.WinDLL("user32", use_last_error=True)
USER.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
USER.FindWindowW.restype = wintypes.HWND
USER.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
USER.SendMessageW.restype = wintypes.LPARAM
USER.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
USER.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER.GetDlgCtrlID.argtypes = [wintypes.HWND]
USER.IsWindowVisible.argtypes = [wintypes.HWND]
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
USER.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]
USER.EnumChildWindows.argtypes = [wintypes.HWND, CALLBACK, wintypes.LPARAM]


def details(handle):
    caption = ctypes.create_unicode_buffer(1000)
    class_name = ctypes.create_unicode_buffer(100)
    USER.GetWindowTextW(handle, caption, len(caption))
    USER.GetClassNameW(handle, class_name, len(class_name))
    process = wintypes.DWORD()
    USER.GetWindowThreadProcessId(handle, ctypes.byref(process))
    return {
        "handle": handle,
        "pid": process.value,
        "class": class_name.value,
        "id": USER.GetDlgCtrlID(handle),
        "title": caption.value,
    }


def windows(parent=None):
    found = []

    @CALLBACK
    def callback(handle, _):
        found.append(details(handle))
        return True

    if parent:
        USER.EnumChildWindows(parent, callback, 0)
    else:
        USER.EnumWindows(callback, 0)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inspect", "save", "close"])
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    main_window = USER.FindWindowW(None, "SimPy Lab Studio")
    assert main_window, "Native SimPy Lab Studio window not found"
    process = details(main_window)["pid"]
    if args.action == "close":
        USER.PostMessageW(main_window, 0x0010, 0, 0)
    else:
        dialogs = [
            item
            for item in windows()
            if item["pid"] == process
            and item["class"] == "#32770"
            and USER.IsWindowVisible(item["handle"])
        ]
        assert len(dialogs) == 1, dialogs
        controls = windows(dialogs[0]["handle"])
        if args.action == "inspect":
            print(json.dumps(controls, ensure_ascii=False, indent=2))
        else:
            edit = next(item for item in controls if item["class"] == "Edit" and item["id"] == 1001)
            name = ctypes.create_unicode_buffer(str(Path(args.path).resolve()))
            USER.SendMessageW(edit["handle"], 0x000C, 0, ctypes.addressof(name))
            button = next(
                item for item in controls if item["class"] == "Button" and item["id"] == 1
            )
            USER.PostMessageW(button["handle"], 0x00F5, 0, 0)
            print("Native Save dialog accepted the selected output path")


if __name__ == "__main__":
    main()
