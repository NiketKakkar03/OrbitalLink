"""Local recipe and serial assertion checks; these do not emulate guest boot."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guest import VERSIONS, command, validate_source, prepare_state
from smoke import assert_summary, Console


class RecipeTests(unittest.TestCase):
    def test_existing_persistent_state_is_never_reformatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            state = work / 'state.ext4'
            state.write_bytes(b'retained outbox')
            prepare_state(work / 'output')
            self.assertEqual(state.read_bytes(), b'retained outbox')

    def test_loss_or_missing_ack_cannot_pass_smoke(self):
        valid = {'generated': 100, 'acknowledged': 100, 'dropped_oldest': 0, 'discarded_shutdown': 0}
        assert_summary(valid, 100)
        for field in valid:
            bad = dict(valid); bad[field] = valid[field] + 1
            with self.assertRaises(RuntimeError):
                assert_summary(bad, 100)
        with self.assertRaises(RuntimeError):
            assert_summary({'generated': 100}, 100)

    def test_launcher_preserves_image_and_pins_machine(self):
        args = list(map(str, command(Path('/tmp/orbit-image'))))
        self.assertIn('virt-9.2', args)
        self.assertNotIn('-snapshot', args)
        self.assertTrue(any('id=rootfs,snapshot=on' in value for value in args))
        self.assertTrue(any('id=state,snapshot=off' in value for value in args))
        self.assertIn('virtio-blk-pci,drive=rootfs,bus=pcie.0,addr=0x1', args)
        self.assertIn('virtio-blk-pci,drive=state,bus=pcie.0,addr=0x2', args)
        self.assertIn('tcg', args)
        self.assertIn('user,id=net0', args)
        self.assertFalse(any('hostfwd' in value for value in args))

    def test_upstream_metadata_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            files = {'board/qemu/patches/linux/linux.hash': VERSIONS['linux']['sha256'],
                     'package/qemu/qemu.hash': VERSIONS['qemu']['sha256'],
                     'package/qemu/qemu.mk': 'QEMU_VERSION = 9.2.0',
                     'package/gcc/Config.in.host': '"13.4.0"'}
            for name, value in files.items():
                p = src / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
            validate_source(src)
            (src / 'package/qemu/qemu.hash').write_text('wrong')
            with self.assertRaises(RuntimeError):
                validate_source(src)

    def test_serial_timeout_is_bounded(self):
        import os
        read, write = os.pipe()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        try:
            with tempfile.TemporaryFile() as log:
                console = Console(read, child, log)
                os.write(write, b'booted\n')
                console.wait(rb'booted\n', 1)
                with self.assertRaises(RuntimeError):
                    console.wait(rb'never-arrives', 0.01)
        finally:
            child.terminate(); child.wait()
            os.close(read); os.close(write)

    def test_resolved_buildroot_config_if_present(self):
        config = ROOT.parent / 'build/guest/output/.config'
        if not config.exists() or '-gd030e36b' not in config.read_text():
            self.skipTest('run pinned Buildroot defconfig first')
        text = config.read_text()
        for symbol in ['BR2_aarch64=y', 'BR2_TOOLCHAIN_BUILDROOT_CXX=y',
                       'BR2_TOOLCHAIN_BUILDROOT_GLIBC=y', 'BR2_PACKAGE_LIBOPENSSL_BIN=y',
                       'BR2_PACKAGE_ORBIT_FLIGHT=y', 'BR2_PACKAGE_HOST_QEMU_SYSTEM_MODE=y',
                       'BR2_GCC_VERSION="13.4.0"', 'BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE="6.12.27"']:
            self.assertIn(symbol + '\n', text)


if __name__ == '__main__':
    unittest.main()
