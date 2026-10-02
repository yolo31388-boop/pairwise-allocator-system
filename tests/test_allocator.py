import pytest
from allocator.system import AllocatorSystem


class TestAllocatorSystem:

    def test_allocate(self):
        """分配内存不做对齐，跨平台访问崩溃"""
        obj = AllocatorSystem()
        result = obj.allocate()
        self.assertIsNotNone(result)

    def test_deallocate(self):
        """释放内存不归还池，直接free"""
        obj = AllocatorSystem()
        result = obj.deallocate()
        self.assertIsNotNone(result)

    def test_init_pool(self):
        """内存池不做大小分级，大对象小对象用同一个池"""
        obj = AllocatorSystem()
        result = obj.init_pool()
        self.assertIsNotNone(result)

    def test_defragment(self):
        """碎片整理不做移动，只是标记"""
        obj = AllocatorSystem()
        result = obj.defragment()
        self.assertIsNotNone(result)

    def test_get_stats(self):
        """内存统计只记录总量，不分类别"""
        obj = AllocatorSystem()
        result = obj.get_stats()
        self.assertIsNotNone(result)

