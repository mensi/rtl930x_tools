#!/usr/bin/env -S uv run --script
#
# /// script
# dependencies = [
#   "typer",
#   "platformdirs",
#   "pyserial",
#   "paho-mqtt",
# ]
# ///

import os
import sys
import time
import socket
import struct
import threading
import json
import re
import inspect
import codecs
from collections import deque
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path
import serial
import typer
import platformdirs
import paho.mqtt.publish as publish

app = typer.Typer(
    help="Serial port monitor and manager. Architecture: One 'manage' process runs per port, providing a buffer and optional boot interruption. Clients (reset, read, send, etc.) connect to the manager via Unix sockets.",
    rich_markup_mode="markdown"
)

# --- Constants and Configuration ---
DEFAULT_BAUD = 115200
DEFAULT_BUFFER_SIZE = 10000
SOCKET_TIMEOUT = 60.0

# --- TLV Protocol Helpers ---
# Type: 1 byte, Length: 4 bytes (I), Value: length bytes
TYPE_REQ = 1
TYPE_RESP_OK = 2
TYPE_RESP_ERR = 3
TYPE_PROGRESS = 4

def send_tlv(sock: socket.socket, t: int, value: Any):
    data = json.dumps(value).encode('utf-8')
    header = struct.pack('!BI', t, len(data))
    sock.sendall(header + data)

def recv_tlv(sock: socket.socket) -> Tuple[int, Any]:
    header = b""
    while len(header) < 5:
        chunk = sock.recv(5 - len(header))
        if not chunk:
            raise EOFError("Connection closed")
        header += chunk
    
    t, length = struct.unpack('!BI', header)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("Connection closed")
        data += chunk
    return t, json.loads(data.decode('utf-8'))

# --- Session Management ---
def get_session_dir() -> Path:
    d = Path(platformdirs.user_runtime_dir("serialmon"))
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_socket_path(port: str) -> Path:
    # Clean up port name for filename
    safe_name = port.replace('/', '_').replace('.', '_')
    return get_session_dir() / f"{safe_name}.sock"

# --- Manager Logic ---
class LineBuffer:
    def __init__(self, max_lines: int):
        self.max_lines = max_lines
        self.buffer: List[Tuple[int, str]] = []
        self.counter = 0
        self.lock = threading.Lock()
        self.current_line = ""
        self.last_char = ""

    def append_char(self, char: str):
        with self.lock:
            if char == '\n':
                if self.last_char == '\r':
                    # Already handled the line break on \r
                    pass
                else:
                    self._commit_line()
            elif char == '\r':
                self._commit_line()
            else:
                self.current_line += char
            self.last_char = char

    def _commit_line(self):
        # This is called with self.lock held by append_char
        self.counter += 1
        self.buffer.append((self.counter, self.current_line))
        self.current_line = ""
        if len(self.buffer) > self.max_lines:
            self.buffer.pop(0)

    def get_pos(self) -> Tuple[int, int]:
        with self.lock:
            return self.counter, len(self.current_line)

    def get_data_after(self, start_counter: int, start_pos: int) -> str:
        with self.lock:
            parts = []
            found_any = False
            for num, line in self.buffer:
                if num > start_counter:
                    if num == start_counter + 1:
                        parts.append(line[start_pos:])
                    else:
                        parts.append(line)
                    found_any = True
            
            if start_counter == self.counter:
                parts.append(self.current_line[start_pos:])
            else:
                # If we've already added lines from the buffer, we add the full current_line.
                # If we haven't found any lines > start_counter but start_counter is NOT self.counter,
                # it means start_counter is older than the buffer, so we take the full current_line too.
                parts.append(self.current_line)
            
            return "\n".join(parts)

    def get_lines(self, start: int, end: Optional[int] = None) -> List[Tuple[int, str]]:
        with self.lock:
            result = []
            for num, line in self.buffer:
                if num >= start:
                    if end is not None and num > end:
                        break
                    result.append((num, line))
            return result

    def get_range(self) -> Tuple[int, int, str]:
        with self.lock:
            if not self.buffer:
                return 0, 0, self.current_line
            return self.buffer[0][0], self.buffer[-1][0], self.current_line

    def filter_regex(self, pattern: str) -> List[Tuple[int, str]]:
        prog = re.compile(pattern)
        with self.lock:
            return [(num, line) for num, line in self.buffer if prog.search(line)]

class SerialManager:
    GPIO_DIR = 0xb8003308
    GPIO_DAT = 0xb800330c

    # YModem Constants
    SOH = b'\x01'
    STX = b'\x02'
    EOT = b'\x04'
    ACK = b'\x06'
    NAK = b'\x15'
    CAN = b'\x18'
    CRC = b'C'

    def __init__(self, port: str, baud: int, buffer_size: int, verbose: bool = False):
        self.port = port
        self.initial_baud = baud
        self.current_baud = baud
        self.verbose = verbose
        self.line_buffer = LineBuffer(buffer_size)
        self.raw_queue = deque() # For YModem raw byte access
        self.raw_mode = False # When True, skip LineBuffer processing
        self.serial_alive = True
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
        except serial.SerialException as e:
            print(f"Could not open serial port {port}: {e}")
            sys.exit(1)
        self.ser_lock = threading.Lock()
        self.reset_method = "none"
        self.reset_params = {}
        self.interrupt_method = "none"
        self.stop_event = threading.Event()

    def serial_reader(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while not self.stop_event.is_set():
            try:
                # Read all available bytes
                data = self.ser.read(self.ser.in_waiting or 1)
                if data:
                    with self.ser_lock:
                        self.raw_queue.append(data)
                    
                    if not self.raw_mode:
                        chars = decoder.decode(data)
                        if self.verbose:
                            sys.stdout.write(chars)
                            sys.stdout.flush()
                        for char in chars:
                            self.line_buffer.append_char(char)
            except (serial.SerialException, OSError) as e:
                if not self.stop_event.is_set():
                    print(f"Serial port error: {e}")
                    self.serial_alive = False
                break
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"Unexpected reader error: {e}")
                break
        self.ser.close()

    def check_serial(self) -> Tuple[bool, str]:
        if not self.serial_alive:
            return False, f"Serial port {self.port} is disconnected or inaccessible"
        if not self.ser.is_open:
            return False, f"Serial port {self.port} is closed"
        return True, ""

    def read_bytes(self, count: int, timeout: float = 5.0) -> bytes:
        """Helper for YModem to read raw bytes while reader thread is active."""
        start = time.time()
        collected = b""
        while len(collected) < count and time.time() - start < timeout:
            with self.ser_lock:
                if self.raw_queue:
                    chunk = self.raw_queue.popleft()
                    collected += chunk
                else:
                    time.sleep(0.01)
                    continue
            
            if len(collected) > count:
                # Put back extra bytes
                extra = collected[count:]
                collected = collected[:count]
                with self.ser_lock:
                    self.raw_queue.appendleft(extra)
        return collected

    def perform_reset(self) -> bool:
        # Revert to initial baud rate on reset
        if self.current_baud != self.initial_baud:
            print(f"Reverting baud rate to {self.initial_baud} for reset...")
            with self.ser_lock:
                self.ser.baudrate = self.initial_baud
            self.current_baud = self.initial_baud

        if self.reset_method == "mqtt":
            host = self.reset_params.get("host")
            topic = self.reset_params.get("topic")
            on_val = self.reset_params.get("on", "ON")
            off_val = self.reset_params.get("off", "OFF")
            
            print(f"Triggering MQTT reset on {host} topic {topic}")
            try:
                publish.single(topic, off_val, hostname=host)
                time.sleep(2)
                publish.single(topic, on_val, hostname=host)
            except Exception as e:
                print(f"MQTT error: {e}")
                return False
        
        if self.interrupt_method != "none":
            return self.handle_interrupt()
        return True

    def handle_interrupt(self) -> bool:
        if self.interrupt_method == "uboot":
            print("Waiting for U-Boot interrupt prompt...")
            pos = self.line_buffer.get_pos()
            # Send single Esc first to be less aggressive
            if self.wait_and_send("Hit Esc key to stop autoboot", "\x1b", start_pos=pos):
                pos2 = self.line_buffer.get_pos()
                return self.wait_and_send("RTL9300# ", "\r", timeout=5.0, start_pos=pos2)
            return False
        elif self.interrupt_method == "imi":
            print("Waiting for IMI interrupt marker ('No ethernet found.')...")
            pos = self.line_buffer.get_pos()
            if self.wait_and_send("No ethernet found.", None, start_pos=pos, timeout=30.0):
                print("Marker seen. Waiting for countdown...")
                start_countdown = time.time()
                while time.time() - start_countdown < 5:
                    data = self.line_buffer.get_data_after(*pos)
                    if any(c in data for c in [" 3 ", " 2 ", " 1 ", " 0 "]):
                        print("Countdown detected. Sending interrupt sequence (Ctrl+C, z, h)...")
                        with self.ser_lock:
                            self.ser.write(b'\x03zh')
                        
                        # Wait for the prompt
                        pos2 = self.line_buffer.get_pos()
                        if self.wait_and_send("RTL9300# ", "\r", timeout=5.0, start_pos=pos2):
                            print("Successfully entered U-Boot prompt!")
                            return True
                        else:
                            # Try one more Enter just in case
                            with self.ser_lock:
                                self.ser.write(b'\r')
                            if self.wait_and_send("RTL9300# ", None, timeout=2.0, start_pos=pos2):
                                print("Successfully entered U-Boot prompt!")
                                return True
                    time.sleep(0.01)
                print("Timed out waiting for countdown.")
                return False
            return False
        return True

    def wait_and_send(self, prompt: str, keys: Optional[str], timeout: float = 30.0, start_pos: Optional[Tuple[int, int]] = None):
        if start_pos is None:
            start_pos = self.line_buffer.get_pos()
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            data = self.line_buffer.get_data_after(*start_pos)
            if prompt in data:
                if keys:
                    with self.ser_lock:
                        self.ser.write(keys.encode('utf-8'))
                    print(f"Sent keys: {repr(keys)}")
                    time.sleep(0.1)
                return True
            time.sleep(0.05)
        print(f"Timed out waiting for prompt {repr(prompt)}")
        return False

    def exec_u_boot_cmd(self, cmd: str, timeout: float = 2.0) -> str:
        start_pos = self.line_buffer.get_pos()
        with self.ser_lock:
            self.ser.write(f"{cmd}\n".encode('ascii'))
        
        # Wait for prompt
        if self.wait_and_send("RTL9300# ", None, timeout=timeout, start_pos=start_pos):
            return self.line_buffer.get_data_after(*start_pos)
        return ""

    def switch_baud(self, new_baud: int) -> bool:
        print(f"Requesting device baud rate change to {new_baud}...")
        self.exec_u_boot_cmd(f"setenv baudrate {new_baud}")
        
        time.sleep(0.2)
        
        print(f"Switching manager baud rate to {new_baud}...")
        with self.ser_lock:
            self.ser.baudrate = new_baud
        self.current_baud = new_baud
        
        time.sleep(0.5)
        with self.ser_lock:
            self.ser.write(b"\r\n\n")
        
        pos = self.line_buffer.get_pos()
        if self.wait_and_send("RTL9300# ", None, timeout=5.0, start_pos=pos):
            print(f"Successfully switched to {new_baud} baud.")
            return True
        else:
            print(f"Failed to get prompt at {new_baud} baud.")
            return False

    def crc16(self, data: bytes) -> int:
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

    def upload_ymodem(self, filepath: str, addr: str, conn: Optional[socket.socket] = None) -> bool:
        if not os.path.exists(filepath):
            print(f"File not found: {filepath}")
            return False

        filesize = os.path.getsize(filepath)
        filename = os.path.basename(filepath)

        # Start the loady command
        with self.ser_lock:
            self.raw_queue.clear()
            self.ser.write(f"loady {addr}\n".encode('ascii'))

        self.raw_mode = True # Skip LineBuffer
        try:
            def wait_b(targets, timeout=30.0):
                start = time.time()
                while time.time() - start < timeout:
                    b = self.read_bytes(1, timeout=0.1)
                    if b in targets: return b
                return b''

            b = wait_b([self.CRC], timeout=30)
            if b != self.CRC:
                print(f"Timeout waiting for initial 'C' (got {repr(b)})")
                return False

            # Header packet (Block 0)
            header = filename.encode('ascii') + b'\x00' + str(filesize).encode('ascii') + b'\x00'
            header = header.ljust(128, b'\x00')
            packet = self.SOH + b'\x00\xff' + header + struct.pack('>H', self.crc16(header))
            with self.ser_lock:
                self.ser.write(packet)

            if wait_b([self.ACK]) != self.ACK: 
                return False
            if wait_b([self.CRC]) != self.CRC: 
                return False

            with open(filepath, 'rb') as f:
                block_num = 1
                sent_bytes = 0
                while True:
                    chunk = f.read(1024)
                    if not chunk: break

                    sent_bytes += len(chunk)
                    chunk = chunk.ljust(1024, b'\x1a')

                    packet = self.STX + struct.pack('BB', block_num & 0xFF, (255 - (block_num & 0xFF))) + chunk + struct.pack('>H', self.crc16(chunk))

                    # Retry loop for this block
                    retries = 5
                    while retries > 0:
                        with self.ser_lock:
                            self.ser.write(packet)

                        b = wait_b([self.ACK, self.NAK, self.CRC], timeout=20)
                        if b == self.ACK:
                            break
                        elif b in [self.NAK, self.CRC]:
                            retries -= 1
                        else:
                            return False

                    if retries == 0:
                        return False

                    block_num += 1
                    if block_num % 50 == 0:
                        percent = (sent_bytes/filesize)*100
                        if conn:
                            try:
                                send_tlv(conn, TYPE_PROGRESS, {"percent": percent})
                            except:
                                pass # Client might have closed
                        sys.stdout.write(f"\rProgress: {sent_bytes}/{filesize} bytes ({percent:.1f}%)")
                        sys.stdout.flush()

            print(f"\rProgress: {sent_bytes}/{filesize} bytes (100.0%)")

            with self.ser_lock:
                self.ser.write(self.EOT)

            b = wait_b([self.NAK], timeout=5)
            with self.ser_lock:
                self.ser.write(self.EOT)

            wait_b([self.ACK], timeout=5)
            wait_b([self.CRC], timeout=5)

            null_block = b'\x00' * 128
            packet = self.SOH + b'\x00\xff' + null_block + struct.pack('>H', self.crc16(null_block))
            with self.ser_lock:
                self.ser.write(packet)

            wait_b([self.ACK], timeout=5)

            print("\nUpload complete!")
            return True
        except Exception as e:
            print(f"YModem exception: {e}")
            return False
        finally:
            self.raw_mode = False


    def read_reg(self, addr: int) -> int:
        output = self.exec_u_boot_cmd(f"md.l {addr:#x} 1")
        prefix = f"{addr & 0xffffffff:08x}: "
        for line in output.splitlines():
            if prefix in line:
                parts = line.split(prefix)[1].strip().split()
                if parts:
                    try:
                        return int(parts[0], 16)
                    except ValueError:
                        pass
        return 0

    def disable_hasivo_mcu_watchdog(self) -> bool:
        print("Attempting to disable Hasivo MCU watchdog via I2C bit-banging...")
        
        # Get current state
        initial_dir = self.read_reg(self.GPIO_DIR)
        initial_dat = self.read_reg(self.GPIO_DAT)
        if initial_dir == 0 and initial_dat == 0:
            print("Failed to read GPIO registers. Are you at a U-Boot prompt?")
            return False
            
        print(f"Initial GPIO DIR: {initial_dir:#010x}, DAT: {initial_dat:#010x}")
        
        state = {'dir': initial_dir, 'dat': initial_dat}
        
        def update_dir():
            self.exec_u_boot_cmd(f"mw.l {self.GPIO_DIR:#x} {state['dir']:#x} 1")

        def update_dat():
            self.exec_u_boot_cmd(f"mw.l {self.GPIO_DAT:#x} {state['dat']:#x} 1")

        def i2c_set_scl(val):
            if val: state['dat'] |= (1 << 3)
            else: state['dat'] &= ~(1 << 3)
            update_dat()

        def i2c_set_sda(val):
            if val: state['dat'] |= (1 << 4)
            else: state['dat'] &= ~(1 << 4)
            update_dat()

        def i2c_set_sda_dir(is_output):
            if is_output: state['dir'] |= (1 << 4)
            else: state['dir'] &= ~(1 << 4)
            update_dir()

        # Initial state: SCL, SDA high and output
        state['dir'] |= (1 << 3) | (1 << 4)
        state['dat'] |= (1 << 3) | (1 << 4)
        update_dir()
        update_dat()

        def i2c_start():
            i2c_set_sda(1); i2c_set_scl(1); i2c_set_sda(0); i2c_set_scl(0)

        def i2c_stop():
            i2c_set_sda(0); i2c_set_scl(1); i2c_set_sda(1)

        def i2c_write_byte(byte):
            for i in range(8):
                i2c_set_sda((byte >> (7 - i)) & 1)
                i2c_set_scl(1); i2c_set_scl(0)
            i2c_set_sda_dir(False)
            i2c_set_scl(1); i2c_set_scl(0)
            i2c_set_sda_dir(True)

        def i2c_read_byte():
            byte = 0
            i2c_set_sda_dir(False)
            for i in range(8):
                i2c_set_scl(1)
                val = self.read_reg(self.GPIO_DAT)
                bit = (val >> 4) & 1
                byte = (byte << 1) | bit
                i2c_set_scl(0)
            return byte

        def i2c_send_ack(ack):
            i2c_set_sda_dir(True)
            i2c_set_sda(0 if ack else 1)
            i2c_set_scl(1); i2c_set_scl(0); i2c_set_sda(1)

        def i2c_read_reg(addr, reg):
            i2c_start()
            i2c_write_byte(addr)
            i2c_write_byte(reg)
            i2c_start()
            i2c_write_byte(addr | 1)
            val = i2c_read_byte()
            i2c_send_ack(False)
            i2c_stop()
            return val

        def i2c_write_reg(addr, reg, val):
            i2c_start()
            i2c_write_byte(addr)
            i2c_write_byte(reg)
            i2c_write_byte(val)
            i2c_stop()

        found_addr = None
        print("Checking for MCU watchdog...")
        if i2c_read_reg(0xde, 0xfd) == 0x91:
            found_addr = 0xde
        elif i2c_read_reg(0xdc, 0x66) == 0x91:
            found_addr = 0xdc

        if found_addr:
            print(f"Found MCU watchdog at {found_addr:#x}. Disabling...")
            i2c_write_reg(found_addr, 0x09, 0x4f)
            i2c_write_reg(found_addr, 0x0a, 0x3f)
            print("MCU watchdog disabled.")
            return True
        else:
            print("MCU watchdog not found.")
            return False

    def run_server(self, socket_path: Path):
        if socket_path.exists():
            socket_path.unlink()
        
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(5)
        server.settimeout(0.5)
        
        while not self.stop_event.is_set():
            try:
                conn, _ = server.accept()
                threading.Thread(target=self.handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
        server.close()

    def handle_client(self, conn: socket.socket):
        with conn:
            try:
                t, req = recv_tlv(conn)
                if t != TYPE_REQ:
                    send_tlv(conn, TYPE_RESP_ERR, "Invalid request type")
                    return
                
                cmd = req.get("cmd")
                args = req.get("args", {})
                
                # Port check for commands that need serial
                if cmd in ["reset", "disable-hasivo-mcu-watchdog", "switch-baud", "upload-ymodem", "send"]:
                    alive, err = self.check_serial()
                    if not alive:
                        send_tlv(conn, TYPE_RESP_ERR, err)
                        return

                if cmd == "reset":
                    success = self.perform_reset()
                    if success:
                        send_tlv(conn, TYPE_RESP_OK, "Reset complete")
                    else:
                        send_tlv(conn, TYPE_RESP_ERR, "Reset/Interrupt failed")
                elif cmd == "disable-hasivo-mcu-watchdog":
                    success = self.disable_hasivo_mcu_watchdog()
                    if success:
                        send_tlv(conn, TYPE_RESP_OK, "Hasivo MCU watchdog disabled")
                    else:
                        send_tlv(conn, TYPE_RESP_ERR, "Failed to disable Hasivo MCU watchdog")
                elif cmd == "switch-baud":
                    success = self.switch_baud(args.get("baud"))
                    if success:
                        send_tlv(conn, TYPE_RESP_OK, "Baud rate switched")
                    else:
                        send_tlv(conn, TYPE_RESP_ERR, "Failed to switch baud rate")
                elif cmd == "upload-ymodem":
                    success = self.upload_ymodem(args.get("filepath"), args.get("addr"), conn)
                    if success:
                        send_tlv(conn, TYPE_RESP_OK, "Upload complete")
                    else:
                        send_tlv(conn, TYPE_RESP_ERR, "Upload failed")
                elif cmd == "lines":
                    send_tlv(conn, TYPE_RESP_OK, self.line_buffer.get_range())
                elif cmd == "read":
                    lines = self.line_buffer.get_lines(args.get("start"), args.get("end"))
                    send_tlv(conn, TYPE_RESP_OK, lines)
                elif cmd == "regex":
                    lines = self.line_buffer.filter_regex(args.get("pattern"))
                    send_tlv(conn, TYPE_RESP_OK, lines)
                elif cmd == "send":
                    text = args.get("text")
                    prompt = args.get("prompt")
                    timeout = args.get("timeout", 30.0)
                    
                    start_pos = self.line_buffer.get_pos()
                    with self.ser_lock:
                        self.ser.write(text.encode('utf-8'))
                    
                    if prompt:
                        success = self.wait_and_send(prompt, "", timeout, start_pos=start_pos)
                        if success:
                            send_tlv(conn, TYPE_RESP_OK, "Sent and prompt found")
                        else:
                            send_tlv(conn, TYPE_RESP_ERR, f"Timed out waiting for prompt: {prompt}")
                    else:
                        send_tlv(conn, TYPE_RESP_OK, "Sent")
                else:
                    send_tlv(conn, TYPE_RESP_ERR, f"Unknown command: {cmd}")
            except Exception as e:
                try:
                    send_tlv(conn, TYPE_RESP_ERR, str(e))
                except:
                    pass


# --- CLI Commands ---

@app.command()
def manage(
    port: str = typer.Argument(..., help="Serial port to manage (e.g., /dev/ttyUSB0)."),
    baud: int = typer.Option(DEFAULT_BAUD, help="Baud rate for the serial connection."),
    buffer_size: int = typer.Option(DEFAULT_BUFFER_SIZE, help="Maximum number of lines to keep in the history buffer."),
    mqtt_host: Optional[str] = typer.Option(None, help="MQTT host for smart socket reset"),
    mqtt_topic: Optional[str] = typer.Option(None, help="MQTT topic for smart socket reset"),
    mqtt_on: str = typer.Option("ON", help="MQTT payload for ON"),
    mqtt_off: str = typer.Option("OFF", help="MQTT payload for OFF"),
    interrupt_boot: str = typer.Option("none", help="Boot interrupt method (uboot, imi, none)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show serial traffic"),
):
    """Start manager mode for a serial port. This runs in the foreground."""
    mgr = SerialManager(port, baud, buffer_size, verbose=verbose)
    if mqtt_host and mqtt_topic:
        mgr.reset_method = "mqtt"
        mgr.reset_params = {"host": mqtt_host, "topic": mqtt_topic, "on": mqtt_on, "off": mqtt_off}
    mgr.interrupt_method = interrupt_boot
    
    socket_path = get_socket_path(port)
    print(f"Starting manager on {port}")
    print(f"Socket: {socket_path}")
    print("Press Ctrl+C to stop.")
    
    reader_thread = threading.Thread(target=mgr.serial_reader, daemon=True)
    reader_thread.start()
    
    try:
        mgr.run_server(socket_path)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        mgr.stop_event.set()
        if socket_path.exists():
            socket_path.unlink()

@app.command()
def sessions():
    """List all currently active serialmon manager sessions by their port names."""
    sessions = list(get_session_dir().glob("*.sock"))
    if not sessions:
        print("No active sessions found.")
    else:
        print("Active sessions:")
        for s in sessions:
            print(f"  {s.stem.replace('_', '/')}")

def client_request(cmd: str, args: Dict[str, Any] = None, port: Optional[str] = None, timeout: float = SOCKET_TIMEOUT) -> Tuple[bool, Any]:
    sessions = list(get_session_dir().glob("*.sock"))
    if not sessions:
        print("No active sessions found. Run 'manage' first.")
        sys.exit(1)
    
    if port:
        socket_path = get_socket_path(port)
    elif len(sessions) == 1:
        socket_path = sessions[0]
    else:
        print("Multiple sessions active, please specify port with --port:")
        for s in sessions:
            print(f"  {s.stem.replace('_', '/')}")
        sys.exit(1)

    if not socket_path.exists():
        print(f"Socket {socket_path} does not exist for port {port}.")
        sys.exit(1)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            send_tlv(client, TYPE_REQ, {"cmd": cmd, "args": args or {}})
            
            last_progress = False
            while True:
                t, resp = recv_tlv(client)
                if t == TYPE_PROGRESS:
                    percent = resp.get("percent", 0)
                    sys.stdout.write(f"\rProgress: {percent:.1f}%")
                    sys.stdout.flush()
                    last_progress = True
                    continue
                
                if last_progress:
                    print() # Newline after progress bar
                
                if t == TYPE_RESP_OK:
                    return True, resp
                else:
                    return False, resp
    except ConnectionRefusedError:
        print(f"Connection refused to {socket_path}. Is the manager running?")
        sys.exit(1)
    except socket.timeout:
        print(f"Error connecting to manager: timed out after {timeout}s")
        sys.exit(1)
    except Exception as e:
        print(f"Error connecting to manager: {e}")
        sys.exit(1)

@app.command()
def reset(port: Optional[str] = typer.Option(None, help="Specific port session to use. Optional if only one session is active.")):
    """
    Trigger a device reset. 
    
    If an interrupt method is configured in the manager, this command will wait until 
    the boot process is successfully interrupted before returning.
    """
    ok, resp = client_request("reset", port=port)
    print(resp)
    if not ok:
        sys.exit(1)

@app.command()
def disable_hasivo_mcu_watchdog(port: Optional[str] = typer.Option(None, help="Specific port session to use.")):
    """
    Disable the Hasivo MCU watchdog. 
    
    This command performs manual I2C bit-banging via U-Boot GPIO commands to talk 
    to the MCU and disable the hardware watchdog. The device must be at a U-Boot prompt.
    """
    ok, resp = client_request("disable-hasivo-mcu-watchdog", port=port)
    print(resp)
    if not ok:
        sys.exit(1)

@app.command()
def switch_baud(
    baud: int = typer.Argument(..., help="New baud rate to switch to."),
    port: Optional[str] = typer.Option(None, help="Specific port session to use.")
):
    """
    Switch the serial baud rate on both the device and the manager.
    
    The manager will automatically revert to the initial baud rate upon a device reset.
    """
    ok, resp = client_request("switch-baud", {"baud": baud}, port=port)
    print(resp)
    if not ok:
        sys.exit(1)

@app.command()
def upload_ymodem(
    filepath: str = typer.Argument(..., help="Path to the file to upload."),
    addr: str = typer.Argument(..., help="U-Boot memory address to upload to (e.g., 0x81000000)."),
    port: Optional[str] = typer.Option(None, help="Specific port session to use.")
):
    """Upload a file to the device memory using the YModem protocol."""
    ok, resp = client_request("upload-ymodem", {"filepath": filepath, "addr": addr}, port=port)
    print(resp)
    if not ok:
        sys.exit(1)

@app.command()
def lines(port: Optional[str] = typer.Option(None, help="Specific port session to use.")):
    """
    Get the range of line numbers currently available in the buffer.
    
    Returns the oldest and newest line counters, and the content of the current (incomplete) line.
    """
    ok, resp = client_request("lines", port=port)
    if not ok:
        print(f"Error: {resp}")
        sys.exit(1)
    start, end, current = resp
    print(f"Lines in buffer: {start} to {end}")
    print(f"Current line (partial): {repr(current)}")

@app.command()
def read(
    start: int = typer.Argument(..., help="Starting line number (inclusive)."),
    end: Optional[int] = typer.Argument(None, help="Ending line number (inclusive). If omitted, reads until the end of the buffer."),
    port: Optional[str] = typer.Option(None, help="Specific port session to use.")
):
    """Read a specific range of lines from the history buffer."""
    ok, resp = client_request("read", {"start": start, "end": end}, port=port)
    if not ok:
        print(f"Error: {resp}")
        sys.exit(1)
    for num, line in resp:
        print(f"{num:6}: {line}")

@app.command()
def regex(
    pattern: str = typer.Argument(..., help="Regex pattern to search for in buffered lines."),
    port: Optional[str] = typer.Option(None, help="Specific port session to use.")
):
    """Search the entire buffer for lines matching the given regex pattern."""
    ok, resp = client_request("regex", {"pattern": pattern}, port=port)
    if not ok:
        print(f"Error: {resp}")
        sys.exit(1)
    for num, line in resp:
        print(f"{num:6}: {line}")

@app.command()
def send(
    text: str = typer.Argument(..., help="String to send to the device."),
    prompt: Optional[str] = typer.Option(None, help="Optional: Wait for this string to appear in the output after sending."),
    timeout: float = typer.Option(30.0, help="Timeout in seconds when waiting for a prompt."),
    port: Optional[str] = typer.Option(None, help="Specific port session to use.")
):
    """Send raw text to the serial port."""
    ok, resp = client_request("send", {"text": text, "prompt": prompt, "timeout": timeout}, port=port)
    print(resp)
    if not ok:
        sys.exit(1)

def sendline(**kwargs):
    """Send text followed by a carriage return (\\r)."""
    if "text" in kwargs:
        kwargs["text"] += "\r"
    return send(**kwargs)

# Copy the signature from send to sendline to inherit all its flags and options
sendline.__signature__ = inspect.signature(send)
app.command(name="sendline")(sendline)

if __name__ == "__main__":
    app()
