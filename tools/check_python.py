"""Lint and check formatting of Python sources, including the embedded helper."""

from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / "farcaster_cleanup_all.sh").read_text()
source = text.split("<<'FARCASTER_PYTHON'\n", 1)[1].rsplit("\nFARCASTER_PYTHON", 1)[0]
files = [
    "archive_viewer/server.py",
    "tests/test_cleanup.py",
    "tests/test_official_cli.py",
    "tests/test_viewer.py",
    "tools/check_python.py",
]
with tempfile.TemporaryDirectory() as folder:
    embedded = Path(folder) / "embedded_cleanup.py"
    embedded.write_text(source + "\n")
    paths = [str(embedded), *(str(root / name) for name in files)]
    subprocess.run([sys.executable, "-m", "ruff", "check", *paths], check=True)
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *paths], check=True
    )
