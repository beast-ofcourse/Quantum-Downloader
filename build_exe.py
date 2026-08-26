"""PyInstaller build script for the standalone `ytchannel` Windows executable.

Run with:  python build_exe.py
Produces dist/ytchannel.exe (one-file, console). The entry point is the
Typer `main` in ytchannel.cli.
"""
from PyInstaller.__main__ import run

if __name__ == "__main__":
    run(
        [
            "ytchannel.cli:main",
            "--onefile",
            "--console",
            "--name",
            "ytchannel",
            "--clean",
        ]
    )
