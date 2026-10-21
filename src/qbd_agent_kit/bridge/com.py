"""The only part of the kit that talks to QuickBooks: the QuickBooks SDK's COM component (QBXMLRP2), through pywin32.

Every COM call runs on one worker thread. QuickBooks cannot cancel a request, so when one outlives its timeout the
caller gets QbTimeoutError while the worker stays inside QuickBooks. The transport then refuses further requests, which
would only queue behind the stuck one, and the process should exit without waiting for it."""
from __future__ import annotations

import queue
import sys
import threading
from typing import Any, Callable

from ..errors import QbConnectionError, QbNoSessionError, QbTimeoutError

# pythoncom joins COM on the thread that first imports it, as a single-threaded apartment by default, and the first such
# apartment is the process's main one. The SDK hangs before it reaches QuickBooks when the main apartment's thread is
# blocked waiting, as the CLI's main thread is. So the main thread joins the multithreaded apartment instead, and the
# worker thread below becomes the main single-threaded apartment.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

LOCAL_QBD = 1         # OpenConnection2: QuickBooks Desktop on this PC
OPEN_ANY_MODE = 2      # BeginSession: single- or multi-user, whichever the file is open in
XML_PARSE_ERROR = "0x80040400"
BUSY = "QuickBooks is still busy with an earlier request that did not answer in time."


def com_error(e: BaseException) -> QbConnectionError:
    """pywin32's com_error as QuickBooks' own code and message, which the SDK puts in the exception info."""
    hr, message, info = (list(e.args) + [None, None, None])[:3]
    if isinstance(info, tuple) and len(info) > 5 and info[5]:
        hr, message = info[5], info[2] or message
    code = f"0x{hr & 0xFFFFFFFF:08X}" if isinstance(hr, int) else str(hr)
    return QbConnectionError(code, str(message or "QuickBooks returned an error").strip())


class ComTransport:
    kind = "com"

    def __init__(self, app_id: str, app_name: str, company_file: str, open_timeout: float, timeout: float):
        self.app_id = app_id
        self.app_name = app_name
        self.company_file = company_file  # "" = the file open in QuickBooks
        self.open_timeout = open_timeout  # includes the time a person needs to answer the permission dialog
        self.timeout = timeout
        self.stuck = False                # a call outlived its timeout and is still inside QuickBooks
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._session: dict[str, Any] | None = None
        self._rp: Any = None              # the following three are used on the worker thread only
        self._connected = False
        self._ticket: Any = None

    def open(self) -> dict[str, Any]:
        if self._session is None:
            self._session = self._call(self._begin, self.open_timeout, QbNoSessionError(self.open_timeout))
        return self._session

    def process(self, qbxml: str) -> str:
        self.open()
        try:
            return self._call(lambda: str(self._rp.ProcessRequest(self._ticket, qbxml)), self.timeout)
        except QbConnectionError as e:
            # Anything but a bad request may mean the session is gone (file closed, QuickBooks quit): start fresh next time.
            if e.code != XML_PARSE_ERROR:
                self.close()
            raise

    def close(self) -> None:
        """Ends the session and stops the worker. Does nothing while a call is stuck in QuickBooks."""
        if self._worker is None or self.stuck:
            return
        try:
            self._call(self._end, 15)
        except QbConnectionError:
            pass
        if not self.stuck:
            self._jobs.put(None)
            self._worker = None
        self._session = None

    def _call(self, fn: Callable[[], Any], timeout: float, late: QbConnectionError | None = None) -> Any:
        if self.stuck:
            raise QbTimeoutError(timeout, BUSY)
        self._start()
        box: list[tuple[bool, Any]] = []
        done = threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            self.stuck = True
            raise late or QbTimeoutError(timeout)
        ok, value = box[0]
        if ok:
            return value
        raise value

    def _start(self) -> None:
        if self._worker is not None:
            return
        if sys.platform != "win32":
            raise QbConnectionError(
                "not-windows",
                "QuickBooks Desktop can only be reached from the Windows PC it runs on.",
                "Run qbd on that PC, or add --demo to try it with the sample company.",
            )
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError as e:
            raise QbConnectionError("no-pywin32", f"pywin32 is missing ({e}).", "Install it with: python -m pip install pywin32") from e
        self._worker = threading.Thread(target=self._serve, name="quickbooks", daemon=True)
        self._worker.start()

    def _serve(self) -> None:
        import pythoncom
        import pywintypes

        pythoncom.CoInitialize()
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                fn, box, done = job
                try:
                    box.append((True, fn()))
                except pywintypes.com_error as e:
                    box.append((False, com_error(e)))
                except Exception as e:  # noqa: BLE001 - handed to the caller
                    box.append((False, e))
                done.set()
        finally:
            pythoncom.CoUninitialize()

    def _begin(self) -> dict[str, Any]:
        import win32com.client

        try:
            if self._rp is None:
                self._rp = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
            if not self._connected:
                self._rp.OpenConnection2(self.app_id, self.app_name, LOCAL_QBD)
                self._connected = True
            self._ticket = self._rp.BeginSession(self.company_file, OPEN_ANY_MODE)
            return {"companyFile": str(self._rp.GetCurrentCompanyFileName(self._ticket))}
        except Exception:
            self._end()
            raise

    def _end(self) -> None:
        if self._ticket is not None:
            try:
                self._rp.EndSession(self._ticket)
            except Exception:  # noqa: BLE001 - the session may already be gone
                pass
            self._ticket = None
        if self._connected:
            try:
                self._rp.CloseConnection()
            except Exception:  # noqa: BLE001
                pass
            self._connected = False
        self._rp = None
