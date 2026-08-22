"""One-off manual test: verifies GalconSession reuses a cached device object
for reconnects instead of rescanning. Not part of the app; delete after use
or keep here for future regression checks."""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import galcon_gui as gg  # noqa: E402


def main():
    q = queue.Queue()
    session = gg.GalconSession(q)

    def drain():
        while True:
            try:
                kind, payload = q.get_nowait()
                print(f"    [{kind}] {payload if kind == 'log' else ''}")
            except queue.Empty:
                break

    print("connect #1 (fresh scan expected)")
    t0 = time.monotonic()
    session.submit(session.connect(scan_time=60.0, debug=False)).result()
    print(f"  elapsed: {time.monotonic() - t0:.2f}s, "
          f"last_device set: {session.last_device is not None}")
    drain()
    time.sleep(0.5)

    print("disconnect")
    session.submit(session.disconnect()).result()
    drain()
    time.sleep(0.5)

    print("connect #2 (should reuse last_device, no scan)")
    t0 = time.monotonic()
    session.submit(session.connect(scan_time=60.0, debug=False)).result()
    print(f"  elapsed: {time.monotonic() - t0:.2f}s")
    drain()

    session.submit(session.disconnect()).result()


if __name__ == "__main__":
    main()
