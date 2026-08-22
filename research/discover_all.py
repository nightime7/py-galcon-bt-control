#!/usr/bin/env python3
"""Discover all characteristics and their values."""

import asyncio
from bleak import BleakClient, BleakScanner

async def main():
    print("[*] Scanning for device...")
    scanner = BleakScanner()
    devices = await scanner.discover(timeout=30)
    
    device = None
    for d in devices:
        if d.name and "GL6100" in d.name:
            device = d
            break
    
    if not device:
        print("[-] Device not found")
        print(f"[*] Found {len(devices)} devices:")
        for d in devices:
            print(f"    {d.name} ({d.address})")
        return
    
    print(f"[+] Found {device.name}")
    print(f"[*] Services in advertisement:")
    if device.metadata:
        if 'uuids' in device.metadata:
            for uuid in device.metadata['uuids']:
                print(f"    - {uuid}")
        if 'manufacturer_data' in device.metadata:
            mfg_data = device.metadata['manufacturer_data']
            print(f"[*] Manufacturer Data: {mfg_data}")
    
    print(f"\n[*] Connecting to {device.address}...")
    async with BleakClient(device) as client:
        print("[+] Connected")
        
        # Get all services
        services = await client.get_services()
        print(f"\n[+] Found {len(services)} services")
        
        for service in services:
            print(f"\n  Service: {service.uuid}")
            for char in service.characteristics:
                try:
                    # Try to read the characteristic
                    if "read" in char.properties:
                        value = await client.read_gatt_char(char)
                        hex_val = " ".join(f"{b:02x}" for b in value)
                        print(f"    [{char.uuid}]")
                        print(f"      Value: {hex_val}")
                        # Check if it looks like battery (0-100 or 0-200)
                        if len(value) == 1 and 0 <= value[0] <= 200:
                            print(f"      ^^^ POSSIBLY BATTERY: {value[0]}%")
                    else:
                        print(f"    [{char.uuid}]")
                        print(f"      (not readable)")
                except Exception as e:
                    print(f"    [{char.uuid}] - Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
