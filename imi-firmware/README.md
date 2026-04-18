# imi-firmware

On devices with a serial console, U-Boot is usually protected with a custom key combindation: `Ctrl+C`, `z` , `h` have to be
pressed in sequence when the countdown appears.

The scripts acting over serial assume that this unlock sequence has to be used. If you're working with a device that
does not have this, you likely need to adapt the scripts slightly.

## `get_chip_type.py`

Connects over serial, unlocks U-Boot and then reads the chip identification register to determine the exact chip
revision used.

## `run_interactive.py`

Uses serial and TFTP to provide a primitive root shell on the vendor firmware via repeated patch.tar.gz execution
of arbitrary commands.

## `uboot_boot.py`

Connects over serial, unlocks U-Boot, optionally disables the MCU watchdog and then boots an image either via
YModem upload or TFTP.
