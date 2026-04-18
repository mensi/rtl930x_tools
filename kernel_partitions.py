#!/usr/bin/env python3
"""
MTD Partition Table Extractor for Realtek RTL930x.
Identifies partition layouts embedded in vmlinux binaries.
"""

import argparse
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

# --- Configuration Constants ---
DEFAULT_FLASH_SIZE = 0x02000000  # 32 MiB
FLASH_SIZES_TO_SEARCH = [0x00800000, 0x01000000, 0x02000000, 0x04000000, 0x08000000]

KERNEL_BASE = 0x80000000
MTD_STRUCT_STRIDE = 32
MAX_PARTITIONS = 15

TARGET_PARTITION_NAMES = [
    "LOADER",
    "BDINFO",
    "SYSINFO",
    "JFFS2 CFG",
    "JFFS2 LOG",
    "RUNTIME",
    "RUNTIME2",
]

LIKELY_RUNTIME_OFFSETS = {
    0x02000000: [0x300000],  # 32 MiB flash, RUNTIME often at 3 MiB
    0x01000000: [0x300000, 0x200000],  # 16 MiB flash
}


def parse_size(size_str: str) -> int:
    """Parse a size string (e.g., '32M', '16K', '0x2000000') into bytes."""
    size_str = size_str.lower()
    if size_str.endswith("m"):
        return int(size_str[:-1]) * 1024 * 1024
    if size_str.endswith("k"):
        return int(size_str[:-1]) * 1024
    if size_str.startswith("0x"):
        return int(size_str, 16)
    return int(size_str)


def find_partition_names(data: bytes) -> Dict[str, int]:
    """Find the offsets of known partition names in the binary data."""
    name_addrs = {}
    for name in TARGET_PARTITION_NAMES:
        # Try finding with null terminator first for precision
        pos = data.find(name.encode() + b"\0")
        if pos == -1:
            pos = data.find(name.encode())
        if pos != -1:
            name_addrs[name] = pos
    return name_addrs


def extract_partitions(vmlinux_path: str, target_fs: int = DEFAULT_FLASH_SIZE) -> None:
    """
    Extract and display the MTD partition table from a vmlinux binary.
    
    Args:
        vmlinux_path: Path to the vmlinux binary file.
        target_fs: The total flash size to prioritize when selecting a layout table.
    """
    if not os.path.exists(vmlinux_path):
        print(f"[-] File not found: {vmlinux_path}", file=sys.stderr)
        return

    with open(vmlinux_path, "rb") as f:
        data = f.read()

    # 1. Identify partition names and their pointers
    name_addrs = find_partition_names(data)
    if "LOADER" not in name_addrs:
        print("[-] Could not find essential partition names (LOADER missing).", file=sys.stderr)
        return

    # 2. Find the mtd_partition struct array
    loader_ptr = struct.pack(">I", KERNEL_BASE + name_addrs["LOADER"])
    struct_pos = -1

    # Standard mtd_partition struct size is 32 bytes on this MIPS core
    for pos in range(0, len(data) - MTD_STRUCT_STRIDE * 5, 4):
        if data[pos : pos + 4] == loader_ptr:
            # Heuristic: Check if BDINFO follows in the next struct entry
            if "BDINFO" in name_addrs:
                bd_ptr = struct.pack(">I", KERNEL_BASE + name_addrs["BDINFO"])
                if data[pos + MTD_STRUCT_STRIDE : pos + MTD_STRUCT_STRIDE + 4] == bd_ptr:
                    struct_pos = pos
                    break

    if struct_pos == -1:
        print("[-] Could not find mtd_partition struct array.", file=sys.stderr)
        return

    # Resolve all names in the array
    found_names: List[str] = []
    runtime_idx = -1
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
            if all(32 <= ord(c) < 127 for c in name):
                if name == "RUNTIME":
                    runtime_idx = len(found_names)
                found_names.append(name)
            else:
                break
        else:
            break

    # 3. Find the layout tables
    layout_tables: List[Tuple[int, ...]] = []
    search_start = struct_pos
    search_end = min(len(data), struct_pos + 0x2000)

    # Ensure target_fs is in the search list
    search_fs = list(set(FLASH_SIZES_TO_SEARCH + [target_fs]))

    curr = search_start
    while True:
        # Search for a word matching one of the flash sizes
        found_fs = -1
        found_pos = -1
        for fs in search_fs:
            p = data.find(struct.pack(">I", fs), curr, search_end)
            if p != -1 and (found_pos == -1 or p < found_pos):
                found_pos = p
                found_fs = fs

        if found_pos == -1:
            break

        if found_pos % 4 == 0:
            # Found a potential layout table.
            # Verify Offset 1 is 0 and Offset 2 is consistent.
            if found_pos + 12 <= len(data):
                off1, sz1, off2 = struct.unpack(">III", data[found_pos + 4 : found_pos + 16])
                if off1 == 0 and off2 == sz1 and 0 < sz1 < found_fs:
                    # Validated table. Extract words based on found names count.
                    tlen = 1 + 2 * len(found_names)
                    if found_pos + tlen * 4 <= len(data):
                        fmt = ">" + "I" * tlen
                        words = struct.unpack(fmt, data[found_pos : found_pos + tlen * 4])
                        layout_tables.append(words)

        curr = found_pos + 4

    if not layout_tables:
        print("[-] Could not find any layout tables.", file=sys.stderr)
        return

    # 4. Pick the "correct" table
    best_table = None

    # Priority 1: Match target_fs
    for table in layout_tables:
        if table[0] == target_fs:
            best_table = table
            break

    # Priority 2: Match likely runtime offset for target_fs
    if not best_table and runtime_idx != -1 and target_fs in LIKELY_RUNTIME_OFFSETS:
        likely_offs = LIKELY_RUNTIME_OFFSETS[target_fs]
        for table in layout_tables:
            runtime_off = table[1 + 2 * runtime_idx]
            if runtime_off in likely_offs:
                best_table = table
                break

    # Priority 3: Match any likely runtime offset regardless of size
    if not best_table and runtime_idx != -1:
        all_likely = [off for offs in LIKELY_RUNTIME_OFFSETS.values() for off in offs]
        for table in layout_tables:
            runtime_off = table[1 + 2 * runtime_idx]
            if runtime_off in all_likely:
                best_table = table
                break

    if not best_table:
        best_table = layout_tables[0]

    # 5. Display results
    print(f"\nExtracted MTD Partition Table from {vmlinux_path} (Target size: {target_fs/1024/1024:.0f}MB):")
    print("-" * 50)
    print(f"{'Name':<15} {'Offset':<12} {'Size':<12}")
    print("-" * 50)

    for i in range(len(found_names)):
        offset = best_table[1 + 2 * i]
        size = best_table[2 + 2 * i]
        print(f"{found_names[i]:<15} 0x{offset:08X} 0x{size:08X}")
    print("-" * 50)


def main() -> None:
    """CLI Entrypoint."""
    parser = argparse.ArgumentParser(
        description="Extract MTD partition table from RTL930x vmlinux"
    )
    parser.add_argument("vmlinux", help="Path to vmlinux binary")
    parser.add_argument(
        "--size",
        help="Overall image size (e.g. 32M, 16M, 0x02000000). Default: 32M",
        default="32M",
    )

    args = parser.parse_args()

    try:
        flash_size = parse_size(args.size)
    except ValueError:
        print(f"[-] Invalid size format: {args.size}", file=sys.stderr)
        sys.exit(1)

    extract_partitions(args.vmlinux, flash_size)


if __name__ == "__main__":
    main()
