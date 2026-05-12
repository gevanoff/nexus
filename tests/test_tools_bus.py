import unittest
from services.gateway.app.tools_bus import ToolsBus

class TestThreadSafety(unittest.TestCase):
    def test_concurrent_access(self):
        bus = ToolsBus()
        # Add thread-safety verification logic here

test = TestThreadSafety()
