#!/usr/bin/env python3
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
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path
import serial
import typer
import platformdirs
import paho.mqtt.publish as publish

app = typer.Typer(help="Serial port monitor and manager")

# --- Constants and Configuration ---
DEFAULT_BAUD = 115200
DEFAULT_BUFFER_SIZE = 10000
SOCKET_TIMEOUT = 60.0

# --- TLV Protocol Helpers ---
# Type: 1 byte, Length: 4 bytes (I), Value: length bytes
TYPE_REQ = 1
TYPE_RESP_OK = 2
TYPE_RESP_ERR = 3

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
    def __init__(self, port: str, baud: int, buffer_size: int):
        self.port = port
        self.baud = baud
        self.line_buffer = LineBuffer(buffer_size)
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
        while not self.stop_event.is_set():
            try:
                data = self.ser.read(1)
                if data:
                    char = data.decode('utf-8', errors='replace')
                    self.line_buffer.append_char(char)
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"Serial error: {e}")
                break
        self.ser.close()

    def perform_reset(self):
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
        
        if self.interrupt_method != "none":
            self.handle_interrupt()

    def handle_interrupt(self):
        if self.interrupt_method == "uboot":
            print("Waiting for U-Boot interrupt prompt...")
            pos = self.line_buffer.get_pos()
            # Send single Esc first to be less aggressive
            if self.wait_and_send("Hit Esc key to stop autoboot", "\x1b", start_pos=pos):
                pos2 = self.line_buffer.get_pos()
                self.wait_and_send("RTL9300# ", "\r", timeout=5.0, start_pos=pos2)
        elif self.interrupt_method == "imi":
            print("Waiting for IMI interrupt prompt...")
            pos = self.line_buffer.get_pos()
            if self.wait_and_send("No ethernet found.", "\x03zh", start_pos=pos):
                pos2 = self.line_buffer.get_pos()
                self.wait_and_send("RTL9300# ", "\r", timeout=5.0, start_pos=pos2)

    def wait_and_send(self, prompt: str, keys: str, timeout: float = 30.0, start_pos: Optional[Tuple[int, int]] = None):
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
                
                if cmd == "reset":
                    self.perform_reset()
                    send_tlv(conn, TYPE_RESP_OK, "Reset complete")
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
    port: str = typer.Argument(..., help="Serial port to manage (e.g. /dev/ttyUSB0)"),
    baud: int = typer.Option(DEFAULT_BAUD, help="Baud rate"),
    buffer_size: int = typer.Option(DEFAULT_BUFFER_SIZE, help="Number of lines to keep in buffer"),
    mqtt_host: Optional[str] = typer.Option(None, help="MQTT host for smart socket reset"),
    mqtt_topic: Optional[str] = typer.Option(None, help="MQTT topic for smart socket reset"),
    mqtt_on: str = typer.Option("ON", help="MQTT payload for ON"),
    mqtt_off: str = typer.Option("OFF", help="MQTT payload for OFF"),
    interrupt_boot: str = typer.Option("none", help="Boot interrupt method (uboot, imi, none)"),
):
    """Start manager mode for a serial port. This runs in the foreground."""
    mgr = SerialManager(port, baud, buffer_size)
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
    """List active serialmon sessions."""
    sessions = list(get_session_dir().glob("*.sock"))
    if not sessions:
        print("No active sessions found.")
    else:
        print("Active sessions:")
        for s in sessions:
            print(f"  {s.stem.replace('_', '/')}")

def client_request(cmd: str, args: Dict[str, Any] = None, port: Optional[str] = None):
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
            client.settimeout(SOCKET_TIMEOUT)
            client.connect(str(socket_path))
            send_tlv(client, TYPE_REQ, {"cmd": cmd, "args": args or {}})
            t, resp = recv_tlv(client)
            if t == TYPE_RESP_OK:
                return resp
            else:
                print(f"Error from manager: {resp}")
                sys.exit(1)
    except ConnectionRefusedError:
        print(f"Connection refused to {socket_path}. Is the manager running?")
        sys.exit(1)
    except Exception as e:
        print(f"Error connecting to manager: {e}")
        sys.exit(1)

@app.command()
def reset(port: Optional[str] = typer.Option(None, help="Serial port session to use")):
    """Trigger a reset sequence on the device."""
    print(client_request("reset", port=port))

@app.command()
def lines(port: Optional[str] = typer.Option(None, help="Serial port session to use")):
    """Get the first and last line numbers currently held in the buffer."""
    start, end, current = client_request("lines", port=port)
    print(f"Lines in buffer: {start} to {end}")
    print(f"Current line: {repr(current)}")

@app.command()
def read(
    start: int = typer.Argument(..., help="Starting line number"),
    end: Optional[int] = typer.Argument(None, help="Ending line number (optional)"),
    port: Optional[str] = typer.Option(None, help="Serial port session to use")
):
    """Read a range of lines from the buffer."""
    lines = client_request("read", {"start": start, "end": end}, port=port)
    for num, line in lines:
        print(f"{num:6}: {line}")

@app.command()
def regex(
    pattern: str = typer.Argument(..., help="Regex pattern to filter lines"),
    port: Optional[str] = typer.Option(None, help="Serial port session to use")
):
    """Filter lines in the buffer against a regex and print matches."""
    lines = client_request("regex", {"pattern": pattern}, port=port)
    for num, line in lines:
        print(f"{num:6}: {line}")

@app.command()
def send(
    text: str = typer.Argument(..., help="Text to send to the serial port"),
    prompt: Optional[str] = typer.Option(None, help="Wait for this prompt after sending"),
    timeout: float = typer.Option(30.0, help="Timeout for waiting for prompt"),
    port: Optional[str] = typer.Option(None, help="Serial port session to use")
):
    """Send a string to the serial port and optionally wait for a prompt."""
    print(client_request("send", {"text": text, "prompt": prompt, "timeout": timeout}, port=port))

if __name__ == "__main__":
    app()
