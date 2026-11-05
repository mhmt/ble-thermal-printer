"""BLE keşif aracı: yazıcıyı bul, GATT servislerini dök, JXIG + durum sorgula."""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

NOTIFY = "00002af0-0000-1000-8000-00805f9b34fb"
WRITE = "00002af1-0000-1000-8000-00805f9b34fb"


async def main():
    print("Taranıyor (8 sn)...")
    found = await BleakScanner.discover(timeout=8.0, return_adv=True)
    for addr, (dev, adv) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
        print(f"  {addr}  rssi={adv.rssi:4}  name={dev.name!r}  uuids={adv.service_uuids}")

    name = sys.argv[1] if len(sys.argv) > 1 else None
    target = None
    for addr, (dev, adv) in found.items():
        n = dev.name or ""
        uu = [u.lower() for u in adv.service_uuids]
        if (name and name.lower() in n.lower()) or (not name and (n.startswith("P1") or any("18f0" in u for u in uu))):
            target = dev
            break
    if not target:
        print("Yazıcı bulunamadı.")
        return
    print(f"\nBağlanılıyor: {target.name} {target.address}")
    async with BleakClient(target) as c:
        print("MTU:", c.mtu_size)
        for s in c.services:
            print(f"[S] {s.uuid} {s.description}")
            for ch in s.characteristics:
                print(f"   [C] {ch.uuid} {ch.properties} {ch.description}")
        buf = bytearray()
        c_has_notify = any(ch.uuid == NOTIFY for s in c.services for ch in s.characteristics)
        if not c_has_notify:
            print("Beklenen 2AF0/2AF1 karakteristikleri yok!")
            return
        await c.start_notify(NOTIFY, lambda _, d: buf.extend(d))

        await c.write_gatt_char(WRITE, b"\x1b##JXIG", response=False)
        await asyncio.sleep(1.5)
        info = bytes(buf)
        buf.clear()
        print(f"\nJXIG cevabı ({len(info)} bayt):\n{info.hex(' ')}\n{info!r}")
        if len(info) >= 23:
            print("  version   =", int.from_bytes(info[6:8], "little"))
            print("  width     =", int.from_bytes(info[8:10], "little"))
            print("  dpi_type  =", info[10])
            print("  cmd_mode  =", info[11], "(1=ESC, 2=TSPL)")
            print("  speed     =", info[14])
            print("  density   =", info[15])
            print("  paper_type=", info[17])
            print("  compress  =", info[22])

        await c.write_gatt_char(WRITE, b"\x1d\x61\x00", response=False)
        await asyncio.sleep(1.0)
        print(f"\nDurum cevabı: {bytes(buf).hex(' ')}")


asyncio.run(main())
