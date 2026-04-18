#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pyserial",
# ]
# ///

import sys
import os
import io
import tarfile
import hashlib
import serial
import time
import readline
import signal
import argparse

# Default constants
DEFAULT_TFTP_PATH = "/srv/tftp/patch.tar.gz"
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD_RATE = 38400
DEFAULT_TFTP_SERVER_IP = "192.168.1.111"

class SwitchREPL:
    def __init__(self, tftp_path, serial_port, tftp_ip):
        self.ser = None
        self.tftp_path = tftp_path
        self.serial_port = serial_port
        self.tftp_ip = tftp_ip
        self.history_file = os.path.expanduser("~/.switch_history")
        if os.path.exists(self.history_file):
            try:
                readline.read_history_file(self.history_file)
            except Exception:
                pass
        readline.set_history_length(1000)

    def connect(self):
        try:
            print(f"Connecting to {self.serial_port} at {DEFAULT_BAUD_RATE} baud...")
            self.ser = serial.Serial(self.serial_port, DEFAULT_BAUD_RATE, timeout=0.1)
            # Initial wake up
            self.ser.write(b"\n")
            self.login()
        except Exception as e:
            print(f"Error opening serial port: {e}")
            sys.exit(1)

    def expect(self, targets, silent=True):
        collected = b""
        start_time = time.time()
        while time.time() - start_time < 30: # 30 second timeout
            data = self.ser.read(100)
            if data:
                collected += data
                if not silent:
                    sys.stdout.write(data.decode('ascii', errors='ignore'))
                    sys.stdout.flush()
                for t in targets:
                    if t.encode('ascii') in collected:
                        return t
            time.sleep(0.01)
        return None

    def login(self):
        prompt = self.expect(["Username:", "Switch>", "Switch#"])
        
        if prompt == "Username:":
            self.ser.write(b"admin\n")
            if self.expect(["Password:"]):
                self.ser.write(b"admin\n")
            prompt = self.expect(["Switch>", "Switch#"])
            
        if prompt == "Switch>":
            self.ser.write(b"en\n")
            prompt = self.expect(["Switch#"])
            
        if not prompt or prompt != "Switch#":
            print("Failed to reach Switch# prompt.")
            sys.exit(1)

    def run_command(self, user_command):
        # 1. Prepare patch.txt content
        patch_content = f"""# This is a patch.txt file.
cp /home/patch/patch.txt /mnt/patch.txt
# User command:
{user_command}
"""
        
        # 2. Create tarball in memory
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
            content_bytes = patch_content.encode('utf-8')
            info = tarfile.TarInfo(name="patch/patch.txt")
            info.size = len(content_bytes)
            info.mtime = time.time()
            tar.addfile(tarinfo=info, fileobj=io.BytesIO(content_bytes))
        
        tar_data = bio.getvalue()
        
        # 3. Calculate and append MD5 suffix
        data_to_hash = tar_data.split(b'\0')[0]
        h = hashlib.md5(data_to_hash).digest()
        
        # 4. Write to TFTP directory
        try:
            with open(self.tftp_path, 'wb') as f:
                f.write(tar_data)
                f.write(h)
        except PermissionError:
            print(f"Error: Permission denied writing to {self.tftp_path}. Try running with sudo.")
            return
        except Exception as e:
            print(f"Error writing to TFTP directory: {e}")
            return

        # 5. Run download patch
        download_cmd = f"download patch {self.tftp_ip} patch.tar.gz\n"
        self.ser.write(download_cmd.encode('ascii'))
        
        if self.expect(["Do you wish to continue? [Y/N]:"]):
            self.ser.write(b"Y\n")
        
        # 6. Stream and filter output
        collected = b""
        
        # Markers to filter
        ignore_markers = [
            "Downloading file.",
            "Total data bytes sent/received:",
            "Decompressing file.",
            "patch/patch.txt",
            "Updating files.",
            "Update is Completed."
        ]
        
        while True:
            data = self.ser.read(100)
            if data:
                collected += data
                # We stop when we see "Update is Completed." AND a prompt
                if b"Update is Completed." in collected and collected.count(b"Switch#") >= 2:
                    # Final cleanup of the output to show only what we want
                    full_output = collected.decode('ascii', errors='ignore')
                    
                    # Split into lines and filter
                    lines = full_output.splitlines()
                    filtered_lines = []
                    start_printing = False
                    
                    for line in lines:
                        # Skip until we see the "Y" confirmation or the first non-clutter line
                        if not start_printing:
                            if line.strip() == "Y":
                                start_printing = True
                                continue
                            if any(marker in line for marker in ignore_markers):
                                continue
                            # If it's not a marker and not empty, it might be output
                            if line.strip() and not line.startswith("Switch#"):
                                start_printing = True
                        
                        if start_printing:
                            if any(marker in line for marker in ignore_markers):
                                continue
                            if line.strip() == "Switch#": # Skip the redundant prompts
                                continue
                            filtered_lines.append(line)
                    
                    print("\n".join(filtered_lines).strip())
                    break
            else:
                time.sleep(0.01)

    def loop(self):
        print(f"Switch Interactive REPL at {self.tftp_ip} (Ctrl-D to exit, Ctrl-C to abort command)")
        while True:
            try:
                line = input("# ")
                if not line.strip():
                    continue
                self.run_command(line)
                readline.write_history_file(self.history_file)
            except EOFError:
                print("\nExiting...")
                break
            except KeyboardInterrupt:
                print("\nAborted.")
                continue

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive Switch REPL via TFTP Patching")
    parser.add_argument("--tftp-path", default=DEFAULT_TFTP_PATH, help=f"Path to the TFTP patch file (default: {DEFAULT_TFTP_PATH})")
    parser.add_argument("--serial-port", default=DEFAULT_SERIAL_PORT, help=f"Serial port device (default: {DEFAULT_SERIAL_PORT})")
    parser.add_argument("--tftp-ip", default=DEFAULT_TFTP_SERVER_IP, help=f"TFTP server IP for the switch to connect to (default: {DEFAULT_TFTP_SERVER_IP})")
    
    args = parser.parse_args()
    
    repl = SwitchREPL(args.tftp_path, args.serial_port, args.tftp_ip)
    repl.connect()
    repl.loop()
