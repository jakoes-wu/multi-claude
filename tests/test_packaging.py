"""打包与首页素材的静态检查：PyPI 元数据、README 演示图地址、release 包排除素材。"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_URL = "https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/docs/assets/demo.gif"


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class PackagingTest(unittest.TestCase):
    def test_pyproject_metadata(self):
        # PyPI 上 multi-claude 已被占用，发行名改为 multi-claude-cli；命令名与导入包不能跟着变
        text = read("pyproject.toml")
        self.assertIn('name = "multi-claude-cli"', text)
        self.assertIn('multi-claude = "multi_claude.cli:main"', text)
        self.assertIn("Changelog = ", text)
        self.assertIn("Issues = ", text)

    def test_readme_demo_points_at_existing_file(self):
        # README 用绝对地址引用演示图，PyPI 页面才能显示；文件本身必须在仓库里
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs", "assets", "demo.gif")))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs", "assets", "social-preview.png")))
        for name in ("README.md", "README.zh-CN.md"):
            self.assertIn(DEMO_URL, read(name), name)

    @unittest.skipUnless(shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")),
                         "needs a git checkout")
    def test_git_archive_excludes_assets(self):
        # release 包由 git archive 生成；.gitattributes 的 export-ignore 让图片不进安装包
        with tempfile.TemporaryDirectory() as tmp:
            # 用临时 index 把工作区（含未跟踪、未忽略的文件）写成一棵树，测试结果与是否已提交无关；
            # 不碰仓库真正的 index
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
            git = ["git", "-C", ROOT]
            subprocess.run(git + ["add", "-A"], env=env, check=True)
            tree = subprocess.run(git + ["write-tree"], env=env, capture_output=True,
                                  text=True, check=True).stdout.strip()
            listed = subprocess.run(git + ["ls-tree", "-r", "--name-only", tree],
                                    capture_output=True, text=True, check=True).stdout.split()
            # 对照：图片确实在树里，下面它没进包才能说明是 export-ignore 生效
            self.assertIn("docs/assets/demo.gif", listed)
            out = os.path.join(tmp, "a.tar")
            subprocess.run(git + ["archive", "-o", out, tree], check=True)
            with tarfile.open(out) as tar:
                names = tar.getnames()
        self.assertIn("src/multi_claude/cli.py", names)
        self.assertFalse([n for n in names if n.startswith("docs/assets")])


if __name__ == "__main__":
    unittest.main()
