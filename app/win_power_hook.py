"""Bounded Win32 shutdown/logoff/suspend notifications for detached pythonw."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading

logger=logging.getLogger(__name__)

CTRL_CLOSE_EVENT=2
CTRL_LOGOFF_EVENT=5
CTRL_SHUTDOWN_EVENT=6
WM_QUERYENDSESSION=0x0011
WM_ENDSESSION=0x0016
WM_POWERBROADCAST=0x0218
PBT_APMSUSPEND=0x0004
WM_QUIT=0x0012


class WinPowerHook:
    """Install console and hidden top-level window hooks; a no-op off Windows."""
    def __init__(self, release_callback, *, platform=None):
        self.release_callback=release_callback
        self.platform=sys.platform if platform is None else platform
        self.thread=None
        self.thread_id=None
        self.hwnd=None
        self._ctrl_handler=None
        self._wndproc=None
        self._class_name=f'NovelPulsePowerHook_{os.getpid()}_{id(self):x}'
        self._released=threading.Event()
        self._release_lock=threading.Lock()
        self._ready=threading.Event()
        self._closed=threading.Event()
        self._class_atom=None
        self._console_registered=False

    def _release_once(self, reason):
        if not self._release_lock.acquire(blocking=False):
            return
        try:
            if self._released.is_set():
                return
            self._released.set()
            try:
                self.release_callback(reason)
            except Exception:
                logger.exception('Win32 power-event release callback failed')
        finally:
            self._release_lock.release()

    def start(self):
        if self.platform!='win32':
            return False
        if self.thread and self.thread.is_alive():
            return True
        self._ctrl_handler=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_uint)(self._on_console_control)
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler.argtypes=[ctypes.c_void_p,ctypes.c_bool]
            ctypes.windll.kernel32.SetConsoleCtrlHandler.restype=ctypes.c_bool
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler,True):
                logger.warning('SetConsoleCtrlHandler could not be registered')
            else:
                self._console_registered=True
        except Exception as error:
            logger.warning('Console control hook unavailable: %s',type(error).__name__)
        self.thread=threading.Thread(target=self._window_thread,name='NovelPulse-WinPowerHook',daemon=True)
        self.thread.start()
        self._ready.wait(timeout=2)
        return self._ready.is_set() or self._console_registered

    def _on_console_control(self, event):
        reasons={CTRL_CLOSE_EVENT:'win32_power_event',CTRL_LOGOFF_EVENT:'win32_power_event',
                 CTRL_SHUTDOWN_EVENT:'win32_power_event'}
        if event in reasons:
            self._release_once(reasons[event])
            return True
        return False

    def _window_thread(self):
        try:
            from ctypes import wintypes

            LRESULT=ctypes.c_ssize_t
            WNDPROC=ctypes.WINFUNCTYPE(LRESULT,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)

            class WNDCLASSW(ctypes.Structure):
                _fields_=[('style',wintypes.UINT),('lpfnWndProc',WNDPROC),('cbClsExtra',ctypes.c_int),
                    ('cbWndExtra',ctypes.c_int),('hInstance',wintypes.HINSTANCE),('hIcon',wintypes.HICON),
                    ('hCursor',wintypes.HANDLE),('hbrBackground',wintypes.HBRUSH),
                    ('lpszMenuName',wintypes.LPCWSTR),('lpszClassName',wintypes.LPCWSTR)]

            user32=ctypes.windll.user32
            kernel32=ctypes.windll.kernel32
            user32.RegisterClassW.argtypes=[ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype=wintypes.ATOM
            user32.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,
                wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,
                wintypes.HMENU,wintypes.HINSTANCE,wintypes.LPVOID]
            user32.CreateWindowExW.restype=wintypes.HWND
            user32.DefWindowProcW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
            user32.DefWindowProcW.restype=ctypes.c_ssize_t
            user32.GetMessageW.argtypes=[ctypes.POINTER(wintypes.MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT]
            user32.GetMessageW.restype=ctypes.c_int
            user32.TranslateMessage.argtypes=[ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.argtypes=[ctypes.POINTER(wintypes.MSG)]
            user32.DestroyWindow.argtypes=[wintypes.HWND]
            kernel32.GetModuleHandleW.argtypes=[wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype=wintypes.HINSTANCE
            kernel32.GetCurrentThreadId.restype=wintypes.DWORD
            self.thread_id=kernel32.GetCurrentThreadId()

            def wndproc(hwnd,msg,wparam,lparam):
                if msg==WM_QUERYENDSESSION:
                    self._release_once('win32_power_event')
                    return 1  # Allow Windows shutdown/logoff to continue.
                if msg==WM_ENDSESSION and wparam:
                    self._release_once('win32_power_event')
                    return 0
                if msg==WM_POWERBROADCAST and wparam==PBT_APMSUSPEND:
                    self._release_once('win32_power_event')
                    return 1
                if msg==WM_QUIT:
                    return 0
                return user32.DefWindowProcW(hwnd,msg,wparam,lparam)

            self._wndproc=WNDPROC(wndproc)
            instance=kernel32.GetModuleHandleW(None)
            window_class=WNDCLASSW(0,self._wndproc,0,0,instance,None,None,None,None,self._class_name)
            self._class_atom=user32.RegisterClassW(ctypes.byref(window_class))
            if not self._class_atom:
                raise ctypes.WinError()
            # An invisible top-level popup receives broadcast session/power messages.
            self.hwnd=user32.CreateWindowExW(0,self._class_name,'NovelPulse Power Listener',
                0x80000000,0,0,1,1,None,None,instance,None)
            if not self.hwnd:
                raise ctypes.WinError()
            self._ready.set()
            message=wintypes.MSG()
            while not self._closed.is_set():
                result=user32.GetMessageW(ctypes.byref(message),None,0,0)
                if result<=0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
        except Exception as error:
            logger.exception('Win32 hidden power window unavailable: %s',type(error).__name__)

    def close(self):
        if self.platform!='win32':
            return
        self._closed.set()
        try:
            if self.thread_id:
                from ctypes import wintypes
                ctypes.windll.user32.PostThreadMessageW.argtypes=[ctypes.c_uint,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
                ctypes.windll.user32.PostThreadMessageW(self.thread_id,WM_QUIT,0,0)
        except Exception:
            pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=.5)
        try:
            if self._ctrl_handler:
                ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler,False)
        except Exception:
            pass
