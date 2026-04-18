#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pyserial",
# ]
# ///
"""Read RTL9300 MODEL_NAME_INFO register via U-Boot and decode chip identity.

Connects to serial, interrupts U-Boot, reads the switchcore MODEL_NAME_INFO
register (0xbb000004), and decodes the chip ID, revision, and chip type
using the same logic as the Realtek SDK's _drv_swcore_cid9300_get().
"""

import serial
import sys
import time
import argparse

# Configuration
MODEL_NAME_INFO_ADDR = 0xbb000004

# Register bit fields (from swcore_rtl9300.h)
RTL_ID_OFFSET = 16
RTL_ID_MASK = 0xFFFF0000
MODEL_CHAR_1ST_OFFSET = 11
MODEL_CHAR_1ST_MASK = 0x1F << MODEL_CHAR_1ST_OFFSET
MODEL_CHAR_2ND_OFFSET = 6
MODEL_CHAR_2ND_MASK = 0x1F << MODEL_CHAR_2ND_OFFSET
MODEL_CHAR_3RD_OFFSET = 4
MODEL_CHAR_3RD_MASK = 0x3 << MODEL_CHAR_3RD_OFFSET
RTL_VID_OFFSET = 0
RTL_VID_MASK = 0xF

# Chip IDs (from include/hal/chipdef/chip.h)
CHIP_IDS_FORMAL = {
    0x93010000: "RTL9301",
    0x93014000: "RTL9301H",
    0x93020800: "RTL9302A",
    0x93021000: "RTL9302B",
    0x93021800: "RTL9302C",
    0x93022000: "RTL9302D",
    0x93022140: "RTL9302DE",
    0x93023001: "RTL9302F",
    0x93030000: "RTL9303",
}

# Test/ES chip IDs (model_char_3rd != 0 → CHIP_TYPE_1)
CHIP_IDS_TEST = {
    0x93010000: ("RTL9301_24G",     0x93016810),
    0x93014000: ("RTL9301H_4X2_5G", 0x93014010),
    0x93020800: ("RTL9302A_12X2_5G", 0x93020810),
    0x93021000: ("RTL9302B_8X2_5G", 0x93021010),
    0x93021800: ("RTL9302C_16X2_5G", 0x93021810),
    0x93022000: ("RTL9302D_24X2_5G", 0x93022010),
    0x93030000: ("RTL9303_8XG",     0x93036810),
}

# Some chip families have two register patterns mapping to the same chip
# (e.g. RTL9301 and RTL9301_24G both map to the RTL9301 family)
CHIP_IDS_ALT = {
    0x93016800: 0x93010000,  # RTL9301_24G mask → RTL9301 family
    0x93036800: 0x93030000,  # RTL9303_8XG mask → RTL9303 family
}

# Revision names
REV_NAMES = {0: "A", 1: "B", 2: "C", 3: "D"}
LATEST_REV_FORMAL = 1
LATEST_REV_ES = 3


def wait_for_string(ser, target, timeout=10):
    buffer = ""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            sys.stdout.write(chunk)
            sys.stdout.flush()
            buffer += chunk
            if target in buffer:
                return True
        time.sleep(0.01)
    return False


def send_break(ser):
    print("[-] Power on the switch now...")
    if not wait_for_string(ser, "No ethernet found.", timeout=30):
        print("\n[!] Failed to see 'No ethernet found.' message.")
        return False

    print("\n[-] Sending interrupt sequence...")
    start_interrupt = time.time()
    while time.time() - start_interrupt < 5:
        ser.write(b'\x03zh')
        time.sleep(0.05)
        if ser.in_waiting:
            out = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            sys.stdout.write(out)
            sys.stdout.flush()
            if '#' in out or 'RTKX' in out or 'RTL9300' in out:
                print("\n[+] Entered U-Boot prompt.")
                return True

    ser.write(b'\n')
    if wait_for_string(ser, "#", timeout=2):
        print("\n[+] Entered U-Boot prompt.")
        return True
    return False


def read_reg(ser, addr):
    ser.reset_input_buffer()
    ser.write(f'md.l {addr:#x} 1\n'.encode('ascii'))

    prefix = f'{addr & 0xffffffff:08x}: '
    buffer = ""
    start_time = time.time()
    while time.time() - start_time < 2:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            buffer += chunk
            if "#" in buffer:
                break
        time.sleep(0.01)

    for line in buffer.splitlines():
        if prefix in line:
            parts = line.split(prefix)[1].strip().split()
            if parts:
                try:
                    return int(parts[0], 16)
                except ValueError:
                    pass
    return None


def decode_chip_id(reg_val):
    """Decode MODEL_NAME_INFO register using SDK logic from chip_probe.c."""
    model_info = reg_val & 0xFFFFFFC0
    model_char_3rd = (reg_val & MODEL_CHAR_3RD_MASK) >> MODEL_CHAR_3RD_OFFSET
    chip_rev_id = (reg_val & RTL_VID_MASK) >> RTL_VID_OFFSET

    # Resolve alternate register patterns to canonical family
    family_key = CHIP_IDS_ALT.get(model_info, model_info)

    is_chip_type_1 = model_char_3rd != 0

    if is_chip_type_1 and family_key in CHIP_IDS_TEST:
        chip_name, chip_id_val = CHIP_IDS_TEST[family_key]
        if chip_rev_id > LATEST_REV_ES:
            chip_rev_id = LATEST_REV_ES
    elif family_key in CHIP_IDS_FORMAL:
        chip_name = CHIP_IDS_FORMAL[family_key]
        chip_id_val = family_key
        if chip_rev_id > LATEST_REV_FORMAL:
            chip_rev_id = LATEST_REV_FORMAL
        # RTL9302DE has no test variant
        if family_key == 0x93022140:
            is_chip_type_1 = False
    else:
        chip_name = "UNKNOWN"
        chip_id_val = model_info

    return {
        'raw': reg_val,
        'model_info': model_info,
        'model_char_3rd': model_char_3rd,
        'chip_id': chip_id_val,
        'chip_name': chip_name,
        'chip_rev_id': chip_rev_id,
        'chip_rev_name': REV_NAMES.get(chip_rev_id, f"?({chip_rev_id})"),
        'chip_type_1': is_chip_type_1,
    }


def main():
    parser = argparse.ArgumentParser(description='Read RTL9300 chip type via U-Boot serial console.')
    parser.add_argument('port', nargs='?', default='/dev/ttyUSB0', help='Serial port (default: /dev/ttyUSB0)')
    parser.add_argument('--baud', '-b', type=int, default=38400, help='Baud rate (default: 38400)')
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as e:
        print(f"[!] Error opening port: {e}")
        return 1

    if not send_break(ser):
        print("[!] Failed to enter U-Boot.")
        ser.close()
        return 1

    time.sleep(0.3)
    ser.reset_input_buffer()
    ser.write(b'\n')
    wait_for_string(ser, "#", timeout=2)

    print(f"\n[-] Reading MODEL_NAME_INFO at {MODEL_NAME_INFO_ADDR:#010x}...")
    reg_val = read_reg(ser, MODEL_NAME_INFO_ADDR)
    ser.close()

    if reg_val is None:
        print("[!] Failed to read register.")
        return 1

    info = decode_chip_id(reg_val)

    print(f"\n{'='*50}")
    print(f"RTL9300 MODEL_NAME_INFO Register")
    print(f"{'='*50}")
    print(f"  Raw value:       {info['raw']:#010x}")
    print(f"  model_info:      {info['model_info']:#010x}")
    print(f"  model_char_3rd:  {info['model_char_3rd']}")
    print(f"  chip_rev_id raw: {info['raw'] & RTL_VID_MASK}")
    print(f"{'='*50}")
    print(f"  Chip ID:         {info['chip_name']} ({info['chip_id']:#010x})")
    print(f"  Chip Revision:   {info['chip_rev_name']} ({info['chip_rev_id']})")
    print(f"  CHIP_TYPE_1:     {'YES' if info['chip_type_1'] else 'NO'}")
    print(f"{'='*50}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
