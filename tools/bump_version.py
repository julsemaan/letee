import os
import re
import tomllib
from pathlib import Path


pyproject_path = Path("pyproject.toml")
package_path = Path("letee/__init__.py")
current = tomllib.loads(pyproject_path.read_text())["project"]["version"]
version = os.getenv("VERSION")
if version is None:
    major, minor, patch = map(int, current.split("."))
    version = f"{major}.{minor}.{patch + 1}"
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit(f"VERSION must use MAJOR.MINOR.PATCH format: {version!r}")

pyproject = pyproject_path.read_text()
pyproject, count = re.subn(
    r'(?m)^(version = ")[^"]+(".*)$', rf"\g<1>{version}\2", pyproject, count=1
)
if count != 1:
    raise SystemExit("project version not found in pyproject.toml")
pyproject_path.write_text(pyproject)
package_path.write_text(f'__version__ = "{version}"\n')
print(version)
