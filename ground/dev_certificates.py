"""Generate disposable development certificates; never use this CA in production."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    openssl = shutil.which("openssl") or "openssl"

    def run(*args: str) -> None:
        subprocess.run([openssl, *args], cwd=directory, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=OrbitalLink development CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name, purpose in (("ground", "serverAuth"), ("flight", "clientAuth"),
                          ("unauthorized", "clientAuth")):
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key",
            "-out", f"{name}.csr", "-subj", f"/CN={name}")
        extensions = directory / f"{name}.ext"
        extensions.write_text(f"subjectAltName=DNS:{name}.orbital.test\n"
                              f"extendedKeyUsage={purpose}\nkeyUsage=digitalSignature\n"
                              "basicConstraints=critical,CA:FALSE\n")
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "2", "-out", f"{name}.pem", "-extfile", f"{name}.ext")
    for path in directory.glob("*.key"):
        os.chmod(path, 0o600)
    (directory / "index").write_text("")
    (directory / "serial").write_text("1000\n")
    (directory / "newcerts").mkdir(exist_ok=True)
    (directory / "signing.cnf").write_text("""[ca]
default_ca=development
[development]
database=index
serial=serial
new_certs_dir=newcerts
certificate=ca.pem
private_key=ca.key
default_md=sha256
policy=names
unique_subject=no
[names]
commonName=supplied
""")
    for name, start, end in (("expired", "20200101000000Z", "20200102000000Z"),
                             ("future", "20990101000000Z", "20990102000000Z")):
        run("ca", "-batch", "-config", "signing.cnf", "-in", "flight.csr",
            "-out", f"{name}.pem", "-startdate", start, "-enddate", end,
            "-extfile", "flight.ext")
        (directory / f"{name}.key").write_bytes((directory / "flight.key").read_bytes())
        os.chmod(directory / f"{name}.key", 0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory.resolve())
