#!/usr/bin/env python3
"""Monitor device for periodic battery notifications."""

import asyncio
from bleak import BleakClient, BleakScanner
import time

CHAR_COMMAND = "20900101-bdee-493a-aa74-a8137c9d43f0"
CHAR_STATUS = "20900102-bdee-493a-aa74-a8137c9d43f0"
CHAR_VALVE = "20900103-bdee-493a-aa74-a8137c9d43f0"
CHAR_POLL = "20900106-bdee-493a-aa74-a8137c9d43f0"

def notification_handler(sender, data):
    """Handle incoming notifications."""
    timestamp = time.strftime("%H:%M:%S")
    hex_str = " ".join(f"{b:02x}" for b in data)
    print(f"[{timestamp}] NOTIFY {sender.uuid}: {hex_str}")

async def main():
    print("[*] Scanning for device...")
    scanner = BleakScanner()
    devices = await scanner.discover(timeout=30)
    
    device = None
    for d in devices:
        if "GL6100" in d.name:
            device = d
            break
    
    if not device:
        print("[-] Device not found")
        return
    
    print(f"[+] Found {device.name} at {device.address}")
    
    async with BleakClient(device) as client:
        print("[+] Connected")
        
        # Subscribe to both characteristics
        print("[*] Subscribing to notifications...")
        await client.start_notify(CHAR_COMMAND, notification_handler)
        await client.start_notify(CHAR_STATUS, notification_handler)
        
        # Send a harmless status poll. Do not write 01 02 to CHAR_COMMAND:
        # that can be interpreted as zone 1 duration = 2h00m.
        print("[*] Sending status poll...")
        await client.write_gatt_char(CHAR_POLL, bytes([0x02, 0x00]))
        
        # Monitor for 60 seconds
        print("[*] Monitoring for 60 seconds (idle, no commands)...")
        await asyncio.sleep(60)
        
        await client.stop_notify(CHAR_COMMAND)
        await client.stop_notify(CHAR_STATUS)
    
    print("[+] Done")

if __name__ == "__main__":
    asyncio.run(main())
