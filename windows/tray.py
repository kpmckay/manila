"""A notification-area icon for Manila, in ctypes and nothing else.

Manila runs a web server, and a web server has no window to close: shutting the
browser tab leaves it running, which is confusing in something launched from an
icon. So on Windows it puts an icon in the notification area -- double-click to
open it, right-click to stop it -- and behaves like the installed program it is
pretending to be.

Pure stdlib, because the rest of Manila is: no pywin32, no pystray, nothing to
install. That means talking to Win32 directly, and the fiddly parts are marked
where they are not obvious.
"""

import ctypes
import threading
import time
import winreg
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# LRESULT and the message parameters are pointer-sized. Saying so matters:
# left as the default int, every one of these calls corrupts the stack on 64
# bit Windows, which is every Windows anyone runs now.
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
TRAY_MESSAGE = WM_APP + 1          # what the icon sends us mouse events as

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_NONE = 0x00

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

ID_OPEN, ID_STOP = 1, 2

# Windows 11 files every new notification-area icon into the overflow flyout
# and shows nothing on the taskbar. For most apps that is a reasonable default;
# here the icon is the only way to stop Manila, so hiding it hides the one
# control the app has. The shell records which icons are promoted per user
# here, and writing the flag is what dragging an icon out of the overflow does.
NOTIFY_SETTINGS = r"Control Panel\NotifyIconSettings"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadImageW.restype = wintypes.HANDLE
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


SW_RESTORE = 9
ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def focus_window(title, skip_class="ManilaTray"):
    """Raise an already-open window with exactly this title. False if none.

    So that asking for Manila when Manila is already on screen brings it to the
    front, rather than opening a second window onto the same server -- which is
    what any installed program does, and what a browser will not do for you.

    Our own message-only window carries the same title, so it is skipped by
    class; it is invisible anyway, but not by accident.
    """
    match = []

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        text = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, text, 512)
        if text.value != title:
            return True
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if name.value == skip_class:
            return True
        match.append(hwnd)
        return False                      # found it; stop looking

    user32.EnumWindows(ENUMPROC(visit), 0)
    if not match:
        return False
    hwnd = match[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


class Tray:
    """The icon, its menu, and the message loop that drives them."""

    def __init__(self, title, icon_path, on_open, on_stop, promote_match="Manila"):
        self.title = title
        self.promote_match = promote_match
        self.icon_path = icon_path
        self.on_open = on_open
        self.on_stop = on_stop
        self.hwnd = None
        self.hicon = None
        # Explorer can be restarted; when it is, every tray icon is dropped and
        # has to be added again. It tells us so with this message.
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        # Held on the instance because ctypes does not: let the thunk be
        # collected and the next message dispatched runs freed memory.
        self._wndproc = WNDPROC(self._dispatch)

    # --- setup ---------------------------------------------------------------

    def _register(self):
        instance = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = instance
        cls.lpszClassName = "ManilaTray"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A real window is required to receive the icon's messages, but it is
        # never shown -- the icon is the whole user interface.
        self.hwnd = user32.CreateWindowExW(0, "ManilaTray", "Manila", 0,
                                           0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def _load_icon(self):
        if self.icon_path and self.icon_path.exists():
            handle = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON,
                                       0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
        return user32.LoadIconW(None, wintypes.LPCWSTR(32512))   # IDI_APPLICATION

    def _data(self, flags):
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = TRAY_MESSAGE
        data.hIcon = self.hicon
        data.szTip = self.title
        return data

    def _add(self):
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())

    # --- being visible -------------------------------------------------------

    def _promote_once(self):
        """Set IsPromoted on our own entry. False if the shell has not made
        one yet, which is normal for the first second after the icon is added."""
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_SETTINGS)
        except OSError:
            return False
        done = False
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, index)
                    access = winreg.KEY_READ | winreg.KEY_SET_VALUE
                    with winreg.OpenKey(root, name, 0, access) as entry:
                        tip, _ = winreg.QueryValueEx(entry, "InitialTooltip")
                        # Ours and only ours: every Manila tooltip starts this
                        # way, and no other app's entry is touched.
                        if not str(tip).startswith(self.promote_match):
                            continue
                        try:
                            winreg.QueryValueEx(entry, "IsPromoted")
                        except FileNotFoundError:
                            # No flag yet means nobody has decided: this is the
                            # Windows default, not a preference, so fix it.
                            winreg.SetValueEx(entry, "IsPromoted", 0,
                                              winreg.REG_DWORD, 1)
                        # A flag that exists is a decision -- yours, made by
                        # dragging the icon in or out. Leave it alone; an app
                        # that re-promotes itself every launch is the reason
                        # this corner of the taskbar needs defending.
                        done = True
                except OSError:
                    continue        # entry vanished or is not ours to write
        return done

    def promote(self, attempts=12, pause=0.5):
        """Keep trying until the shell has registered the icon, then give up.

        Best effort throughout: an icon in the overflow is worse than one on
        the taskbar, but it is not a reason to fail to start.
        """
        for _ in range(attempts):
            try:
                if self._promote_once():
                    return True
            except OSError:
                return False
            time.sleep(pause)
        return False

    def notify(self, title, message):
        """A one-off balloon, so the first launch explains where it went."""
        data = self._data(NIF_INFO | NIF_ICON | NIF_TIP | NIF_MESSAGE)
        data.szInfoTitle = title
        data.szInfo = message
        data.dwInfoFlags = NIIF_NONE
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # --- events --------------------------------------------------------------

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "&Open Manila")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_STOP, "&Stop Manila")
        user32.SetMenuDefaultItem(menu, ID_OPEN, 0)

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Without this the menu will not dismiss when you click elsewhere --
        # a documented quirk of showing a menu from a window with no focus.
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                       point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)      # the other half of the fix
        user32.DestroyMenu(menu)
        return chosen

    def _dispatch(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created:
            self._add()                              # Explorer restarted
        elif message == TRAY_MESSAGE:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONDBLCLK:
                self.on_open()
            elif event == WM_RBUTTONUP:
                chosen = self._menu()
                if chosen == ID_OPEN:
                    self.on_open()
                elif chosen == ID_STOP:
                    user32.DestroyWindow(self.hwnd)
        elif message in (WM_CLOSE, WM_DESTROY):
            self.remove()
            user32.PostQuitMessage(0)
            return 0
        elif message == WM_COMMAND:
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # --- lifecycle -----------------------------------------------------------

    def remove(self):
        if self.hwnd:
            data = self._data(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

    def run(self, hello=None):
        """Show the icon and pump messages until Stop is chosen."""
        self._register()
        self.hicon = self._load_icon()
        self._add()
        # Off the message loop: the shell writes its entry a moment after the
        # icon is added, so this waits, and waiting here would freeze the menu.
        threading.Thread(target=self.promote, daemon=True).start()
        if hello:
            self.notify(*hello)
        message = wintypes.MSG()
        while True:
            got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if got in (0, -1):                       # WM_QUIT, or an error
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self.remove()
        self.on_stop()
