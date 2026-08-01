import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


class BumpVersionTest(unittest.TestCase):
    def run_bump(self, version=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy("Makefile", root / "Makefile")
            shutil.copy("pyproject.toml", root / "pyproject.toml")
            (root / "letee").mkdir()
            shutil.copy("letee/__init__.py", root / "letee/__init__.py")
            (root / "tools").mkdir()
            shutil.copy("tools/bump_version.py", root / "tools/bump_version.py")
            env = os.environ.copy()
            env.pop("VERSION", None)
            if version is not None:
                env["VERSION"] = version
            subprocess.run(
                ["make", "bump-version"], cwd=root, env=env, check=True,
                capture_output=True, text=True,
            )
            return (
                (root / "pyproject.toml").read_text(),
                (root / "letee/__init__.py").read_text(),
            )

    def test_bumps_patch_version_from_pyproject(self):
        current = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
        major, minor, patch = map(int, current.split("."))
        expected = f"{major}.{minor}.{patch + 1}"

        pyproject, package = self.run_bump()
        self.assertIn(f'version = "{expected}"', pyproject)
        self.assertEqual(package, f'__version__ = "{expected}"\n')

    def test_version_environment_variable_sets_version(self):
        pyproject, package = self.run_bump("2.4.6")
        self.assertIn('version = "2.4.6"', pyproject)
        self.assertEqual(package, '__version__ = "2.4.6"\n')


if __name__ == "__main__":
    unittest.main()
