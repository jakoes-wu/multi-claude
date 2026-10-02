"""v0.8.0 `export` / `import`：不含凭据的账号配置包（方案 docs/feature/feature-config-bundle.md §8）。"""

import io
import json
import os
import stat
import tarfile
import unittest

from helpers import CliTestCase


class BundleBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "src")
        self.ok("add", "dst")
        self.src = os.path.join(self.root, "src")
        self.dst = os.path.join(self.root, "dst")
        self.write(os.path.join(self.src, "settings.json"), '{"env": {"API_KEY": "sk-secret-1"}, "model": "opus"}\n')
        self.write(os.path.join(self.src, "CLAUDE.md"), "rules\n")
        self.write(os.path.join(self.src, "skills", "s1", "SKILL.md"), "skill\n")
        os.symlink("/etc/hosts", os.path.join(self.src, "skills", "s1", "link"))
        self.write(os.path.join(self.src, ".credentials.json"), '{"token": "cred-secret-2"}', 0o600)
        self.write(os.path.join(self.src, "projects", "p", "s.jsonl"), "{}\n")
        self.write(os.path.join(self.src, ".claude.json"), json.dumps({
            "oauthAccount": {"emailAddress": "me@example.com"},
            "mcpServers": {"gh": {"command": "gh-mcp", "headers": {"Authorization": "Bearer hdr-secret-3"}}}}))
        self.bundle = os.path.join(self.tmp, "src.tgz")

    def names(self, path=None):
        with tarfile.open(path or self.bundle) as tar:
            return sorted(tar.getnames())

    def member(self, name, path=None):
        with tarfile.open(path or self.bundle) as tar:
            return tar.extractfile(name).read().decode()

    def make_bundle(self, path, entries, manifest=None):
        """手工构造配置包：entries 是 (TarInfo, bytes|None)。"""
        with tarfile.open(path, "w:gz") as tar:
            data = json.dumps(manifest or {"format": "multi-claude-config", "version": 1}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for info, content in entries:
                tar.addfile(info, io.BytesIO(content) if content is not None else None)


class ExportTest(BundleBase):
    def test_t1_whitelist_only(self):
        result = self.ok("export", "src", self.bundle)
        names = self.names()
        self.assertIn("items/settings.json", names)
        self.assertIn("items/skills/s1/SKILL.md", names)
        self.assertIn("mcp-servers.json", names)
        for forbidden in ("credentials", "projects", ".claude.json", "items/skills/s1/link"):
            self.assertFalse([n for n in names if forbidden in n], forbidden)
        self.assertNotIn("me@example.com", self.member("mcp-servers.json"))
        self.assertIn("skipped 1 link(s)", result.out)
        self.assertEqual(stat.S_IMODE(os.stat(self.bundle).st_mode), 0o600)
        self.assertEqual(json.loads(self.member("manifest.json"))["items"], ["settings.json", "CLAUDE.md", "skills"])

    def test_t2_shared_link_existing_output_unregistered(self):
        os.rename(os.path.join(self.src, "CLAUDE.md"), os.path.join(self.tmp, "shared.md"))
        os.symlink(os.path.join(self.tmp, "shared.md"), os.path.join(self.src, "CLAUDE.md"))
        result = self.ok("export", "src", self.bundle)
        self.assertIn("skip CLAUDE.md (link)", result.out)
        self.assertNotIn("items/CLAUDE.md", self.names())
        self.assertEqual(self.run_cli("export", "src", self.bundle).code, 3)
        broken = os.path.join(self.tmp, "broken.tgz")
        os.symlink(os.path.join(self.tmp, "nowhere", "x.tgz"), broken)
        self.assertEqual(self.run_cli("export", "src", broken).code, 3)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nowhere")))
        self.assertEqual(self.run_cli("export", "nosuch", os.path.join(self.tmp, "n.tgz")).code, 1)
        self.assertEqual(self.run_cli("export", "src", os.path.join(self.tmp, "no", "dir.tgz")).code, 1)

    def test_t3_secret_key_names_without_values(self):
        result = self.ok("export", "src", self.bundle)
        self.assertIn("env.API_KEY", result.err)
        self.assertIn("mcpServers.gh.headers.Authorization", result.err)
        for value in ("sk-secret-1", "hdr-secret-3", "cred-secret-2"):
            self.assertNotIn(value, result.out + result.err)


class ImportTest(BundleBase):
    def setUp(self):
        super().setUp()
        self.ok("export", "src", self.bundle)
        self.write(os.path.join(self.dst, ".claude.json"), json.dumps({"numStartups": 3}))

    def test_t4_t5_import_and_rerun(self):
        self.ok("import", self.bundle, "dst")
        with open(os.path.join(self.dst, "settings.json")) as handle:
            self.assertIn("opus", handle.read())
        self.assertTrue(os.path.isfile(os.path.join(self.dst, "skills", "s1", "SKILL.md")))
        with open(os.path.join(self.dst, ".claude.json")) as handle:
            state = json.load(handle)
        self.assertEqual(state["numStartups"], 3)
        self.assertEqual(state["mcpServers"]["gh"]["command"], "gh-mcp")
        self.assertFalse(os.path.exists(os.path.join(self.dst, ".credentials.json")))
        again = self.ok("import", self.bundle, "dst")
        self.assertNotIn("create", again.out)
        self.assertIn("settings.json: unchanged", again.out)
        self.assertIn("mcp server gh: unchanged", again.out)
        self.assertFalse([n for n in os.listdir(self.dst) if n.startswith(".multi-claude-import")])

    def test_t6_conflicts_and_force(self):
        self.write(os.path.join(self.dst, "settings.json"), '{"model": "haiku"}\n')
        self.write(os.path.join(self.dst, ".claude.json"), json.dumps({"mcpServers": {"gh": {"command": "other"}}}))
        before = self.snapshot(self.dst)
        result = self.run_cli("import", self.bundle, "dst")
        self.assertEqual(result.code, 3, result)
        self.assertIn("settings.json already exists", result.err)
        self.assertIn("MCP server gh", result.err)
        self.assertEqual(before, self.snapshot(self.dst))
        self.ok("import", self.bundle, "dst", "--force")
        backups = [n for n in os.listdir(self.dst) if ".multi-claude-bak." in n]
        self.assertTrue(any(n.startswith("settings.json.") for n in backups), backups)
        self.assertTrue(any(n.startswith(".claude.json.") for n in backups), backups)
        with open(os.path.join(self.dst, ".claude.json")) as handle:
            self.assertEqual(json.load(handle)["mcpServers"]["gh"]["command"], "gh-mcp")

    def test_t7_shared_link_blocks_even_with_force(self):
        os.symlink(os.path.join(self.tmp, "shared-skills"), os.path.join(self.dst, "skills"))
        for extra in ((), ("--force",)):
            result = self.run_cli("import", self.bundle, "dst", *extra)
            self.assertEqual(result.code, 3, result)
            self.assertIn("shared link", result.err)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "settings.json")))

    def test_t8_malicious_bundles(self):
        def entry(name, kind=tarfile.REGTYPE, content=b"x", linkname=""):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = linkname
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            return info, content if kind == tarfile.REGTYPE else None
        cases = {
            "dotdot": [entry("items/skills/../../escape")],
            "absolute": [entry("/tmp/escape")],
            "symlink": [entry("items/skills/l", tarfile.SYMTYPE, linkname="/etc")],
            "hardlink": [entry("items/skills/h", tarfile.LNKTYPE, linkname="manifest.json")],
            "outside": [entry("items/.credentials.json")],
            "toplevel": [entry("other.txt")],
        }
        before = self.snapshot(self.dst)
        for label, entries in cases.items():
            with self.subTest(label=label):
                path = os.path.join(self.tmp, label + ".tgz")
                self.make_bundle(path, entries)
                self.assertEqual(self.run_cli("import", path, "dst").code, 1)
        bad_version = os.path.join(self.tmp, "v9.tgz")
        self.make_bundle(bad_version, [], manifest={"format": "multi-claude-config", "version": 9})
        self.assertEqual(self.run_cli("import", bad_version, "dst").code, 1)
        no_manifest = os.path.join(self.tmp, "nom.tgz")
        with tarfile.open(no_manifest, "w:gz") as tar:
            info, content = entry("items/CLAUDE.md")
            tar.addfile(info, io.BytesIO(content))
        self.assertEqual(self.run_cli("import", no_manifest, "dst").code, 1)
        self.assertEqual(self.run_cli("import", os.path.join(self.tmp, "missing.tgz"), "dst").code, 1)
        self.assertEqual(before, self.snapshot(self.dst))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "escape")))

    def test_t9_dry_run(self):
        before = self.snapshot(self.dst)
        result = self.ok("import", self.bundle, "dst", "--dry-run")
        self.assertIn("(dry-run) import settings.json: create", result.out)
        self.assertEqual(before, self.snapshot(self.dst))

    def test_t10_unregistered_and_unfinished_migration(self):
        self.assertEqual(self.run_cli("import", self.bundle, "nosuch").code, 1)
        with open(os.path.join(self.state, "migrate-journal.json"), "w") as handle:
            json.dump({"name": "main", "source": "s", "target": "t", "backup": "b", "mode": "rename",
                       "phase": "planned"}, handle)
        result = self.run_cli("import", self.bundle, "dst")
        self.assertEqual(result.code, 1, result)
        self.assertIn("unfinished migration", result.err)
        self.assertEqual(self.run_cli("export", "src", os.path.join(self.tmp, "x.tgz")).code, 1)


class ImportSafetyTest(BundleBase):
    def setUp(self):
        super().setUp()
        self.ok("export", "src", self.bundle)
        self.state_file = os.path.join(self.dst, ".claude.json")

    def test_invalid_claude_json_changes_nothing(self):
        self.write(self.state_file, "{broken")
        before = self.snapshot(self.dst)
        self.assertEqual(self.run_cli("import", self.bundle, "dst", "--force").code, 1)
        self.assertEqual(before, self.snapshot(self.dst))

    def test_mcp_servers_not_an_object_is_blocked(self):
        self.write(self.state_file, json.dumps({"mcpServers": ["x"]}))
        self.assertEqual(self.run_cli("import", self.bundle, "dst", "--force").code, 3)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "settings.json")))

    def test_mode_kept_and_backup_only_when_replacing(self):
        self.write(self.state_file, json.dumps({"numStartups": 1}), 0o600)
        result = self.ok("import", self.bundle, "dst")
        self.assertIn("may overwrite the imported MCP servers", result.err)
        self.assertEqual(stat.S_IMODE(os.stat(self.state_file).st_mode), 0o600)
        self.assertFalse([n for n in os.listdir(self.dst) if ".multi-claude-bak." in n])

    def test_symlinked_claude_json_is_written_through(self):
        real = self.write(os.path.join(self.tmp, "dotfiles", "claude.json"), json.dumps({"numStartups": 1}))
        os.symlink(real, self.state_file)
        self.ok("import", self.bundle, "dst")
        self.assertTrue(os.path.islink(self.state_file))
        with open(real) as handle:
            self.assertIn("gh", json.load(handle)["mcpServers"])

    def test_leftover_staging_removed_and_force_dry_run(self):
        leftover = os.path.join(self.dst, ".multi-claude-import.old")
        os.makedirs(os.path.join(leftover, "items"))
        self.write(os.path.join(self.dst, "settings.json"), '{"model": "haiku"}\n')
        result = self.ok("import", self.bundle, "dst", "--force", "--dry-run")
        self.assertIn("(dry-run) import settings.json: replace", result.out)
        with open(os.path.join(self.dst, "settings.json")) as handle:
            self.assertIn("haiku", handle.read())
        self.assertFalse([n for n in os.listdir(self.dst) if n.startswith(".multi-claude-import")])

    def test_bundle_shape_checks(self):
        def file_entry(name, content=b"x", mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            return info, content

        def dir_entry(name):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            return info, None
        cases = {
            "duplicate": [file_entry("items/CLAUDE.md"), file_entry("items/CLAUDE.md")],
            "settings-dir": [dir_entry("items/settings.json")],
            "skills-file": [file_entry("items/skills")],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                path = os.path.join(self.tmp, label + ".tgz")
                self.make_bundle(path, entries)
                self.assertEqual(self.run_cli("import", path, "dst").code, 1)
        loose = os.path.join(self.tmp, "loose.tgz")
        self.make_bundle(loose, [dir_entry("items/skills"), file_entry("items/skills/run.sh", mode=0o4777),
                                 file_entry("items/CLAUDE.md", mode=0o666)])
        self.ok("import", loose, "dst")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dst, "skills", "run.sh")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dst, "CLAUDE.md")).st_mode), 0o600)

    def test_missing_account_directory(self):
        import shutil
        shutil.rmtree(self.dst)
        self.assertEqual(self.run_cli("import", self.bundle, "dst").code, 1)


class ExportPathTest(BundleBase):
    def test_existing_file_dir_and_symlinked_parent(self):
        existing = self.write(os.path.join(self.tmp, "exists.tgz"), "keep")
        self.assertEqual(self.run_cli("export", "src", existing).code, 3)
        with open(existing) as handle:
            self.assertEqual(handle.read(), "keep")
        os.makedirs(os.path.join(self.tmp, "adir"))
        self.assertEqual(self.run_cli("export", "src", os.path.join(self.tmp, "adir")).code, 3)
        # 父路径含软链加 ..：必须落在内核解析出的真实位置（软链目标的父目录）
        real_parent = os.path.join(self.tmp, "real", "sub")
        os.makedirs(real_parent)
        os.symlink(real_parent, os.path.join(self.tmp, "link"))
        self.ok("export", "src", "link/../out.tgz", cwd=self.tmp)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "real", "out.tgz")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out.tgz")))
        self.assertEqual(self.run_cli("export").code, 2)


class DefaultIdentityTest(CliTestCase):
    def test_t11_default_identity_uses_home_claude_json(self):
        source = os.path.join(self.home, ".claude")
        self.write(os.path.join(source, "settings.json"), "{}\n")
        self.write(os.path.join(self.home, ".claude.json"),
                   json.dumps({"mcpServers": {"m": {"command": "m"}}, "numStartups": 1}))
        self.ok("migrate-default", "main")
        bundle = os.path.join(self.tmp, "main.tgz")
        self.ok("export", "main", bundle)
        with tarfile.open(bundle) as tar:
            self.assertIn('"m"', tar.extractfile("mcp-servers.json").read().decode())
        self.ok("add", "other")
        with open(os.path.join(self.home, ".claude.json"), "w") as handle:
            json.dump({"numStartups": 1}, handle)
        self.ok("import", bundle, "main")
        with open(os.path.join(self.home, ".claude.json")) as handle:
            self.assertIn("m", json.load(handle)["mcpServers"])


class HelpTest(CliTestCase):
    def test_t12_help_and_completion(self):
        self.assertIn("export, import", self.ok("--help").out)
        for shell in ("bash", "zsh", "fish"):
            script = self.ok("completion", shell).out
            self.assertIn("export", script)
            self.assertIn("import", script)


if __name__ == "__main__":
    unittest.main()
