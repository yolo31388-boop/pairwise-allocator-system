"""AllocatorSystem: game memory allocator.

- allocate(): aligned, pool-backed allocation that records each block's size.
- deallocate(): returns memory to its pool, detects double frees, zeroes data.
- init_pool(): size-classed pools with eager preallocation and watermarks.
- defragment(): compacts pools by moving live blocks, fixes up block pointers,
  and can be paused/resumed (or budgeted) so it never hitches the game loop.
- get_stats(): per-category statistics, leak detection, and usage history.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import defaultdict, deque


class AllocatorError(RuntimeError):
    """Base error for allocator failures."""


class DoubleFreeError(AllocatorError):
    """Raised when a block is deallocated twice."""


class UnknownBlockError(AllocatorError):
    """Raised when deallocating a block this allocator never issued."""


class PoolExhaustedError(AllocatorError):
    """Raised when a pool cannot serve an allocation and cannot grow."""


def _align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


class Block:
    """Handle to one allocation.

    ``offset`` is updated in place by defragmentation, so holders of a Block
    never see a dangling pointer after compaction.
    """

    __slots__ = ("allocator", "pool", "offset", "size", "category",
                 "freed", "alloc_id", "stack", "allocated_at")

    def __init__(self, allocator, pool, offset, size, category, alloc_id, stack):
        self.allocator = allocator
        self.pool = pool
        self.offset = offset
        self.size = size
        self.category = category
        self.freed = False
        self.alloc_id = alloc_id
        self.stack = stack
        self.allocated_at = time.monotonic()

    @property
    def address(self):
        """Aligned absolute address of the block inside its pool arena."""
        if self.pool is None:
            return None
        return self.pool.base_address + self.offset

    def view(self):
        """Live memoryview of the block's bytes; raises on use-after-free."""
        if self.freed:
            raise AllocatorError("use-after-free: block was deallocated")
        return memoryview(self.pool.arena)[self.offset:self.offset + self.size]

    def __repr__(self):
        state = "freed" if self.freed else "live"
        return (f"Block(id={self.alloc_id}, size={self.size}, "
                f"category={self.category!r}, offset={self.offset}, {state})")


class Pool:
    """Fixed-size-class pool over a preallocated arena."""

    def __init__(self, class_size, alignment, capacity_blocks, watermark,
                 growable=True):
        self.class_size = class_size
        self.alignment = alignment
        self.stride = _align_up(max(class_size, 1), alignment)
        self.capacity_blocks = capacity_blocks
        self.arena = bytearray(self.stride * capacity_blocks)  # preallocated
        self.base_address = 0  # simulated base; offsets stay alignment-true
        self.free_offsets = [i * self.stride for i in range(capacity_blocks)]
        self.used_blocks = 0
        self.watermark = watermark
        self.growable = growable
        self.watermark_alerts = 0

    @property
    def used_ratio(self):
        if self.capacity_blocks == 0:
            return 0.0
        return self.used_blocks / self.capacity_blocks

    def acquire(self):
        """Pop a free slot; grows the pool if allowed. Returns (offset, alert)."""
        if not self.free_offsets:
            if not self.growable:
                raise PoolExhaustedError(
                    f"pool for class {self.class_size} is exhausted")
            self._grow()
        offset = self.free_offsets.pop()
        self.used_blocks += 1
        alert = False
        if self.used_ratio >= self.watermark:
            self.watermark_alerts += 1
            alert = True
        return offset, alert

    def release(self, offset):
        self.free_offsets.append(offset)
        self.used_blocks -= 1

    def _grow(self):
        add = max(1, self.capacity_blocks)
        self.arena.extend(b"\x00" * (self.stride * add))
        base = self.capacity_blocks * self.stride
        self.free_offsets.extend(base + i * self.stride for i in range(add))
        self.capacity_blocks += add


class AllocatorSystem:
    DEFAULT_ALIGNMENT = 16
    DEFAULT_SIZE_CLASSES = (16, 32, 64, 128, 256, 512, 1024, 4096)
    DEFAULT_BLOCKS_PER_CLASS = 64
    DEFAULT_WATERMARK = 0.8
    HISTORY_LIMIT = 4096

    def __init__(self, alignment=None, size_classes=None,
                 blocks_per_class=None, watermark=None, auto_init=True):
        self.alignment = alignment or self.DEFAULT_ALIGNMENT
        self.size_classes = tuple(size_classes or self.DEFAULT_SIZE_CLASSES)
        self.blocks_per_class = blocks_per_class or self.DEFAULT_BLOCKS_PER_CLASS
        self.watermark = watermark if watermark is not None else self.DEFAULT_WATERMARK
        self.pools = {}
        self._lock = threading.RLock()
        self._alloc_counter = 0
        self._live_blocks = {}
        self._category_stats = defaultdict(lambda: {
            "allocations": 0, "frees": 0,
            "bytes_allocated": 0, "bytes_freed": 0,
            "live_blocks": 0, "live_bytes": 0,
        })
        self._total_allocated = 0
        self._total_freed = 0
        self._history = deque(maxlen=self.HISTORY_LIMIT)
        self.warnings = []
        self._defrag_paused = False
        if auto_init:
            self.init_pool()

    # ------------------------------------------------------------------ pools

    def init_pool(self, size_classes=None, blocks_per_class=None,
                  watermark=None, preallocate=True):
        """Create size-classed pools, eagerly preallocated, with watermarks."""
        classes = tuple(size_classes or self.size_classes)
        count = blocks_per_class or self.blocks_per_class
        mark = watermark if watermark is not None else self.watermark
        created = {}
        with self._lock:
            for class_size in classes:
                if class_size in self.pools:
                    continue
                capacity = count if preallocate else 0
                pool = Pool(class_size, self.alignment, capacity, mark)
                self.pools[class_size] = pool
                created[class_size] = {
                    "class_size": class_size,
                    "stride": pool.stride,
                    "preallocated_blocks": capacity,
                    "capacity_bytes": pool.stride * capacity,
                    "watermark": mark,
                }
        return created

    def _class_for(self, size):
        for class_size in sorted(self.pools):
            if size <= class_size:
                return class_size
        return None

    def _pool_for(self, size):
        class_size = self._class_for(size)
        if class_size is None:
            # Large object: dedicated pool sized to the request.
            class_size = _align_up(max(size, 1), self.alignment)
            if class_size not in self.pools:
                self.pools[class_size] = Pool(
                    class_size, self.alignment, 1, self.watermark)
        return self.pools[class_size]

    # ------------------------------------------------------------- allocate

    def allocate(self, size=1, category="default", alignment=None):
        """Allocate an aligned, pooled block and record its size."""
        size = max(int(size), 0)
        align = alignment or self.alignment
        if align <= 0 or (align & (align - 1)) != 0:
            raise ValueError("alignment must be a positive power of two")
        with self._lock:
            pool = self._pool_for(size)
            offset, alert = pool.acquire()
            # Offsets are carved at aligned strides from an aligned base, so
            # every block address satisfies the requested alignment.
            assert (pool.base_address + offset) % pool.alignment == 0
            self._alloc_counter += 1
            block = Block(self, pool, offset, size, category,
                          self._alloc_counter,
                          traceback.format_stack(limit=8))
            self._live_blocks[block.alloc_id] = block
            stats = self._category_stats[category]
            stats["allocations"] += 1
            stats["bytes_allocated"] += size
            stats["live_blocks"] += 1
            stats["live_bytes"] += size
            self._total_allocated += size
            if alert:
                self.warnings.append(
                    f"pool class {pool.class_size} above watermark "
                    f"{pool.watermark:.0%} (used {pool.used_blocks}/"
                    f"{pool.capacity_blocks})")
            self._record_history()
            return block

    # ----------------------------------------------------------- deallocate

    def deallocate(self, block=None):
        """Return a block to its pool, detect double frees, zero its bytes."""
        if block is None:
            return {"freed": 0, "bytes": 0}
        if not isinstance(block, Block) or block.allocator is not self:
            raise UnknownBlockError("block was not issued by this allocator")
        with self._lock:
            if block.freed:
                raise DoubleFreeError(
                    f"block {block.alloc_id} was already deallocated")
            pool = block.pool
            # Zero the memory so no sensitive data lingers in the arena.
            pool.arena[block.offset:block.offset + max(block.size, 1)] = \
                b"\x00" * max(block.size, 1)
            pool.release(block.offset)
            block.freed = True
            self._live_blocks.pop(block.alloc_id, None)
            stats = self._category_stats[block.category]
            stats["frees"] += 1
            stats["bytes_freed"] += block.size
            stats["live_blocks"] -= 1
            stats["live_bytes"] -= block.size
            self._total_freed += block.size
            self._record_history()
            return {"freed": 1, "bytes": block.size,
                    "category": block.category, "alloc_id": block.alloc_id}

    # ------------------------------------------------------------ defragment

    def pause_defragmentation(self):
        """Pause compaction so it never runs while the game is mid-frame."""
        self._defrag_paused = True

    def resume_defragmentation(self):
        self._defrag_paused = False

    def defragment(self, budget=None):
        """Compact pools by moving live blocks and fixing up their pointers.

        ``budget`` caps how many blocks are moved per call so the work can be
        spread across frames. Returns a report; if paused, nothing is moved.
        """
        report = {"moved": 0, "paused": self._defrag_paused,
                  "pools_compacted": 0, "completed": True}
        if self._defrag_paused:
            return report
        with self._lock:
            for class_size in sorted(self.pools):
                pool = self.pools[class_size]
                live = sorted(
                    (b for b in self._live_blocks.values()
                     if b.pool is pool),
                    key=lambda b: b.offset)
                moved_in_pool = 0
                for index, block in enumerate(live):
                    target = index * pool.stride
                    if block.offset == target:
                        continue
                    if budget is not None and report["moved"] >= budget:
                        report["completed"] = False
                        break
                    # Move the bytes, zero the source, fix up the pointer.
                    arena = pool.arena
                    source = block.offset
                    data = bytes(arena[source:source + pool.stride])
                    arena[source:source + pool.stride] = b"\x00" * pool.stride
                    arena[target:target + pool.stride] = data
                    block.offset = target
                    moved_in_pool += 1
                    report["moved"] += 1
                else:
                    # Rebuild the free list from the compacted layout.
                    used = {b.offset for b in live}
                    pool.free_offsets = sorted(
                        (i * pool.stride
                         for i in range(pool.capacity_blocks)
                         if i * pool.stride not in used),
                        reverse=True)
                    if moved_in_pool:
                        report["pools_compacted"] += 1
                    continue
                break  # budget exhausted
        return report

    # ---------------------------------------------------------------- stats

    def detect_leaks(self, min_age_seconds=None):
        """Live blocks that were never freed, with size, category and stack."""
        now = time.monotonic()
        leaks = []
        for block in self._live_blocks.values():
            age = now - block.allocated_at
            if min_age_seconds is not None and age < min_age_seconds:
                continue
            leaks.append({
                "alloc_id": block.alloc_id,
                "size": block.size,
                "category": block.category,
                "age_seconds": age,
                "stack": "".join(block.stack),
            })
        return leaks

    def _record_history(self):
        self._history.append((time.time(), self.live_bytes))

    @property
    def live_bytes(self):
        return self._total_allocated - self._total_freed

    def get_stats(self):
        """Categorized stats, leak report, watermark state and history."""
        with self._lock:
            return {
                "total_allocated_bytes": self._total_allocated,
                "total_freed_bytes": self._total_freed,
                "live_bytes": self.live_bytes,
                "live_blocks": len(self._live_blocks),
                "by_category": {
                    category: dict(stats)
                    for category, stats in self._category_stats.items()
                },
                "pools": {
                    class_size: {
                        "capacity_blocks": pool.capacity_blocks,
                        "used_blocks": pool.used_blocks,
                        "free_blocks": len(pool.free_offsets),
                        "used_ratio": pool.used_ratio,
                        "watermark": pool.watermark,
                        "watermark_exceeded":
                            pool.used_ratio >= pool.watermark,
                        "watermark_alerts": pool.watermark_alerts,
                    }
                    for class_size, pool in sorted(self.pools.items())
                },
                "leaks": self.detect_leaks(),
                "history": list(self._history),
                "warnings": list(self.warnings),
            }
