"""Compatibility entry point: package only; use build.py to deploy."""
import subprocess
import sys
from pathlib import Path

subprocess.run([sys.executable, str(Path(__file__).with_name("build.py")), "--package-only"], check=True)
