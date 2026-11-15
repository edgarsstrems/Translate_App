"""Optional dependency-free resource sampling, off the audio writer."""
import os
import time


class ResourceSampler:
    def __init__(self):
        self.wall, self.cpu = time.monotonic(), time.process_time()

    def sample(self):
        wall, cpu = time.monotonic(), time.process_time()
        result = {'process_cpu_percent_one_core': 100 * max(0, cpu - self.cpu) / max(.001, wall - self.wall),
                  'logical_cpus': os.cpu_count(), 'working_set_bytes': None}
        self.wall, self.cpu = wall, cpu
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('peak_rss', 'rss', 'peak_paged', 'paged',
                        'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            # GetCurrentProcess is a pseudo-handle; explicitly use pointer width.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                result['working_set_bytes'] = counters.rss
                result['peak_working_set_bytes'] = counters.peak_rss
        return result
