#!/usr/bin/env python3
"""Actual QEMU qtest MMIO/timer/latch/overflow/reset/scenario checks."""
import argparse
from collections import Counter
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

BASE = 0x090c0000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='orbit-qtest-') as temporary, \
            (args.output / 'qemu.log').open('wb') as log:
        endpoint = Path(temporary) / 'qtest.sock'
        child = subprocess.Popen([str(args.qemu.resolve()), '-M', 'virt-9.2',
            '-accel', 'qtest', '-cpu', 'cortex-a57', '-display', 'none', '-nodefaults',
            '-qtest', f'unix:{endpoint},server=on,wait=off'], stdout=log, stderr=log)
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(5)
        try:
            deadline = time.monotonic() + 10
            while not endpoint.exists():
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('QEMU qtest startup failed; inspect qemu.log')
                time.sleep(0.01)
            client.connect(str(endpoint))
            stream = client.makefile('rwb', buffering=0)

            def command(text):
                stream.write((text + '\n').encode())
                while True:
                    line = stream.readline().decode().strip()
                    if not line:
                        raise RuntimeError('QEMU qtest connection closed')
                    if line.startswith('IRQ '):
                        continue
                    if not line.startswith('OK'):
                        raise RuntimeError(f'{text}: {line}')
                    return line.split()[1:]

            def read(offset):
                return int(command(f'readl {BASE + offset:#x}')[0], 0)

            def write(offset, value):
                command(f'writel {BASE + offset:#x} {value:#x}')

            def tick(ns=100_000_000):
                command(f'clock_step {ns}')

            def irq_pending():
                return bool(int(command('readl 0x08000204')[0], 0) & (1 << 10))

            def sample():
                return [read(0x40 + i * 4) for i in range(16)]

            assert read(0) == 0x4f534e32 and read(4) == 2
            assert read(0xffc) == 0
            command(f'readb {BASE:#x}')  # Invalid widths must not mutate state.
            assert read(0) == 0x4f534e32
            assert read(0x24) == 64 and read(0x28) == 1
            write(0x14, 7)
            write(8, 3)
            categories = Counter()
            events = []
            sequence = 0
            last_ns = 0
            for _ in range(600):
                tick()
                while read(0x20):
                    words = sample()
                    assert words[0] == 2 and words[2] == 1 and words[3] == sequence
                    assert words[7] == 1 and words[6] == 0
                    ns = (words[5] << 32) | words[4]
                    assert ns >= last_ns and ns > 0
                    last_ns = ns
                    sequence += 1
                    categories[words[1]] += 1
                    if words[1] == 6:
                        events.append(words[8])
                    write(0x1c, 1)
            assert categories == {1:120, 2:60, 3:300, 4:60, 5:60, 6:6}, categories
            assert sorted(events) == [1,2,3,4,5,6], events
            write(0x80, 1)
            assert read(0x90) == 606 and read(0x98) == 0 and read(0x84) == 0
            tick()
            head = sample()
            assert read(0x0c) & 4
            assert irq_pending(), 'GIC did not observe the asserted sensor SPI'
            write(8, 1)
            assert not irq_pending(), 'IRQ mask failed to deassert the SPI'
            write(8, 3)
            assert irq_pending(), 'unmask did not restore nonempty FIFO IRQ'
            for _ in range(70):
                tick()
            assert sample() == head, 'head latch changed without POP'
            assert read(0x20) == 56 and read(0x0c) & 2
            assert read(0x90) == 606, 'snapshot changed without SNAP'
            write(0x80, 1)
            assert read(0x98) >= 7
            for _ in range(1800):
                tick()
            assert read(0x20) == 64
            assert 0 < read(0xa0) <= 8 and read(0xac) > 0
            write(0x18, 1)
            words = sample()
            assert words[1] == 6 and words[2] == 2 and words[3] == 0 and words[8] == 7
            write(0x1c, 1)
            assert read(0x20) == 0 and not (read(0x0c) & 4)
            assert not irq_pending()
            write(0x10, 0)
            assert read(0x10) == 100
            write(0x10, 1)
            tick(1_000_000)
            assert read(0x20) == 1
            write(8, 0)
            count = read(0x20)
            tick()
            assert read(0x20) == count
            while read(0x20):
                write(0x1c, 1)
            write(0xd0, 3)
            fault = sample()
            assert fault[1] == 6 and fault[8] == 8 and fault[10] == 3
            write(0x1c, 1)
            write(8, 3)
            for _ in range(3):
                tick(1_000_000)
            fault_found = False
            while read(0x20):
                words = sample()
                fault_found |= words[1] == 3 and words[6] == 1
                write(0x1c, 1)
            assert fault_found
            write(0xd0, 0)
            write(0xd0, 6)
            assert read(0xd0) == 0
            (args.output / 'result.json').write_text(json.dumps({
                'status':'passed', 'routine_ticks':600, 'reports':606,
                'categories':dict(categories), 'event_codes':events,
                'checks':['MMIO identity','virtual timer','latched coherent head',
                    'bounded FIFO/overrun','atomic counter snapshot','reset generation/event',
                    'rate validation','IRQ level status','stop control','bounded sensor fault event/quality']}, indent=2) + '\n')
        finally:
            client.close()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()


if __name__ == '__main__':
    main()
