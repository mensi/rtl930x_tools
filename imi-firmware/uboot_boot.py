# /// script
# dependencies = [
#   "pyserial",
# ]
# ///

import serial
import time
import sys
import os
import struct
import argparse
import shutil

# Constants
BAUD_UPLOAD = 115200
TFTP_LOAD_ADDR = '0x84f00000'

# YModem Constants
SOH = b'\x01'
STX = b'\x02'
EOT = b'\x04'
ACK = b'\x06'
NAK = b'\x15'
CAN = b'\x18'
CRC = b'C'

# RTL9300 GPIO Registers
GPIO_DIR = 0xb8003308
GPIO_DAT = 0xb800330c

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

def wait_for_byte(ser, targets, timeout=5):
    start_time = time.time()
    while time.time() - start_time < timeout:
        b = ser.read(1)
        if b in targets:
            return b
        if b:
            try:
                sys.stdout.write(b.decode('ascii', errors='ignore'))
                sys.stdout.flush()
            except:
                pass
    return b''

def send_break(ser):
    print("[-] Power on the switch now...")
    if not wait_for_string(ser, "No ethernet found.", timeout=30):
        print("\n[!] Failed to see 'No ethernet found.' message.")
        return False
    
    print("\n[-] Marker seen. Spamming interrupt sequence (Ctrl+C, z, h)...")
    start_interrupt = time.time()
    while time.time() - start_interrupt < 5:
        ser.write(b'\x03zh')
        time.sleep(0.05)
        if ser.in_waiting:
            out = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            sys.stdout.write(out)
            sys.stdout.flush()
            if '#' in out or 'RTKX' in out or 'RTL9300' in out:
                print("\n[+] Successfully entered U-Boot prompt!")
                return True
    
    ser.write(b'\n')
    if wait_for_string(ser, "#", timeout=2) or wait_for_string(ser, "RTKX", timeout=2):
        print("\n[+] Successfully entered U-Boot prompt!")
        return True
    return False

def change_baudrate(ser):
    print(f"\n[-] Requesting baudrate change to {BAUD_UPLOAD}...")
    ser.write(f'setenv baudrate {BAUD_UPLOAD}\n'.encode('ascii'))
    
    # Wait for Switch baudrate message
    buffer = ""
    start_time = time.time()
    while time.time() - start_time < 2:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            sys.stdout.write(chunk)
            sys.stdout.flush()
            buffer += chunk
            if "Switch baudrate" in buffer or "press ENTER" in buffer:
                break
        time.sleep(0.01)

    time.sleep(0.2)
    ser.baudrate = BAUD_UPLOAD
    print(f"[+] Terminal baudrate switched to {BAUD_UPLOAD}")
    
    time.sleep(0.5)
    ser.write(b'\r\n\n')
    if wait_for_string(ser, "#", timeout=5) or wait_for_string(ser, "RTL9300", timeout=5):
        print("\n[+] U-Boot prompt received at 115200 baud.")
        return True
    print("\n[!] Baudrate switch failed or no prompt received.")
    return False

def crc16(data):
    crc = 0
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = (crc << 1)
            crc &= 0xFFFF
    return crc

def send_ymodem(ser, filepath):
    filesize = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    
    print(f"\n[-] Starting Ymodem upload of {filename} ({filesize} bytes)...")

    if wait_for_byte(ser, [CRC], timeout=30) != CRC:
        print("\n[!] Timeout waiting for initial 'C'")
        return False

    header = filename.encode('ascii') + b'\x00' + str(filesize).encode('ascii') + b'\x00'
    header = header.ljust(128, b'\x00')
    packet = SOH + b'\x00\xff' + header + struct.pack('>H', crc16(header))
    ser.write(packet)
    
    if wait_for_byte(ser, [ACK], timeout=5) != ACK: return False
    if wait_for_byte(ser, [CRC], timeout=5) != CRC: return False

    with open(filepath, 'rb') as f:
        block_num = 1
        sent_bytes = 0
        while True:
            chunk = f.read(1024)
            if not chunk: break
            
            sent_bytes += len(chunk)
            chunk = chunk.ljust(1024, b'\x1a')
            
            packet = STX + struct.pack('BB', block_num & 0xFF, (255 - (block_num & 0xFF))) + chunk + struct.pack('>H', crc16(chunk))
            ser.write(packet)
            
            if wait_for_byte(ser, [ACK], timeout=10) != ACK:
                print(f"\n[!] Failed at block {block_num}")
                return False
            
            block_num += 1
            if block_num % 20 == 0:
                sys.stdout.write(f"\rProgress: {sent_bytes}/{filesize} bytes ({(sent_bytes/filesize)*100:.1f}%)")
                sys.stdout.flush()
            
            # Tiny delay for stability
            time.sleep(0.001)

    ser.write(EOT)
    wait_for_byte(ser, [NAK], timeout=2)
    ser.write(EOT)
    wait_for_byte(ser, [ACK], timeout=2)
    wait_for_byte(ser, [CRC], timeout=2)

    null_block = b'\x00' * 128
    packet = SOH + b'\x00\xff' + null_block + struct.pack('>H', crc16(null_block))
    ser.write(packet)
    wait_for_byte(ser, [ACK], timeout=2)
    
    print("\n[+] Upload complete!")
    return True

def exec_cmd(ser, cmd, timeout=2):
    ser.write(f'{cmd}\n'.encode('ascii'))
    return wait_for_string(ser, "#", timeout=timeout)

def read_reg(ser, addr):
    # Clear input buffer
    ser.reset_input_buffer()
    ser.write(f'md.l {addr:#x} 1\n'.encode('ascii'))
    
    prefix = f'{addr & 0xffffffff:08x}: '
    buffer = ""
    start_time = time.time()
    while time.time() - start_time < 2:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode('ascii', errors='ignore')
            sys.stdout.write(chunk)
            sys.stdout.flush()
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
    return 0

def disable_mcu_watchdog(ser):
    print("[-] Attempting to disable MCU watchdog via manual bit-banging...")
    
    # Get current state to avoid clobbering other GPIOs
    initial_dir = read_reg(ser, GPIO_DIR)
    initial_dat = read_reg(ser, GPIO_DAT)
    print(f"\n[+] Initial GPIO DIR: {initial_dir:#010x}, DAT: {initial_dat:#010x}")
    
    # Maintain local state for Port A (GPIO 0-7)
    state = {'dir': initial_dir, 'dat': initial_dat}
    
    def update_dir():
        exec_cmd(ser, f'mw.l {GPIO_DIR:#x} {state["dir"]:#x} 1')
        time.sleep(0.01)

    def update_dat():
        exec_cmd(ser, f'mw.l {GPIO_DAT:#x} {state["dat"]:#x} 1')
        time.sleep(0.01)

    def i2c_set_scl(val):
        if val: state['dat'] |= (1 << 3)
        else: state['dat'] &= ~(1 << 3)
        update_dat()
        time.sleep(0.01)

    def i2c_set_sda(val):
        if val: state['dat'] |= (1 << 4)
        else: state['dat'] &= ~(1 << 4)
        update_dat()
        time.sleep(0.01)

    def i2c_set_sda_dir(is_output):
        if is_output: state['dir'] |= (1 << 4)
        else: state['dir'] &= ~(1 << 4)
        update_dir()
        time.sleep(0.01)

    # Initial state: SCL, SDA high and output
    state['dir'] |= (1 << 3) | (1 << 4)
    state['dat'] |= (1 << 3) | (1 << 4)
    update_dir()
    update_dat()

    def i2c_start():
        i2c_set_sda(1)
        i2c_set_scl(1)
        i2c_set_sda(0)
        i2c_set_scl(0)

    def i2c_stop():
        i2c_set_sda(0)
        i2c_set_scl(1)
        i2c_set_sda(1)

    def i2c_write_byte(byte):
        for i in range(8):
            i2c_set_sda((byte >> (7 - i)) & 1)
            i2c_set_scl(1)
            i2c_set_scl(0)
        # ACK bit: switch SDA to input (tristate)
        i2c_set_sda_dir(False)
        i2c_set_scl(1)
        # Give slave time to pull SDA low, then clock it
        i2c_set_scl(0)
        i2c_set_sda_dir(True)

    def i2c_read_byte():
        byte = 0
        i2c_set_sda_dir(False)
        for i in range(8):
            i2c_set_scl(1)
            # Read bit
            val = read_reg(ser, GPIO_DAT)
            bit = (val >> 4) & 1
            byte = (byte << 1) | bit
            i2c_set_scl(0)
        return byte

    def i2c_send_ack(ack):
        i2c_set_sda_dir(True)
        i2c_set_sda(0 if ack else 1)
        i2c_set_scl(1)
        i2c_set_scl(0)
        i2c_set_sda(1)

    def i2c_read_reg(addr, reg):
        i2c_start()
        i2c_write_byte(addr) # Write mode to set address
        i2c_write_byte(reg)
        i2c_start() # Repeated start
        i2c_write_byte(addr | 1) # Read mode
        val = i2c_read_byte()
        i2c_send_ack(False) # NACK
        i2c_stop()
        return val

    def i2c_write_reg(addr, reg, val):
        i2c_start()
        i2c_write_byte(addr)
        i2c_write_byte(reg)
        i2c_write_byte(val)
        i2c_stop()

    # Check existence
    found_addr = None
    # Try 0xde:0xfd == 0x91
    print("[-] Checking for MCU watchdog at 0xde...")
    for _ in range(3):
        if i2c_read_reg(0xde, 0xfd) == 0x91:
            print("\n[+] Found MCU watchdog at 0xde")
            found_addr = 0xde
            break
        time.sleep(0.2)
    
    if not found_addr:
        print("[-] Checking for MCU watchdog at 0xdc...")
        for _ in range(3):
            if i2c_read_reg(0xdc, 0x66) == 0x91:
                print("\n[+] Found MCU watchdog at 0xdc")
                found_addr = 0xdc
                break
            time.sleep(0.2)

    if found_addr:
        print(f"[-] Disabling MCU watchdog at {found_addr:#x}...")
        i2c_write_reg(found_addr, 0x09, 0x4f)
        i2c_write_reg(found_addr, 0x0a, 0x3f)
        print("[+] MCU watchdog disabled.")
    else:
        print("[!] MCU watchdog not found, skipping disable.")
        
    print("[+] MCU watchdog sequence complete.")
    ser.write(b'\n')
    wait_for_string(ser, "#", timeout=1)
    return True


def boot_via_tftp(ser, image_path, tftp_server, tftp_image, usb_eth_mac):
    tftp_dest = f'/srv/tftp/{tftp_image}'
    print(f"[-] Copying {image_path} → {tftp_dest}...")
    shutil.copy2(image_path, tftp_dest)
    print("[+] Image copied.")

    if usb_eth_mac:
        print(f"[-] Setting up USB Ethernet MAC address: {usb_eth_mac}...")
        exec_cmd(ser, f'env set ethaddr {usb_eth_mac}')
        exec_cmd(ser, f'env set usbethaddr {usb_eth_mac}')
    else:
        print("[!] Warning: USB_ETH_MAC not provided, skipping MAC address setup.")

    print("[-] Starting USB subsystem...")
    if not exec_cmd(ser, 'usb start', timeout=30):
        print("[!] USB start timed out.")
        return False

    print(f"[-] Starting TFTP download from {tftp_server}:{tftp_image} to {TFTP_LOAD_ADDR}...")
    if not exec_cmd(ser, f'tftpboot {TFTP_LOAD_ADDR} {tftp_server}:{tftp_image}', timeout=120):
        print("[!] tftpboot timed out.")
        return False

    print(f"\n[+] Image successfully loaded via TFTP.")
    print(f"bootm {TFTP_LOAD_ADDR}")
    return True


def boot_via_ymodem(ser, image_path):
    print("[-] Sending 'loady 0x84f00000' command...")
    ser.write(b'loady 0x84f00000\n')
    if send_ymodem(ser, image_path):
        print("\n[+] Image successfully loaded.")
        print("bootm 0x84f00000")
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description='Boot Horaco switch via U-Boot')
    parser.add_argument('image_path', help='Path to the kernel image')
    parser.add_argument('--port', default='/dev/ttyUSB0', help='Serial port (default: /dev/ttyUSB0)')
    parser.add_argument('--baud', type=int, default=38400, help='Initial baudrate (default: 38400)')
    parser.add_argument('--tftp-server', default='192.168.1.111', help='TFTP server IP (default: 192.168.1.111)')
    parser.add_argument('--tftp-image', default='openwrt.img', help='TFTP image filename (default: openwrt.img)')
    parser.add_argument('--usb-eth-mac', help='USB Ethernet MAC address')
    parser.add_argument('--ymodem', action='store_true',
                        help='Use YModem serial transfer instead of USB TFTP (default: TFTP)')
    parser.add_argument('--disable-watchdog', action='store_true',
                        help='Disable MCU watchdog via I2C bit-bang in U-Boot (default: let kernel driver handle it)')
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as e:
        print(f"[!] Error opening port: {e}")
        return

    if send_break(ser):
        # Clear any initial garbage
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.write(b'\n')
        wait_for_string(ser, "#", timeout=2)

        # Disable hardware watchdog timer via mw command
        print("[-] Disabling internal hardware watchdog...")
        ser.write(b'mw.l 0xb8003268 0 1\n')
        time.sleep(0.1)
        ser.write(b'mw.l 0xb8003260 0 1\n')
        time.sleep(0.1)

        # Disable MCU watchdog
        if args.ymodem:
            print("[!] Warning: Disabling MCU watchdog implicitly for YModem upload to prevent interruption during the long transfer.")
            disable_mcu_watchdog(ser)
        elif args.disable_watchdog:
            disable_mcu_watchdog(ser)
        else:
            print("[-] Skipping MCU watchdog disable (kernel driver will handle it).")

        # Disable autoboot for the session
        ser.write(b'setenv bootdelay -1\n')
        time.sleep(0.1)
        ser.write(b'setenv bootcmd\n')
        time.sleep(0.1)

        if change_baudrate(ser):
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.write(b'\n')
            if not wait_for_string(ser, "#", timeout=5) and not wait_for_string(ser, "RTL9300", timeout=5):
                print("[!] Failed to get prompt after baudrate change.")
                ser.write(b'\n')
                wait_for_string(ser, "#", timeout=2)

            if args.ymodem:
                boot_via_ymodem(ser, args.image_path)
            else:
                boot_via_tftp(ser, args.image_path, args.tftp_server, args.tftp_image, args.usb_eth_mac)

    ser.close()

if __name__ == '__main__':
    main()

