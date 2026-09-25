"""Real-time feed of every network send/receive on this PC, with process ID and size.

Windows publishes this through the "Microsoft-Windows-Kernel-Network" ETW
(Event Tracing for Windows) provider -- it's what Resource Monitor uses for its
per-process network view. We start a private real-time trace session, enable
that provider, and get a callback per event. Needs Administrator rights.

Event payload layout (confirmed via `wevtutil gp Microsoft-Windows-Kernel-Network /ge /gm`,
whose messages read "TCPv4: %2 bytes transmitted from %4:%6 to %3:%5"):
    %1 PID  UInt32
    %2 size UInt32
    %3 daddr  4 bytes (IPv4) or 16 bytes (IPv6)
    %4 saddr  same
    %5 dport, %6 sport ...
We only need PID, size and the two addresses.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import uuid
from ctypes import POINTER, Structure, WINFUNCTYPE, byref, c_int64, c_uint64, c_ulong, c_long
from ctypes import c_ubyte, c_ushort, c_void_p, c_wchar, c_wchar_p, sizeof

log = logging.getLogger(__name__)

KERNEL_NETWORK_PROVIDER = "7dd42a49-5329-4832-8dfd-43d979153a88"

# Event id -> (is_send, is_ipv6). TCP and UDP both count: QUIC (YouTube,
# most Google traffic), games and many video streams run over UDP.
EVENTS = {
    10: (True, False), 11: (False, False),   # TCPv4 send / receive
    26: (True, True), 27: (False, True),     # TCPv6
    42: (True, False), 43: (False, False),   # UDPv4
    58: (True, True), 59: (False, True),     # UDPv6
}
KEYWORD_IPV4 = 0x10
KEYWORD_IPV6 = 0x20

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_CONTROL_STOP = 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
TRACE_LEVEL_VERBOSE = 5
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223


class GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        """Build a GUID struct from its string form."""
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class WNODE_HEADER(Structure):
    _fields_ = [
        ("BufferSize", c_ulong), ("ProviderId", c_ulong), ("HistoricalContext", c_uint64),
        ("TimeStamp", c_int64), ("Guid", GUID), ("ClientContext", c_ulong), ("Flags", c_ulong),
    ]


class EVENT_TRACE_PROPERTIES(Structure):
    _fields_ = [
        ("Wnode", WNODE_HEADER), ("BufferSize", c_ulong), ("MinimumBuffers", c_ulong),
        ("MaximumBuffers", c_ulong), ("MaximumFileSize", c_ulong), ("LogFileMode", c_ulong),
        ("FlushTimer", c_ulong), ("EnableFlags", c_ulong), ("AgeLimit", c_long),
        ("NumberOfBuffers", c_ulong), ("FreeBuffers", c_ulong), ("EventsLost", c_ulong),
        ("BuffersWritten", c_ulong), ("LogBuffersLost", c_ulong), ("RealTimeBuffersLost", c_ulong),
        ("LoggerThreadId", c_void_p), ("LogFileNameOffset", c_ulong), ("LoggerNameOffset", c_ulong),
    ]


class _PropsBuffer(Structure):
    """EVENT_TRACE_PROPERTIES must be followed in memory by space for the session name."""
    _fields_ = [("props", EVENT_TRACE_PROPERTIES), ("logger_name", c_wchar * 256), ("log_file", c_wchar * 256)]


class EVENT_TRACE_HEADER(Structure):
    _fields_ = [
        ("Size", c_ushort), ("FieldTypeFlags", c_ushort), ("Version", c_ulong), ("ThreadId", c_ulong),
        ("ProcessId", c_ulong), ("TimeStamp", c_int64), ("Guid", GUID), ("ProcessorTime", c_uint64),
    ]


class EVENT_TRACE(Structure):
    _fields_ = [
        ("Header", EVENT_TRACE_HEADER), ("InstanceId", c_ulong), ("ParentInstanceId", c_ulong),
        ("ParentGuid", GUID), ("MofData", c_void_p), ("MofLength", c_ulong), ("ClientContext", c_ulong),
    ]


class SYSTEMTIME(Structure):
    _fields_ = [(n, c_ushort) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMs")]


class TIME_ZONE_INFORMATION(Structure):
    _fields_ = [
        ("Bias", c_long), ("StandardName", c_wchar * 32), ("StandardDate", SYSTEMTIME), ("StandardBias", c_long),
        ("DaylightName", c_wchar * 32), ("DaylightDate", SYSTEMTIME), ("DaylightBias", c_long),
    ]


class TRACE_LOGFILE_HEADER(Structure):
    _fields_ = [
        ("BufferSize", c_ulong), ("Version", c_ulong), ("ProviderVersion", c_ulong),
        ("NumberOfProcessors", c_ulong), ("EndTime", c_int64), ("TimerResolution", c_ulong),
        ("MaximumFileSize", c_ulong), ("LogFileMode", c_ulong), ("BuffersWritten", c_ulong),
        ("LogInstanceGuid", GUID), ("LoggerName", c_void_p), ("LogFileName", c_void_p),
        ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", c_int64), ("PerfFreq", c_int64),
        ("StartTime", c_int64), ("ReservedFlags", c_ulong), ("BuffersLost", c_ulong),
    ]


class EVENT_DESCRIPTOR(Structure):
    _fields_ = [
        ("Id", c_ushort), ("Version", c_ubyte), ("Channel", c_ubyte), ("Level", c_ubyte),
        ("Opcode", c_ubyte), ("Task", c_ushort), ("Keyword", c_uint64),
    ]


class EVENT_HEADER(Structure):
    _fields_ = [
        ("Size", c_ushort), ("HeaderType", c_ushort), ("Flags", c_ushort), ("EventProperty", c_ushort),
        ("ThreadId", c_ulong), ("ProcessId", c_ulong), ("TimeStamp", c_int64), ("ProviderId", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR), ("ProcessorTime", c_uint64), ("ActivityId", GUID),
    ]


class ETW_BUFFER_CONTEXT(Structure):
    _fields_ = [("ProcessorIndex", c_ushort), ("LoggerId", c_ushort)]


class EVENT_RECORD(Structure):
    _fields_ = [
        ("EventHeader", EVENT_HEADER), ("BufferContext", ETW_BUFFER_CONTEXT),
        ("ExtendedDataCount", c_ushort), ("UserDataLength", c_ushort),
        ("ExtendedData", c_void_p), ("UserData", c_void_p), ("UserContext", c_void_p),
    ]


EVENT_RECORD_CALLBACK = WINFUNCTYPE(None, POINTER(EVENT_RECORD))


class EVENT_TRACE_LOGFILEW(Structure):
    _fields_ = [
        ("LogFileName", c_wchar_p), ("LoggerName", c_wchar_p), ("CurrentTime", c_int64),
        ("BuffersRead", c_ulong), ("ProcessTraceMode", c_ulong), ("CurrentEvent", EVENT_TRACE),
        ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", c_void_p), ("BufferSize", c_ulong),
        ("Filled", c_ulong), ("EventsLost", c_ulong), ("EventRecordCallback", EVENT_RECORD_CALLBACK),
        ("IsKernelTrace", c_ulong), ("Context", c_void_p),
    ]


_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_StartTraceW = _advapi.StartTraceW
_StartTraceW.argtypes = [POINTER(c_uint64), c_wchar_p, POINTER(EVENT_TRACE_PROPERTIES)]
_StartTraceW.restype = c_ulong
_ControlTraceW = _advapi.ControlTraceW
_ControlTraceW.argtypes = [c_uint64, c_wchar_p, POINTER(EVENT_TRACE_PROPERTIES), c_ulong]
_ControlTraceW.restype = c_ulong
_EnableTraceEx2 = _advapi.EnableTraceEx2
_EnableTraceEx2.argtypes = [c_uint64, POINTER(GUID), c_ulong, c_ubyte, c_uint64, c_uint64, c_ulong, c_void_p]
_EnableTraceEx2.restype = c_ulong
_OpenTraceW = _advapi.OpenTraceW
_OpenTraceW.argtypes = [POINTER(EVENT_TRACE_LOGFILEW)]
_OpenTraceW.restype = c_uint64
_ProcessTrace = _advapi.ProcessTrace
_ProcessTrace.argtypes = [POINTER(c_uint64), c_ulong, c_void_p, c_void_p]
_ProcessTrace.restype = c_ulong
_CloseTrace = _advapi.CloseTrace
_CloseTrace.argtypes = [c_uint64]
_CloseTrace.restype = c_ulong


def _new_props() -> _PropsBuffer:
    """Session settings: real-time only (no log file), generous buffers to avoid dropping events."""
    buf = _PropsBuffer()
    p = buf.props
    p.Wnode.BufferSize = sizeof(buf)
    p.Wnode.Flags = WNODE_FLAG_TRACED_GUID
    p.Wnode.ClientContext = 1  # QPC timestamps
    p.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
    p.BufferSize = 256  # KB per buffer
    p.MinimumBuffers = 8
    p.MaximumBuffers = 128
    p.FlushTimer = 1  # deliver events at least once a second
    p.LoggerNameOffset = _PropsBuffer.logger_name.offset
    return buf


class NetworkEventTrace:
    """Accumulates bytes per (pid, direction, address pair) until drain() is called.

    The callback runs on the ETW thread for every send/receive -- tens of
    thousands per second during a big download -- so it does the bare minimum:
    slice the payload and add to a dict. Address classification happens later
    in drain(), once per 10 seconds, instead of per event.
    """

    SESSION_NAME = "HogWatch-KernelNetwork"

    def __init__(self):
        """Prepare (but don't start) the trace session."""
        self._acc: dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._session = c_uint64(0)
        self._trace = c_uint64(INVALID_PROCESSTRACE_HANDLE)
        self._thread: threading.Thread | None = None
        # Keep a reference so the C callback isn't garbage-collected while ETW still calls it.
        self._callback = EVENT_RECORD_CALLBACK(self._on_event)
        self.events_seen = 0
        # PIDs seen for the first time, so their names can be looked up while the
        # process is still alive (a download tool can exit within seconds).
        self._known_pids: set[int] = set()
        self._new_pids: set[int] = set()

    def start(self) -> None:
        """Start the session and the consumer thread. Raises PermissionError without admin."""
        props = _new_props()
        rc = _StartTraceW(byref(self._session), self.SESSION_NAME, byref(props.props))
        if rc == ERROR_ALREADY_EXISTS:
            # Left over from a previous run that was killed; stop it and take over.
            self._stop_by_name()
            props = _new_props()
            rc = _StartTraceW(byref(self._session), self.SESSION_NAME, byref(props.props))
        if rc == ERROR_ACCESS_DENIED:
            raise PermissionError("Per-program tracking needs Administrator rights")
        if rc != ERROR_SUCCESS:
            raise OSError(rc, f"StartTrace failed ({rc})")

        guid = GUID.parse(KERNEL_NETWORK_PROVIDER)
        rc = _EnableTraceEx2(self._session, byref(guid), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                             TRACE_LEVEL_VERBOSE, KEYWORD_IPV4 | KEYWORD_IPV6, 0, 0, None)
        if rc != ERROR_SUCCESS:
            self.stop()
            raise OSError(rc, f"EnableTraceEx2 failed ({rc})")

        logfile = EVENT_TRACE_LOGFILEW()
        logfile.LoggerName = self.SESSION_NAME
        logfile.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
        logfile.EventRecordCallback = self._callback
        self._logfile = logfile  # must outlive OpenTrace
        handle = _OpenTraceW(byref(logfile))
        if handle == INVALID_PROCESSTRACE_HANDLE:
            err = ctypes.get_last_error()
            self.stop()
            raise OSError(err, f"OpenTrace failed ({err})")
        self._trace = c_uint64(handle)
        self._thread = threading.Thread(target=self._pump, name="etw", daemon=True)
        self._thread.start()
        log.info("per-program network tracing started")

    def _pump(self) -> None:
        """Blocks inside ProcessTrace, which invokes our callback, until the session stops."""
        rc = _ProcessTrace(byref(self._trace), 1, None, None)
        if rc not in (ERROR_SUCCESS, ERROR_CANCELLED):
            log.warning("ProcessTrace ended with code %s", rc)

    def _on_event(self, rec_ptr) -> None:
        """ETW callback: record PID, direction, size and the raw address bytes."""
        try:
            rec = rec_ptr.contents
            info = EVENTS.get(rec.EventHeader.EventDescriptor.Id)
            if info is None:
                return
            is_send, v6 = info
            need = 40 if v6 else 16
            if rec.UserDataLength < need:
                return
            data = ctypes.string_at(rec.UserData, need)
            pid, size = struct.unpack_from("<II", data, 0)
            if v6:
                a, b = data[8:24], data[24:40]
            else:
                a, b = data[8:12], data[12:16]
            key = (pid, is_send, a, b)
            with self._lock:
                self._acc[key] = self._acc.get(key, 0) + size
                self.events_seen += 1
                if pid not in self._known_pids:
                    self._known_pids.add(pid)
                    self._new_pids.add(pid)
        except Exception:  # never let an exception escape into the C caller
            pass

    def take_new_pids(self) -> set[int]:
        """PIDs that started using the network since the last call."""
        with self._lock:
            new, self._new_pids = self._new_pids, set()
        return new

    def forget_pids(self, pids) -> None:
        """Forget exited PIDs so a reused PID is treated as new."""
        with self._lock:
            self._known_pids.difference_update(pids)

    def drain(self) -> dict[tuple, int]:
        """Return and reset the totals collected since the last call.

        Keys are (pid, is_send, addr_a, addr_b) with raw packed addresses;
        values are byte counts.
        """
        with self._lock:
            acc, self._acc = self._acc, {}
        return acc

    def _stop_by_name(self) -> None:
        """Stop a session with our name, whoever started it."""
        props = _new_props()
        _ControlTraceW(0, self.SESSION_NAME, byref(props.props), EVENT_TRACE_CONTROL_STOP)

    def stop(self) -> None:
        """Stop the session; ProcessTrace then returns and the thread exits."""
        props = _new_props()
        _ControlTraceW(self._session.value, None, byref(props.props), EVENT_TRACE_CONTROL_STOP)
        if self._trace.value != INVALID_PROCESSTRACE_HANDLE:
            _CloseTrace(self._trace)
            self._trace = c_uint64(INVALID_PROCESSTRACE_HANDLE)
