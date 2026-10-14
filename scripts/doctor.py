"""Report native build and test prerequisites without changing the host."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import sqlite3
import ssl
import subprocess
import sys


def main() -> int:
    tools = {}
    missing = []
    for name, command in (("make", "make"), ("compiler", os.environ.get("CXX", "c++")),
                          ("openssl", "openssl"), ("git", "git")):
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = []
        path = shutil.which(arguments[0]) if arguments else None
        if path is None:
            missing.append(command)
            tools[name] = {"available": False}
            continue
        try:
            result = subprocess.run(
                [path, *arguments[1:], "version" if name == "openssl" else "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (result.stdout or result.stderr).splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            version = "version unavailable"
        tools[name] = {"available": True, "path": path, "version": version}
    if sys.version_info < (3, 11):
        missing.append("Python 3.11+")
    if importlib.util.find_spec("pytest") is None:
        missing.append(f"pytest in {sys.executable}")
    if not ssl.HAS_TLSv1_3:
        missing.append("Python SSL with TLS 1.3")
    prefixes = [os.environ.get("OPENSSL_PREFIX"), "/opt/homebrew/opt/openssl@3", "/usr/local/opt/openssl@3"]
    prefix = next((p for p in prefixes if p and (Path(p) / "include/openssl/ssl.h").exists()), None)
    print(json.dumps({
        "platform": platform.platform(), "machine": platform.machine(),
        "python": sys.version, "python_executable": sys.executable,
        "sqlite": sqlite3.sqlite_version, "python_ssl": ssl.OPENSSL_VERSION,
        "tools": tools, "openssl_prefix": prefix,
        "missing": missing, "native_prerequisites_available": not missing,
        "next_check": "make check (verifies C++20 and OpenSSL headers/linkage)",
    }, indent=2))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
