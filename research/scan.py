import asyncio
from bleak import BleakScanner

NAME_HINT = "gl6100"

def detection_callback(device, advertisement_data):
    if NAME_HINT in (device.name or "").lower():
        name = device.name or "Galcon"
        rssi = advertisement_data.rssi
        print(f"[FOUND] {name} ({device.address}) | RSSI: {rssi} dBm")

async def monitor_rssi():
    print("Monitoring live RSSI for any GL6100 controller...")
    print("Press Ctrl+C to stop.\n")
    scanner = BleakScanner(detection_callback)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()

try:
    asyncio.run(monitor_rssi())
except KeyboardInterrupt:
    print("\nStopped scanner.")
