#!/usr/bin/env -S uv run --script
"""
MTD Partition Table Extractor for Realtek RTL930x.
Identifies partition layouts embedded in vmlinux binaries and applies them to firmware dumps.
"""

import argparse
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple, Any
from collections import Counter

# --- Configuration Constants ---
FLASH_SIZES_TO_SEARCH = [0x00800000, 0x01000000, 0x02000000, 0x04000000, 0x08000000]

KERNEL_BASE = 0x80000000
MTD_STRUCT_STRIDE = 32
MAX_PARTITIONS = 20

# Names used to locate the struct array
ANCHOR_PARTITION_NAMES = [
    "LOADER",
    "BDINFO",
    "SYSINFO",
]

# Master list of known partition names and their likely indices in layout tables
# Realtek SDK often uses fixed indices for these.
MASTER_PARTITION_LIST = [
    "LOADER",      # 0
    "BDINFO",      # 1
    "SYSINFO",     # 2
    "JFFS2 CFG",   # 3
    "JFFS2 LOG",   # 4
    "RUNTIME",     # 5
    "RUNTIME2",    # 6
    "OEMINFO",     # 7
]

def find_anchor_names(data: bytes) -> Dict[str, int]:
    """Find the offsets of anchor partition names in the binary data."""
    name_addrs = {}
    for name in ANCHOR_PARTITION_NAMES:
        # Try finding with null terminator first for precision
        pos = data.find(name.encode() + b"\0")
        if pos == -1:
            pos = data.find(name.encode())
        if pos != -1:
            name_addrs[name] = pos
    return name_addrs


def extract_partitions(vmlinux_path: str, firmware_path: str, split: bool = False, verbose: bool = False) -> None:
    """
    Extract and display the MTD partition table from a vmlinux binary.
    """
    if not os.path.exists(vmlinux_path):
        print(f"[-] vmlinux file not found: {vmlinux_path}", file=sys.stderr)
        return
    if not os.path.exists(firmware_path):
        print(f"[-] Firmware file not found: {firmware_path}", file=sys.stderr)
        return

    target_fs = os.path.getsize(firmware_path)
    if verbose:
        print(f"[*] Firmware size detected: {target_fs/1024/1024:.2f} MiB (0x{target_fs:08X})")

    with open(vmlinux_path, "rb") as f:
        data = f.read()

    # 1. Identify anchor names and their pointers
    name_addrs = find_anchor_names(data)
    if "LOADER" not in name_addrs:
        print("[-] Could not find essential partition names (LOADER missing).", file=sys.stderr)
        return

    # 2. Find the mtd_partition struct array
    loader_ptr = struct.pack(">I", KERNEL_BASE + name_addrs["LOADER"])
    struct_pos = -1

    for pos in range(0, len(data) - MTD_STRUCT_STRIDE * 5, 4):
        if data[pos : pos + 4] == loader_ptr:
            # Heuristic: Check if BDINFO or SYSINFO follows in the next struct entries
            is_match = False
            for offset in [1, 2]:
                next_ptr_pos = pos + offset * MTD_STRUCT_STRIDE
                if next_ptr_pos + 4 > len(data): continue
                next_ptr = struct.unpack(">I", data[next_ptr_pos : next_ptr_pos + 4])[0]
                for name, addr in name_addrs.items():
                    if name == "LOADER": continue
                    if next_ptr == KERNEL_BASE + addr:
                        is_match = True
                        break
                if is_match: break
            
            if is_match:
                struct_pos = pos
                break

    if struct_pos == -1:
        print("[-] Could not find mtd_partition struct array.", file=sys.stderr)
        return

    if verbose:
        print(f"[*] Found mtd_partition struct array at 0x{struct_pos:08X}")

    # Resolve all names in the array
    found_names: List[str] = []
    for i in range(MAX_PARTITIONS):
        ptr_pos = struct_pos + i * MTD_STRUCT_STRIDE
        if ptr_pos + 4 > len(data):
            break
        ptr = struct.unpack(">I", data[ptr_pos : ptr_pos + 4])[0]
        if ptr < KERNEL_BASE or ptr >= KERNEL_BASE + len(data):
            break

        offset = ptr - KERNEL_BASE
        end = data.find(b"\0", offset)
        if end != -1:
            name = data[offset:end].decode("ascii", "ignore")
            if all(32 <= ord(c) < 127 for c in name) and len(name) > 0:
                found_names.append(name)
            else:
                break
        else:
            break

    if verbose:
        print(f"[*] Identified {len(found_names)} partitions in struct array: {', '.join(found_names)}")

    # 3. Find the layout tables and determine stride
    # Add target_fs to search list in case it's non-standard
    search_fs = sorted(list(set(FLASH_SIZES_TO_SEARCH + [target_fs])))
    search_start = struct_pos
    search_end = min(len(data), struct_pos + 0x8000)
    
    fs_positions: List[Tuple[int, int]] = []
    for fs in search_fs:
        curr = search_start
        while True:
            p = data.find(struct.pack(">I", fs), curr, search_end)
            if p == -1: break
            if p % 4 == 0:
                fs_positions.append((p, fs))
            curr = p + 4
    
    fs_positions.sort()
    
    if not fs_positions:
        print("[-] Could not find any layout tables (no flash size markers found).", file=sys.stderr)
        return

    # Determine stride by looking for sequences of flash size markers
    detected_stride = -1
    for i in range(len(fs_positions) - 1):
        s = fs_positions[i+1][0] - fs_positions[i][0]
        if 8 < s < 256 and s % 8 == 4: # Stride must be 4 + N*8
            p = fs_positions[i][0]
            fs = fs_positions[i][1]
            off1 = struct.unpack(">I", data[p+4:p+8])[0]
            sz1 = struct.unpack(">I", data[p+8:p+12])[0]
            if off1 == 0 and 0 < sz1 < fs:
                detected_stride = s
                break
    
    if detected_stride == -1:
        p, fs = fs_positions[0]
        max_words = (search_end - p) // 4
        possible_len = 1
        for i in range(1, max_words - 1, 2):
            off = struct.unpack(">I", data[p + i*4 : p + i*4 + 4])[0]
            sz = struct.unpack(">I", data[p + i*4 + 4 : p + i*4 + 8])[0]
            if off < fs and sz < fs:
                possible_len += 2
            else:
                break
        detected_stride = possible_len * 4

    if verbose:
        print(f"[*] Detected layout table stride: {detected_stride} bytes ({detected_stride//4} words)")

    layout_tables: List[Dict[str, Any]] = []
    for p, fs in fs_positions:
        num_pairs = (detected_stride - 4) // 8
        fmt = ">" + "I" * (1 + 2 * num_pairs)
        if p + struct.calcsize(fmt) <= len(data):
            words = struct.unpack(fmt, data[p : p + struct.calcsize(fmt)])
            if words[1] == 0 and 0 < words[2] < fs:
                layout_tables.append({
                    'total_fs': fs,
                    'offset': p,
                    'pairs': [(words[1+2*i], words[2+2*i]) for i in range(num_pairs)]
                })

    if not layout_tables:
        print("[-] No valid layout tables found after stride detection.", file=sys.stderr)
        return

    # 4. Pick the "best" table
    best_table = None
    for table in layout_tables:
        if table['total_fs'] == target_fs:
            best_table = table
            break
    if not best_table:
        # Find closest flash size
        layout_tables.sort(key=lambda x: abs(x['total_fs'] - target_fs))
        best_table = layout_tables[0]
        if verbose:
            print(f"[*] Exact size marker 0x{target_fs:X} not found, using closest table (0x{best_table['total_fs']:X}).")

    # 5. Map names to pairs
    num_slots = len(best_table['pairs'])
    mapping: List[Optional[str]] = [None] * num_slots
    remaining_names = list(found_names)
    
    for name in list(remaining_names):
        if name in MASTER_PARTITION_LIST:
            idx = MASTER_PARTITION_LIST.index(name)
            if idx < num_slots:
                mapping[idx] = name
                remaining_names.remove(name)
    
    curr_slot = 0
    for name in remaining_names:
        while curr_slot < num_slots and mapping[curr_slot] is not None:
            curr_slot += 1
        if curr_slot < num_slots:
            mapping[curr_slot] = name
            curr_slot += 1
        else:
            if verbose:
                print(f"[!] No slot left for partition: {name}")

    # Prepare split data if requested
    split_data = None
    if split:
        with open(firmware_path, "rb") as f_s:
            split_data = f_s.read()
        if verbose:
            print(f"[*] Loaded {len(split_data)} bytes for splitting from {firmware_path}")

    # 6. Display results
    print(f"\nExtracted MTD Partition Table from {vmlinux_path}")
    print(f"Detected Flash Size: {target_fs/1024/1024:.2f} MiB")
    print("-" * 50)
    print(f"{'Name':<15} {'Offset':<12} {'Size':<12}")
    print("-" * 50)

    for i, (offset, size) in enumerate(best_table['pairs']):
        name = mapping[i]
        if name is None:
            if offset == 0 and size == 0:
                continue
            name = f"(unnamed_{i})"
            
        print(f"{name:<15} 0x{offset:08X} 0x{size:08X}")
        
        if split_data and size > 0:
            name_clean = name.replace(" ", "_").replace("/", "_")
            out_name = f"{name_clean}.bin"
            if offset + size <= len(split_data):
                with open(out_name, "wb") as f_out:
                    f_out.write(split_data[offset : offset + size])
                print(f"    [>] Written to {out_name}")
            else:
                print(f"    [!] Partition {name} (0x{offset:08X}+0x{size:08X}) outside firmware bounds (0x{len(split_data):08X})")

    print("-" * 50)


def main() -> None:
    """CLI Entrypoint."""
    parser = argparse.ArgumentParser(
        description="Extract MTD partition table from RTL930x vmlinux using a firmware image for size reference."
    )
    parser.add_argument("vmlinux", help="Path to vmlinux binary")
    parser.add_argument("firmware", help="Path to the full firmware image")
    parser.add_argument(
        "--split",
        action="store_true",
        help="Split partitions into separate .bin files from the provided firmware image",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()
    extract_partitions(args.vmlinux, args.firmware, args.split, args.verbose)


if __name__ == "__main__":
    main()
