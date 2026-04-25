#!/usr/bin/env python3
# /// script
# dependencies = [
#   "typer[all]",
#   "pyserial",
#   "paho-mqtt",
#   "jsonrpcserver",
#   "requests",
#   "rich",
# ]
# ///

"""
autoboot.py - A serial port manager and boot automation tool.

This script manages a serial connection to an embedded device and provides
a remote interface to control it, including automated reboots via MQTT-controlled
power sockets.

OVERALL ARCHITECTURE:
- Manager Mode: A long-running background process that takes ownership of a
  serial port (e.g., /dev/ttyUSB0). It buffers up to 10,000 lines of output,
  each tagged with an incremental line number. It exposes a JSON-RPC interface
  over HTTP (default port 8080).
- Client Mode: CLI commands that connect to the manager via RPC to issue
  instructions or retrieve buffered data.
"""

import sys
import time
import threading
import re
import json
from typing import Optional, List, Dict, Any
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler

import typer
import serial
import paho.mqtt.client as mqtt
import requests
from jsonrpcserver import method, dispatch, Success, Result
from rich.console import Console
from rich.table import Table

# --- Constants and Configuration ---
DEFAULT_SERIAL = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
DEFAULT_RPC_PORT = 8080
DEFAULT_MQTT_BROKER = "localhost"
DEFAULT_MQTT_TOPIC = "cmnd/tasmota_power/POWER"
BUFFER_SIZE = 10000
PROMPT_WAIT = "Hit Esc key to stop autoboot"
ESC_KEY = b'\x1b'

app = typer.Typer(
    help="Autoboot Manager: Control embedded devices via serial and MQTT power sockets.",
    rich_markup_mode="rich"
)
console = Console()


# --- Manager State ---
class ManagerState:
    """Maintains the shared state of the manager process."""
    def __init__(self):
        self.lines = deque(maxlen=BUFFER_SIZE)
        self.line_counter = 0
        self.serial_port: Optional[serial.Serial] = None
        self.lock = threading.Lock()
        self.rebooting = False
        self.stop_event = threading.Event()
        self.rpc_port = DEFAULT_RPC_PORT
        self.mqtt_broker = DEFAULT_MQTT_BROKER
        self.mqtt_topic = DEFAULT_MQTT_TOPIC

    def reset_buffer(self):
        """Clears the line buffer and resets the line counter."""
        with self.lock:
            self.lines.clear()
            self.line_counter = 0
            console.log("[bold blue]Line buffer and counter reset.")


state = ManagerState()


# --- RPC Methods ---
@method
def trigger_reboot() -> Result:
    """
    Triggers a device power cycle via MQTT.
    Resets the line buffer before powering the device back on.
    """
    if state.rebooting:
        return Success("Reboot already in progress")

    def do_reboot():
        state.rebooting = True
        try:
            client = mqtt.Client()
            client.connect(state.mqtt_broker)

            console.log(f"[bold yellow]Powering OFF via {state.mqtt_topic}...")
            client.publish(state.mqtt_topic, json.dumps({"state": "OFF"}))

            # Reset state while the device is off
            state.reset_buffer()
            time.sleep(2)

            console.log(f"[bold yellow]Powering ON via {state.mqtt_topic}...")
            client.publish(state.mqtt_topic, json.dumps({"state": "ON"}))
            client.disconnect()
            console.log("[bold green]Power cycle command sent.")
        except Exception as e:
            console.log(f"[bold red]Reboot failed: {e}")
        finally:
            state.rebooting = False

    threading.Thread(target=do_reboot, daemon=True).start()
    return Success("Reboot sequence initiated")


@method
def send_data(data: str) -> Result:
    """Sends raw string data to the serial port."""
    if state.serial_port and state.serial_port.is_open:
        with state.lock:
            state.serial_port.write(data.encode('utf-8'))
        return Success(f"Sent {len(data)} bytes")
    return Success("Serial port not open", False)


@method
def read_lines(start: int, end: Optional[int] = None) -> Result:
    """Returns buffered lines from start index to end index."""
    with state.lock:
        # Each entry in deque is (counter, text)
        result = [
            line for count, line in state.lines
            if count >= start and (end is None or count <= end)
        ]
    return Success(result)


@method
def filter_lines(pattern: str) -> Result:
    """Filters all buffered lines using a regex pattern."""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return Success(f"Invalid regex: {e}", False)

    with state.lock:
        result = [
            {"id": count, "text": line}
            for count, line in state.lines
            if regex.search(line)
        ]
    return Success(result)


# --- Manager Server & Serial Logic ---
class RPCRequestHandler(BaseHTTPRequestHandler):
    """Handles JSON-RPC requests over HTTP."""
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        request = self.rfile.read(content_length).decode()
        response = dispatch(request)
        if response:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.encode())

    def log_message(self, format, *args):
        pass  # Suppress server logging for cleaner output


def serial_reader(port: str, baud: int):
    """Thread to read from serial port, buffer lines, and watch for prompts."""
    try:
        state.serial_port = serial.Serial(port, baud, timeout=0.1)
        console.log(f"[bold green]Connected to {port} at {baud}")

        buffer = b""
        last_data_time = time.time()
        while not state.stop_event.is_set():
            data = state.serial_port.read(1024)
            if data:
                last_data_time = time.time()
                # Check for autoboot prompt
                if (PROMPT_WAIT.encode() in data or
                        PROMPT_WAIT.encode() in (buffer + data)):
                    console.log("[bold cyan]Autoboot prompt detected! "
                                "Sending ESC (3x)...")
                    state.serial_port.write(ESC_KEY * 3)

                buffer += data
                while b"\n" in buffer:
                    line_raw, buffer = buffer.split(b"\n", 1)
                    # Handle encoding and strip carriage returns
                    line_text = line_raw.decode('utf-8',
                                                errors='replace').strip('\r')
                    with state.lock:
                        state.line_counter += 1
                        state.lines.append((state.line_counter, line_text))
            else:
                if buffer and (time.time() - last_data_time > 0.5):
                    # Timeout reached, treat buffer as a line
                    line_text = buffer.decode('utf-8',
                                              errors='replace').strip('\r')
                    with state.lock:
                        state.line_counter += 1
                        state.lines.append((state.line_counter, line_text))
                    buffer = b""
                time.sleep(0.01)
    except Exception as e:
        console.log(f"[bold red]Serial error: {e}")
    finally:
        if state.serial_port:
            state.serial_port.close()


# --- CLI Commands ---

@app.command()
def manager(
    port: str = typer.Option(DEFAULT_SERIAL, help="Serial port device path"),
    baud: int = typer.Option(DEFAULT_BAUD, help="Serial baud rate"),
    rpc_port: int = typer.Option(DEFAULT_RPC_PORT,
                                 help="Port for the JSON-RPC server"),
    mqtt_broker: str = typer.Option(DEFAULT_MQTT_BROKER,
                                    help="MQTT broker address"),
    mqtt_topic: str = typer.Option(DEFAULT_MQTT_TOPIC,
                                   help="MQTT topic for power control")
):
    """
    Start the background manager process.

    This process takes ownership of the serial port and provides an RPC interface
    for clients to interact with the device.
    """
    state.rpc_port = rpc_port
    state.mqtt_broker = mqtt_broker
    state.mqtt_topic = mqtt_topic
    console.print(f"[bold blue]Starting Autoboot Manager on port "
                  f"{rpc_port}...")
    console.print(f"[bold blue]Using MQTT broker: {mqtt_broker}, "
                  f"topic: {mqtt_topic}")

    # Start Serial Reader Thread
    reader_thread = threading.Thread(target=serial_reader,
                                     args=(port, baud),
                                     daemon=True)
    reader_thread.start()

    # Start RPC Server (Main Thread)
    server = HTTPServer(("localhost", rpc_port), RPCRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        state.stop_event.set()
        console.print("\n[bold red]Shutting down...")
        server.server_close()


def call_rpc(method_name: str, **params) -> Any:
    """Helper to call the manager's RPC interface."""
    url = f"http://localhost:{DEFAULT_RPC_PORT}"
    payload = {
        "jsonrpc": "2.0",
        "method": method_name,
        "params": params,
        "id": 1,
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        res_json = response.json()
        if "error" in res_json:
            console.print(f"[bold red]RPC Error: {res_json['error']}")
            sys.exit(1)
        return res_json["result"]
    except requests.exceptions.ConnectionError:
        console.print("[bold red]Error: Could not connect to manager. "
                      "Is it running?")
        sys.exit(1)


@app.command()
def reboot():
    """
    Request the manager to power cycle the device and stop at U-Boot.
    """
    result = call_rpc("trigger_reboot")
    console.print(f"[bold green]{result}")


@app.command()
def send(data: str):
    """
    Send a string to the device's serial port (e.g., 'ls\\n').
    """
    # Decode escape sequences like \n, \r, \x1b
    decoded_data = data.encode('utf-8').decode('unicode_escape')
    result = call_rpc("send_data", data=decoded_data)
    console.print(f"[bold green]{result}")


@app.command()
def readlines(
    start: int = typer.Argument(..., help="Starting line number (inclusive)"),
    end: Optional[int] = typer.Argument(None,
                                        help="Ending line number (inclusive)")
):
    """
    Retrieve buffered lines from the manager by index range.
    """
    lines = call_rpc("read_lines", start=start, end=end)
    if not lines:
        console.print("[yellow]No lines found in that range.")
    for line in lines:
        print(line)


@app.command()
def filterlines(regex: str):
    """
    Search all buffered lines using a regular expression.
    """
    results = call_rpc("filter_lines", pattern=regex)
    if isinstance(results, str) and "Invalid regex" in results:
        console.print(f"[bold red]{results}")
        return

    if not results:
        console.print(f"[yellow]No lines matching '{regex}' found.")
        return

    table = Table(title=f"Matches for '{regex}'")
    table.add_column("Line #", style="cyan")
    table.add_column("Content")

    for item in results:
        table.add_row(str(item["id"]), item["text"])

    console.print(table)


if __name__ == "__main__":
    app()
