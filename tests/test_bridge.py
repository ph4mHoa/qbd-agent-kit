"""The COM transport against a stand-in for pywin32 and the QuickBooks SDK, so it runs anywhere."""
import sys
import threading
import time
import types
import unittest
from unittest import mock

from qbd_agent_kit.bridge.com import ComTransport, com_error
from qbd_agent_kit.errors import QbConnectionError, QbNoSessionError, QbTimeoutError


class ComError(Exception):
    pass


def sdk_error(scode: int, message: str) -> ComError:
    """What pywin32 raises for a QuickBooks SDK error: DISP_E_EXCEPTION with the SDK's code in the exception info."""
    return ComError(-2147352567, "Exception occurred.", (0, "QBXMLRP2.RequestProcessor", message, None, 0, scode), None)


class FakeSdk:
    def __init__(self):
        self.calls: list[str] = []
        self.answer = lambda xml: "<QBXML/>"

    def OpenConnection2(self, app_id, app_name, kind):
        self.calls.append("OpenConnection2")

    def BeginSession(self, company_file, mode):
        self.calls.append("BeginSession")
        return "ticket"

    def GetCurrentCompanyFileName(self, ticket):
        return "C:\\Company.qbw"

    def ProcessRequest(self, ticket, xml):
        self.calls.append("ProcessRequest")
        return self.answer(xml)

    def EndSession(self, ticket):
        self.calls.append("EndSession")

    def CloseConnection(self):
        self.calls.append("CloseConnection")


class Bridge(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSdk()
        client = types.SimpleNamespace(Dispatch=lambda progid: self.sdk)
        modules = {
            "pythoncom": types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None),
            "pywintypes": types.SimpleNamespace(com_error=ComError),
            "win32com": types.SimpleNamespace(client=client),
            "win32com.client": client,
        }
        for patch in (mock.patch.dict(sys.modules, modules), mock.patch.object(sys, "platform", "win32")):
            patch.start()
            self.addCleanup(patch.stop)
        self.t = ComTransport("", "qbd test", "", open_timeout=5, timeout=0.3)
        self.addCleanup(self.t.close)

    def test_a_session_is_opened_once_and_ended_on_close(self):
        self.assertEqual(self.t.open(), {"companyFile": "C:\\Company.qbw"})
        self.t.process("<a/>")
        self.t.process("<b/>")
        self.t.close()
        self.assertEqual(self.sdk.calls, ["OpenConnection2", "BeginSession", "ProcessRequest", "ProcessRequest", "EndSession", "CloseConnection"])

    def test_sdk_errors_carry_quickbooks_code_and_message_and_end_the_session(self):
        def fail(xml):
            raise sdk_error(-2147220458, "Could not find the company file.")  # 0x80040416

        self.sdk.answer = fail
        with self.assertRaises(QbConnectionError) as caught:
            self.t.process("<a/>")
        self.assertEqual(caught.exception.code, "0x80040416")
        self.assertEqual(caught.exception.message, "Could not find the company file.")
        self.assertTrue(caught.exception.needs_person)
        self.assertEqual(self.sdk.calls[-2:], ["EndSession", "CloseConnection"])
        self.sdk.answer = lambda xml: "<ok/>"
        self.assertEqual(self.t.process("<a/>"), "<ok/>")
        self.assertEqual(self.sdk.calls.count("BeginSession"), 2)

    def test_a_request_quickbooks_cannot_parse_keeps_the_session(self):
        def bad(xml):
            raise sdk_error(-2147220480, "There was an error when parsing the provided XML file.")  # 0x80040400

        self.sdk.answer = bad
        with self.assertRaises(QbConnectionError) as caught:
            self.t.process("<a/>")
        self.assertEqual(caught.exception.code, "0x80040400")
        self.assertNotIn("EndSession", self.sdk.calls)

    def test_after_a_timeout_later_calls_fail_at_once(self):
        release = threading.Event()
        self.sdk.answer = lambda xml: release.wait(5) and "<late/>"
        self.addCleanup(release.set)
        with self.assertRaises(QbTimeoutError):
            self.t.process("<slow/>")
        self.assertTrue(self.t.stuck)
        started = time.monotonic()
        with self.assertRaises(QbTimeoutError) as caught:
            self.t.process("<next/>")
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIn("still busy", caught.exception.message)
        self.t.close()  # must not wait for the stuck call
        self.assertNotIn("EndSession", self.sdk.calls)

    def test_a_session_that_does_not_open_in_time_asks_for_a_person(self):
        release = threading.Event()
        self.sdk.BeginSession = lambda company_file, mode: release.wait(5) and "ticket"
        self.addCleanup(release.set)
        t = ComTransport("", "qbd test", "", open_timeout=0.3, timeout=0.3)
        with self.assertRaises(QbNoSessionError) as caught:
            t.open()
        self.assertEqual(caught.exception.code, "no-session")
        self.assertTrue(caught.exception.needs_person)
        self.assertIn("Application permission", caught.exception.hint)
        self.assertTrue(t.stuck)

    def test_the_main_thread_joins_the_multithreaded_apartment(self):
        # With the default (single-threaded) the SDK hangs before it reaches QuickBooks: found on Windows 10 with
        # QuickBooks Enterprise 24, where a worker-only set-up and this one both reached QuickBooks.
        self.assertEqual(sys.coinit_flags, 0)

    def worker_steps(self, desktop: str) -> list[str]:
        """What the worker does before its first SDK call when it starts on the named desktop."""
        steps: list[str] = []
        default = types.SimpleNamespace(SetThreadDesktop=lambda: steps.append("SetThreadDesktop"))
        modules = {
            "pythoncom": types.SimpleNamespace(CoInitialize=lambda: steps.append("CoInitialize"), CoUninitialize=lambda: None),
            "win32api": types.SimpleNamespace(GetCurrentThreadId=lambda: 1),
            "win32con": types.SimpleNamespace(UOI_NAME=2, MAXIMUM_ALLOWED=0x2000000),
            "win32service": types.SimpleNamespace(
                GetThreadDesktop=lambda thread: desktop,
                GetUserObjectInformation=lambda handle, index: handle,
                OpenDesktop=lambda name, flags, inherit, access: {"Default": default}[name],
            ),
        }
        with mock.patch.dict(sys.modules, modules):
            t = ComTransport("", "qbd test", "", open_timeout=5, timeout=0.3)
            t.open()
            t.close()
        return steps

    def test_a_worker_on_another_desktop_moves_to_the_default_one_before_it_joins_com(self):
        # From another desktop the SDK answered "Could not start QuickBooks" while QuickBooks was open: found on
        # Windows 10 with QuickBooks Enterprise 24, where agy runs commands on a desktop of its own.
        self.assertEqual(self.worker_steps("agent"), ["SetThreadDesktop", "CoInitialize"])
        self.assertEqual(self.worker_steps("Default"), ["CoInitialize"])

    def test_com_error_without_exception_info(self):
        e = com_error(ComError(-2147221005, "Invalid class string", None, None))
        self.assertEqual(e.code, "0x800401F3")
        self.assertIn("not installed", e.hint)


class NotWindows(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "on Windows the real SDK would be called")
    def test_other_systems_get_a_clear_error(self):
        with self.assertRaises(QbConnectionError) as caught:
            ComTransport("", "qbd test", "", 1, 1).open()
        self.assertEqual(caught.exception.code, "not-windows")


if __name__ == "__main__":
    unittest.main()
