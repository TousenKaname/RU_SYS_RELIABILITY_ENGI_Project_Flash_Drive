"""Build the self-contained Windows package: dist/FlashRel-Rig/ and dist/FlashRel-Rig-v<ver>.zip.

The package carries its own Python (the official "embeddable" CPython for Windows, x64
and ARM64, which needs no installation), so the test PC needs nothing installed:

    FlashRel-Rig/
      Start FlashRel.bat        start the rig (asks for administrator rights)
      Stop FlashRel.bat         emergency stop if the dashboard cannot be reached
      README_先读我.txt         what to do, in Chinese and English
      app/flashrel/             the software (standard library only)
      config/campaign.toml      the design of run 2
      runtime/amd64/, arm64/    CPython embeddable distributions (python.org)
      data/                     everything the test records (send this folder back)
      exports/                  ZIP exports made from the dashboard
      docs/                     test plan and operator guide

    python tools/build_package.py [--python 3.14.10]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tools" / ".cache"
DIST = ROOT / "dist"
NAME = "FlashRel-Rig"


def version() -> str:
    for line in (ROOT / "src" / "flashrel" / "__init__.py").read_text().splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise SystemExit("no __version__")


def fetch(py: str, arch: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = f"python-{py}-embed-{arch}.zip"
    target = CACHE / name
    if not target.exists():
        url = f"https://www.python.org/ftp/python/{py}/{name}"
        print(f"downloading {url}")
        with urllib.request.urlopen(url, timeout=120) as resp:
            target.write_bytes(resp.read())
    return target


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_crlf(path: Path, text: str, *, bom: bool = False) -> None:
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def install_runtime(zip_path: Path, target: Path) -> str:
    target.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(target)
    pth = next(target.glob("python*._pth"))
    lines = [ln for ln in pth.read_text().splitlines() if ln.strip()]
    lines.insert(2, "..\\..\\app")  # the rig's code, relative to the runtime folder
    write_crlf(pth, "\n".join(lines) + "\n")
    for exe in ("python.exe", "pythonw.exe"):
        if not (target / exe).exists():
            raise SystemExit(f"{exe} missing in {zip_path.name}")
    return pth.name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.14.10")
    ap.add_argument("--no-arm64", action="store_true")
    args = ap.parse_args()
    ver = version()
    out = DIST / NAME
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest = [f"FlashRel Rig {ver}", f"built {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                f"Python {args.python} embeddable (python.org)", ""]
    for arch in ("amd64",) + (() if args.no_arm64 else ("arm64",)):
        z = fetch(args.python, arch)
        pth = install_runtime(z, out / "runtime" / arch)
        manifest.append(f"runtime/{arch}: {z.name} sha256 {sha256(z)} ({pth} extended with ..\\..\\app)")
    shutil.copytree(ROOT / "src" / "flashrel", out / "app" / "flashrel",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    (out / "config").mkdir()
    shutil.copy2(ROOT / "config" / "campaign.toml", out / "config" / "campaign.toml")
    for bat in (ROOT / "launcher").glob("*.bat"):
        write_crlf(out / bat.name, bat.read_text(encoding="utf-8"))
    readme = ROOT / "launcher" / "README_先读我.txt"
    write_crlf(out / readme.name, readme.read_text(encoding="utf-8"), bom=True)
    (out / "docs").mkdir()
    for doc in ("test_plan_run2.md", "operator_guide_zh.md"):
        if (ROOT / "docs" / doc).exists():
            write_crlf(out / "docs" / doc, (ROOT / "docs" / doc).read_text(encoding="utf-8"), bom=True)
    for folder, text in (("data", "Everything the test records is written here. After the test, send this folder\n"
                                  "(or the ZIP from exports\\) back for the analysis.\n"),
                         ("exports", "ZIP exports made with 'Export ZIP' on the dashboard.\n")):
        (out / folder).mkdir()
        write_crlf(out / folder / "README.txt", text)
    files = sorted(p for p in out.rglob("*") if p.is_file() and "runtime" not in p.parts)
    manifest += ["", "sha256 of the rig files:"] + [f"{sha256(p)}  {p.relative_to(out).as_posix()}" for p in files]
    write_crlf(out / "VERSION.txt", "\n".join(manifest) + "\n")
    zip_path = DIST / f"{NAME}-v{ver}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, f"{NAME}/{p.relative_to(out).as_posix()}")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"package: {out} ({size / 1e6:.1f} MB)")
    print(f"zip:     {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
