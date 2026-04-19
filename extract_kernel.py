#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
import lzma
import time
import tempfile

# U-Boot style header definitions
SUPPORTED_MAGICS = [0x93000000, 0x83800000]
IH_NMLEN = 32

OS_MAP = {5: "Linux"}
ARCH_MAP = {5: "MIPS"}
TYPE_MAP = {2: "Kernel", 5: "Rootfs", 6: "Multi"}
COMP_MAP = {0: "None", 1: "gzip", 2: "bzip2", 3: "lzma", 4: "lzo"}

def pretty_print_header(h):
    print("=" * 40)
    print("      REALTEK SDK IMAGE HEADER")
    print("=" * 40)
    print(f"Magic:         0x{h['ih_magic']:08X}")
    print(f"Header CRC:    0x{h['ih_hcrc']:08X}")
    print(f"Timestamp:     {time.ctime(h['ih_time'])} (0x{h['ih_time']:08X})")
    print(f"Data Size:     {h['ih_size']} bytes (0x{h['ih_size']:X})")
    print(f"Load Address:  0x{h['ih_load']:08X}")
    print(f"Entry Point:   0x{h['ih_ep']:08X}")
    print(f"Data CRC:      0x{h['ih_dcrc']:08X}")
    print(f"OS:            {OS_MAP.get(h['ih_os'], h['ih_os'])}")
    print(f"Arch:          {ARCH_MAP.get(h['ih_arch'], h['ih_arch'])}")
    print(f"Type:          {TYPE_MAP.get(h['ih_type'], h['ih_type'])}")
    print(f"Compression:   {COMP_MAP.get(h['ih_comp'], h['ih_comp'])}")
    print(f"Name:          {h['ih_name']}")
    print("=" * 40)

def parse_header(data):
    if len(data) < 64:
        return None
    
    fields = struct.unpack('>IIIIIII BBBB 32s', data[:64])
    h = {
        'ih_magic': fields[0],
        'ih_hcrc':  fields[1],
        'ih_time':  fields[2],
        'ih_size':  fields[3],
        'ih_load':  fields[4],
        'ih_ep':    fields[5],
        'ih_dcrc':  fields[6],
        'ih_os':    fields[7],
        'ih_arch':  fields[8],
        'ih_type':  fields[9],
        'ih_comp':  fields[10],
        'ih_name':  fields[11].split(b'\0')[0].decode('ascii', 'ignore')
    }
    return h

def validate_header(data, pos):
    if pos + 64 > len(data):
        return None
    
    magic = struct.unpack('>I', data[pos:pos+4])[0]
    if magic in SUPPORTED_MAGICS:
        # Check if name field (32 bytes at offset 0x20) is a valid ASCII string
        name_field = data[pos+0x20:pos+0x40]
        name_end = name_field.find(b'\0')
        name_to_check = name_field[:name_end] if name_end != -1 else name_field
        
        # Ensure it only contains printable ASCII characters
        if all(32 <= c <= 126 for c in name_to_check):
            return parse_header(data[pos:])
    return None

def extract_kernel(firmware_data, header, header_pos, output_path):
    print(f"[*] Extracting kernel to {output_path}...")
    payload_start = header_pos + 64
    payload_size = header['ih_size']
    payload = firmware_data[payload_start : payload_start + payload_size]
    
    if header['ih_comp'] == 3: # LZMA
        print("[*] Decompressing LZMA kernel...")
        try:
            # Try native lzma first
            uncompressed = lzma.decompress(payload)
            with open(output_path, 'wb') as f:
                f.write(uncompressed)
        except Exception as e:
            print(f"[-] Native LZMA decompression failed: {e}. Trying lzcat...")
            # Fallback to lzcat which is more robust with some LZMA streams
            with tempfile.NamedTemporaryFile(suffix=".lzma", delete=False) as tmp:
                tmp.write(payload)
                tmp_path = tmp.name
            try:
                with open(output_path, "wb") as f:
                    subprocess.run(['lzcat', tmp_path], stdout=f, stderr=subprocess.DEVNULL)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
    else:
        with open(output_path, 'wb') as f:
            f.write(payload)
    
    print(f"[+] Kernel extracted successfully.")

def extract_rootfs(firmware_data, kernel_path, output_dir):
    print(f"[*] Searching for rootfs to extract into {output_dir}...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        if os.path.exists(kernel_path):
            print("[*] Scanning kernel for embedded initramfs (CPIO)...")
            with open(kernel_path, 'rb') as f:
                kernel_data = f.read()
            
            gzip_magic = b'\x1f\x8b\x08'
            start = 0
            while True:
                pos = kernel_data.find(gzip_magic, start)
                if pos == -1: break
                
                # Try to decompress and check if it's CPIO
                try:
                    # We'll use gunzip via subprocess for simplicity
                    tmp_gz = os.path.join(tmpdir, "initramfs.gz")
                    with open(tmp_gz, "wb") as f_gz:
                        f_gz.write(kernel_data[pos:])
                    
                    subprocess.run(['gunzip', '-f', tmp_gz], stderr=subprocess.DEVNULL)
                    
                    uncompressed_cpio = os.path.join(tmpdir, "initramfs")
                    if os.path.exists(uncompressed_cpio):
                        with open(uncompressed_cpio, "rb") as f_cpio:
                            cpio_magic = f_cpio.read(6)
                            if cpio_magic in [b'070701', b'070702']:
                                print(f"[+] Found initramfs (CPIO) at kernel offset 0x{pos:X}")
                                os.makedirs(output_dir, exist_ok=True)
                                f_cpio.seek(0)
                                subprocess.run(['cpio', '-idm'], stdin=f_cpio, cwd=output_dir, stderr=subprocess.DEVNULL)
                                print(f"[+] Rootfs extracted via cpio.")
                                return
                        os.remove(uncompressed_cpio)
                except:
                    pass
                
                start = pos + 1

    print("[-] No valid rootfs found (SquashFS or Initramfs).")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 rtk_extractor.py <image.bin> [kernel_out] [rootfs_dir]")
        sys.exit(1)

    image_path = sys.argv[1]
    kernel_out = sys.argv[2] if len(sys.argv) > 2 else None
    rootfs_dir = sys.argv[3] if len(sys.argv) > 3 else None

    if not os.path.exists(image_path):
        print(f"[-] File not found: {image_path}")
        sys.exit(1)

    with open(image_path, 'rb') as f:
        data = f.read()

    # Scan for Realtek Header
    print(f"[*] Scanning {image_path} for Realtek SDK headers...")
    found_pos = -1
    header = None
    
    # Try 4KiB aligned positions first
    for pos in range(0, len(data), 4096):
        header = validate_header(data, pos)
        if header:
            found_pos = pos
            print(f"[+] Found header with magic 0x{header['ih_magic']:08X} at 4KiB aligned offset 0x{pos:X}")
            break

    # Fallback to byte-by-byte scan
    if found_pos == -1:
        print("[*] No header found at 4KiB aligned offsets, falling back to full scan...")
        for pos in range(len(data) - 64):
            header = validate_header(data, pos)
            if header:
                found_pos = pos
                print(f"[+] Found header with magic 0x{header['ih_magic']:08X} at offset 0x{pos:X}")
                break

    if found_pos == -1:
        print("[-] Could not find a valid Realtek SDK header.")
        sys.exit(1)

    header = parse_header(data[found_pos:])
    pretty_print_header(header)

    if kernel_out:
        extract_kernel(data, header, found_pos, kernel_out)
    
    if rootfs_dir:
        extract_rootfs(data, kernel_out, rootfs_dir)

if __name__ == "__main__":
    main()
