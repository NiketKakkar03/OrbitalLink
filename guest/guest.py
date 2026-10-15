#!/usr/bin/env python3
"""Pinned ARM64 Buildroot recipe; builds require a Linux host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
VERSIONS = json.loads((ROOT / 'versions.json').read_text())


def run(args, **kwargs):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(out, state=None, seed=1):
    state = state or out.parent / 'state.ext4'
    return [out / 'host/bin/qemu-system-aarch64', '-M', VERSIONS['qemu']['machine'],
            '-global', f'orbit-sensor-hub.seed={seed}',
            '-cpu', 'cortex-a57', '-accel', 'tcg', '-m', '512', '-smp', '2',
            '-nographic', '-no-reboot', '-kernel', out / 'images/Image',
            '-append', 'root=/dev/vda rw console=ttyAMA0',
            '-drive', f"file={out / 'images/rootfs.ext4'},if=none,format=raw,id=rootfs,snapshot=on",
            '-device', 'virtio-blk-pci,drive=rootfs,bus=pcie.0,addr=0x1',
            '-drive', f"file={state},if=none,format=raw,id=state,snapshot=off",
            '-device', 'virtio-blk-pci,drive=state,bus=pcie.0,addr=0x2',
            '-object', 'rng-random,id=rng0,filename=/dev/urandom',
            '-device', 'virtio-rng-device,rng=rng0,max-bytes=1024,period=1000',
            '-netdev', 'user,id=net0', '-device', 'virtio-net-device,netdev=net0']


def prepare_state(out, state=None):
    """Create once; never truncate a retained guest outbox."""
    state = state or out.parent / 'state.ext4'
    if state.exists():
        return
    temporary = state.with_suffix('.creating')
    with temporary.open('xb') as stream:
        stream.truncate(128 * 1024 * 1024)
    run([out / 'host/sbin/mkfs.ext4', '-F', '-L', 'orbit-state', temporary])
    temporary.rename(state)


def doctor():
    linux = platform.system() == 'Linux'
    required = ['git', 'make', 'gcc', 'g++', 'bash', 'patch', 'tar', 'gzip',
                'bzip2', 'xz', 'perl', 'cpio', 'unzip', 'rsync', 'wget', 'file', 'bc']
    missing = [name for name in required if shutil.which(name) is None]
    report = {'host': platform.system(), 'architecture': platform.machine(),
              'linux_build_host': linux, 'missing_tools': missing,
              'build_ready': linux and not missing}
    print(json.dumps(report, indent=2))
    return 0 if report['build_ready'] else 2


def fetch(work):
    src = work / 'buildroot'
    pin = VERSIONS['buildroot']['commit']
    if not src.exists():
        run(['git', 'init', src])
        run(['git', '-C', src, 'remote', 'add', 'origin', VERSIONS['buildroot']['repository']])
    # Never reset an existing source tree with local changes.
    dirty = subprocess.check_output(['git', '-C', src, 'status', '--porcelain'], text=True)
    if dirty:
        raise RuntimeError('Buildroot checkout contains changes; use a new --work directory')
    run(['git', '-C', src, 'fetch', '--depth=1', 'origin', pin])
    run(['git', '-C', src, 'checkout', '--detach', pin])
    actual = subprocess.check_output(['git', '-C', src, 'rev-parse', 'HEAD'], text=True).strip()
    if actual != pin:
        raise RuntimeError('Buildroot revision mismatch')
    return src


def validate_source(src):
    checks = [('board/qemu/patches/linux/linux.hash', VERSIONS['linux']['sha256']),
              ('package/qemu/qemu.hash', VERSIONS['qemu']['sha256']),
              ('package/qemu/qemu.mk', 'QEMU_VERSION = ' + VERSIONS['qemu']['version']),
              ('package/gcc/Config.in.host', '"' + VERSIONS['toolchain']['gcc'] + '"')]
    for name, expected in checks:
        if expected not in (src / name).read_text():
            raise RuntimeError(f'pinned source metadata changed: {name}')


def configure(work):
    src = fetch(work)
    validate_source(src)
    out = work / 'output'
    args = ['make', '-C', src, f'O={out}', f'BR2_EXTERNAL={ROOT}']
    # Kconfig alone also runs on macOS; this does not establish build readiness.
    run(args + ['HOSTCC=cc', 'HOSTCXX=c++', 'orbit_aarch64_defconfig'])
    return src, out, args


def build(work, jobs):
    if doctor():
        raise RuntimeError('Guest builds require Linux with the listed tools. See docs/guest.md.')
    src, out, args = configure(work)
    # Existing cached QEMU packages must notice local device/scenario edits.
    qemu_source = out / 'build' / ('host-qemu-' + VERSIONS['qemu']['version'])
    if qemu_source.exists():
        run([sys.executable, REPO / 'qemu/apply.py', qemu_source])
        run(args + [f'BR2_JLEVEL={jobs}', 'host-qemu-rebuild'])
    # Recopy local sources on repeated builds; cached Buildroot local packages do
    # not otherwise notice edits to flight/*.cpp or headers.
    if list((out / 'build').glob('orbit-flight-*')):
        run(args + ['orbit-flight-dirclean'])
    if list((out / 'build').glob('orbit-driver-*')):
        run(args + ['orbit-driver-dirclean'])
    # Buildroot controls package compilation separately from top-level make.
    run(args + [f'-j{jobs}', f'BR2_JLEVEL={jobs}'])
    run(args + ['legal-info'])
    manifest = dict(VERSIONS)
    manifest['recipe_sha256'] = {str(p.relative_to(ROOT)): digest(p)
        for p in sorted(ROOT.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    manifest['flight_sha256'] = {str(p.relative_to(REPO)): digest(p)
        for p in sorted((REPO / 'flight').glob('*')) if p.is_file()}
    manifest['artifacts_sha256'] = {name: digest(out / 'images' / name)
                                  for name in ['Image', 'rootfs.ext4']}
    manifest['configuration_sha256'] = digest(out / '.config')
    manifest['compiler'] = subprocess.check_output(
        [out / 'host/bin/aarch64-buildroot-linux-gnu-g++', '--version'], text=True)
    executable = out / 'target/usr/bin/orbit-flight'
    manifest['flight_elf_header'] = subprocess.check_output(
        [out / 'host/bin/aarch64-buildroot-linux-gnu-readelf', '-h', executable], text=True)
    if 'AArch64' not in manifest['flight_elf_header']:
        raise RuntimeError('flight executable is not AArch64')
    manifest['flight_executable_sha256'] = digest(executable)
    manifest['custom_qemu_sha256'] = {str(p.relative_to(REPO)): digest(p)
        for directory in [REPO / 'qemu', REPO / 'simulation']
        for p in sorted(directory.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    manifest['qemu_executable_sha256'] = digest(out / 'host/bin/qemu-system-aarch64')
    modules = sorted((out / 'target/lib/modules').rglob('orbit_test.ko'))
    if modules:
        manifest['driver_sha256'] = {str(p.relative_to(REPO)): digest(p)
            for p in sorted((REPO / 'driver').rglob('*')) if p.is_file() and
            (p.suffix in {'.c', '.h', '.sh'} or p.name == 'Makefile')}
        manifest['driver_module_sha256'] = {str(p.relative_to(out / 'target')): digest(p) for p in modules}
        manifest['driver_harness_sha256'] = digest(out / 'target/usr/bin/orbit-syscall-contract')
    manifest['host'] = {'system': platform.system(), 'machine': platform.machine()}
    manifest['status'] = 'image built; boot and mTLS telemetry gates pending'
    (out / 'images/orbit-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built image manifest:', out / 'images/orbit-manifest.json')


def boot(work, dry_run, seed=1):
    out = work / 'output'
    args = command(out, seed=seed)
    if dry_run:
        print(json.dumps(list(map(str, args)), indent=2))
        return
    prepare_state(out)
    for path in [out / 'host/bin/qemu-system-aarch64', out / 'images/Image', out / 'images/rootfs.ext4']:
        if not path.exists():
            raise RuntimeError(f'missing build artifact: {path}')
    manifest_path = out / 'images/orbit-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for name, expected in manifest['artifacts_sha256'].items():
        if digest(out / 'images' / name) != expected:
            raise RuntimeError(f'image identity mismatch: {name}')
    os.execv(str(args[0]), list(map(str, args)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=REPO / 'build/guest')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('doctor')
    sub.add_parser('fetch')
    sub.add_parser('configure')
    p = sub.add_parser('build'); p.add_argument('--jobs', type=int, default=2)
    p = sub.add_parser('run'); p.add_argument('--dry-run', action='store_true'); p.add_argument('--seed', type=int, default=1)
    args = parser.parse_args()
    work = args.work.expanduser().resolve()
    if any(ch.isspace() for ch in str(work) + str(ROOT)):
        parser.error('Buildroot paths must not contain whitespace')
    if args.action == 'doctor':
        return doctor()
    if args.action == 'fetch':
        validate_source(fetch(work))
    elif args.action == 'configure':
        configure(work)
    elif args.action == 'build':
        if args.jobs < 1:
            parser.error('--jobs must be positive')
        build(work, args.jobs)
    elif args.action == 'run':
        if not 0 <= args.seed <= 0xffffffff:
            parser.error('--seed must fit uint32')
        boot(work, args.dry_run, args.seed)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f'guest: {error}', file=sys.stderr)
        sys.exit(1)
