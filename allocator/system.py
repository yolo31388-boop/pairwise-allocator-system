"""
pairwise-allocator-system - AllocatorSystem

This module contains a deliberately broken implementation.
Fix all bugs so that tests pass.
"""


class AllocatorSystem:
    def __init__(self):
        self.state = {}

    def allocate(self, *args, **kwargs):
        """BUG: 分配内存不做对齐，跨平台访问崩溃"""
        return None

    def deallocate(self, *args, **kwargs):
        """BUG: 释放内存不归还池，直接free"""
        return None

    def init_pool(self, *args, **kwargs):
        """BUG: 内存池不做大小分级，大对象小对象用同一个池"""
        return None

    def defragment(self, *args, **kwargs):
        """BUG: 碎片整理不做移动，只是标记"""
        return None

    def get_stats(self, *args, **kwargs):
        """BUG: 内存统计只记录总量，不分类别"""
        return None

