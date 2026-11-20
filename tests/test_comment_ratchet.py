import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CommentRatchet(unittest.TestCase):
    def test_no_new_comments(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "comment_ratchet.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
