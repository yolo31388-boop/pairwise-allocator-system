"""
AllocatorSystem - aligned, pooled, tracked memory allocator.

- allocate():   aligned + pooled allocation, records size per allocation
- deallocate(): returns blocks to the pool, detects double free, zeroes memory
- init_pool():  size-classed pools with preallocation and a high-water mark
- defragment(): compacts by moving blocks, fixes up pointers, pauses the world
- get_stats():  per-category stats, leak detection, allocation history
"""

from __future__ import annotations

import threading
import time
from collections import deque


class DoubleFreeError(RuntimeError):
    """Raised when a handle is deallocated twice."""


class Allocation:
    """Handle to a live allocation. Its address is fixed up on defragment."""

    __slots__ = ("address", "size", "category", "buffer", "freed")

    def __init__(self, address: int, size: int, category: str, buffer: bytearray):
        self.address = address
        self.size = size
        self.category = category
        self.buffer = buffer
        self.freed = False

    def __repr__(self):
        state = "freed" if self.freed else "live"
        return f"<Allocation @{self.address:#x} size={self.size} {self.category} {state}>"


class AllocatorSystem:
    DEFAULT_ALIGNMENT = 16
    DEFAULT_SIZE_CLASSES = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)
    DEFAULT_PREALLOCATE = 8
    HISTORY_LIMIT = 1024

    def __init__(self, alignment=None, size_classes=None, high_water_mark=None):
        self.alignment = alignment or self.DEFAULT_ALIGNMENT
        self.size_classes = tuple(size_classes or self.DEFAULT_SIZE_CLASSES)
        self.high_water_mark = high_water_mark
        self._lock = threading.RLock()
        self._resumed = threading.Event()
        self._resumed.set()
        # free blocks per size class (pooling)
        self._pools = {sc: [] for sc in self.size_classes}
        self._preallocated = {sc: 0 for sc in self.size_classes}
        # live allocations: address -> Allocation (records the size of each)
        self._allocations = {}
        self._next_address = 0
        self._total_allocated = 0
        self._peak_allocated = 0
        self._warnings = []
        self._history = deque(maxlen=self.HISTORY_LIMIT)
        self._record_history()

    # -- helpers ---------------------------------------------------------

    def _align(self, value: int) -> int:
        return (value + self.alignment - 1) // self.alignment * self.alignment

    def _size_class_for(self, size: int) -> int:
        for sc in self.size_classes:
            if size <= sc:
                return sc
        return self._align(size)  # large objects get their own aligned class

    def _record_history(self):
        self._history.append((time.time(), self._total_allocated))

    def _check_watermark(self):
        if self.high_water_mark is not None and self._total_allocated > self.high_water_mark:
            self._warnings.append(
                f"high-water mark exceeded: {self._total_allocated} > {self.high_water_mark}"
            )

    # -- public API ------------------------------------------------------

    def allocate(self, size=1, category="default"):
        """Allocate `size` bytes, aligned and served from a size-class pool."""
        if size < 0:
            raise ValueError("size must be >= 0")
        self._resumed.wait()  # blocked while defragmentation pauses the world
        with self._lock:
            size_class = self._size_class_for(max(size, 1))
            pool = self._pools.setdefault(size_class, [])
            if pool:
                block = pool.pop()
            else:
                block = bytearray(size_class)
            address = self._align(self._next_address)
            self._next_address = address + size_class
            handle = Allocation(address, size, category, block)
            self._allocations[address] = handle
            self._total_allocated += size_class
            self._peak_allocated = max(self._peak_allocated, self._total_allocated)
            self._check_watermark()
            self._record_history()
            return handle

    def deallocate(self, handle=None):
        """Free an allocation: zero it, return the block to its pool.

        Raises DoubleFreeError on double free. With no handle, returns a
        summary of the pools instead of freeing anything.
        """
        with self._lock:
            if handle is None:
                return {
                    "live": len(self._allocations),
                    "pooled": {sc: len(blocks) for sc, blocks in self._pools.items()},
                }
            if getattr(handle, "freed", True) or handle.address not in self._allocations:
                raise DoubleFreeError(f"double free detected at {handle.address:#x}")
            size_class = len(handle.buffer)
            # zero the memory so no sensitive data lingers
            handle.buffer[:] = b"\x00" * size_class
            del self._allocations[handle.address]
            handle.freed = True
            # return the block to its size-class pool instead of dropping it
            self._pools.setdefault(size_class, []).append(handle.buffer)
            self._total_allocated -= size_class
            self._record_history()
            return True

    def init_pool(self, size_classes=None, preallocate=None, high_water_mark=None):
        """(Re)build size-classed pools, preallocating blocks per class."""
        with self._lock:
            if size_classes is not None:
                self.size_classes = tuple(size_classes)
            if high_water_mark is not None:
                self.high_water_mark = high_water_mark
            count = self.DEFAULT_PREALLOCATE if preallocate is None else preallocate
            summary = {}
            for sc in self.size_classes:
                pool = self._pools.setdefault(sc, [])
                while len(pool) < count:
                    pool.append(bytearray(sc))
                    self._preallocated[sc] = self._preallocated.get(sc, 0) + 1
                summary[sc] = {"free": len(pool), "preallocated": self._preallocated[sc]}
            return {
                "size_classes": self.size_classes,
                "pools": summary,
                "high_water_mark": self.high_water_mark,
            }

    def defragment(self):
        """Compact the heap: pause, move live blocks, fix up pointers, resume."""
        self._resumed.clear()  # pause new allocations while moving memory
        try:
            with self._lock:
                moved = 0
                new_allocations = {}
                new_next = 0
                for handle in sorted(self._allocations.values(), key=lambda h: h.address):
                    new_address = self._align(new_next)
                    new_next = new_address + len(handle.buffer)
                    if new_address != handle.address:
                        moved += 1
                    handle.address = new_address  # pointer fixup after the move
                    new_allocations[new_address] = handle
                self._allocations = new_allocations
                self._next_address = new_next
                return {
                    "moved": moved,
                    "live": len(self._allocations),
                    "compacted_to": self._next_address,
                }
        finally:
            self._resumed.set()  # resume the world

    def get_stats(self):
        """Categorized stats with leak detection and allocation history."""
        with self._lock:
            by_category = {}
            for handle in self._allocations.values():
                entry = by_category.setdefault(handle.category, {"count": 0, "bytes": 0})
                entry["count"] += 1
                entry["bytes"] += handle.size
            leaks = [
                {"address": h.address, "size": h.size, "category": h.category}
                for h in self._allocations.values()
            ]
            return {
                "total_allocated": self._total_allocated,
                "peak_allocated": self._peak_allocated,
                "by_category": by_category,
                "leak_count": len(leaks),
                "leaks": leaks,
                "history": list(self._history),
                "warnings": list(self._warnings),
                "high_water_mark": self.high_water_mark,
            }
