"""Bundle MCP resources from their canonical documentation sources."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithGuides(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / "reaper_audio" / "guides"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("HANDBOOK.md", "TIMING.md"):
            self.copy_file(str(root / "docs" / name), str(destination / name))


setup(cmdclass={"build_py": BuildPyWithGuides})
