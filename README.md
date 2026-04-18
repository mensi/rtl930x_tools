# RTL930x Tools

This repository contains a collection of utility scripts for working with firmware and binaries associated with Realtek RTL930x-based network switches. 

## Scripts

### `extract_kernel.py`

Can be used to extract the kernel and root filesystem by looking for the custom U-Boot header magic `0x93000000` which binwalk does not seem
to reliably detect. Run this first to get the kernel image and rootfs.

### `kernel_partitions.py`

This script attempts to extract the partition layout from the kernel image. This is mostly useful if you do not already have the
exact partition layout from a serial console.

### `parse_hwp.py`

Attempts to extract the hardware profiles from the `rtcore.ko` kernel module. This can be useful for determining the
proper values for a device tree.
