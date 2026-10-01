"""fsutil 的边界行为：删除含无权限子目录的目录树、遍历时不得静默跳过读不了的目录。"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from multi_claude.fsutil import build_manifest, remove_path  # noqa: E402


class FsutilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcx-fs-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _locked_tree(self):
        root = os.path.join(self.tmp, "tree")
        inner = os.path.join(root, "a", "locked")
        os.makedirs(os.path.join(inner, "deeper"))
        with open(os.path.join(inner, "deeper", "f"), "w") as handle:
            handle.write("x")
        os.chmod(os.path.join(inner, "deeper"), 0)
        os.chmod(inner, 0)
        return root

    def test_remove_path_handles_unreadable_directories(self):
        root = self._locked_tree()
        remove_path(root)
        self.assertFalse(os.path.lexists(root))

    def test_remove_path_does_not_follow_links(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as handle:
            handle.write("x")
        root = os.path.join(self.tmp, "tree")
        os.makedirs(root)
        os.symlink(outside, os.path.join(root, "link"))
        remove_path(root)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))

    def test_manifest_raises_on_unreadable_directory(self):
        root = self._locked_tree()
        with self.assertRaises(PermissionError):
            build_manifest(root)


if __name__ == "__main__":
    unittest.main()
