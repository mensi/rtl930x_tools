# Introduction

This file contains the design for the script serialmon.py. It's overall purpose
is to provide a user-friendly utility to deal with embedded device debugging
and development over serial port.

## General Structure

The script is implemented as a self-contained, single-file, Astral uv style
script. It uses uv's shebang and inline dependency declarations. It implements
a CLI using the typer library.

The CLI is meant to be run in 2 modes:
 - Manager mode runs a background process, owning the a serial port and managing it.
 - Client mode, which connects to the background process to run individual commands.

## Communication between client and background process

The platformdirs library is used to manage unix sockets if available, or text files
containing port numbers for each running background process session. The background
process listens on the unix socket or on a localhost port to accept client commands.

The unix socket or localhost TCP connection uses a simple TLV protocol to serialize
data using only python builtin modules.

## Manager mode

The `manage` CLI command runs manager mode. It takes a serial port as an argument.
The port is opened with optional flags controlling baud rate etc. Manager mode creates
a new socket / port file in a suitable platformdir for serialmon and listens for clients.
The session file / socket is named after the serial port.

A background thread constantly reads from the serial port. A flag-controlled amount
of lines defaulting to 10000 is kept in a buffer. This buffer stores each line combined
with an incremental line counter. The counter keeps running even when the buffer starts
dropping lines due to the limit. Extra care needs to be taken for the characters of the
current line: They constantly need to be appended to the current line and a new line started
after a suitable end of line character is received.

In manage mode, we also have optional support to reset the embedded device. For now, we will
only implement toggling a smart socket over MQTT, but we need to implement it in
a way that easily supports adding more methods. Each reset method should therefore
live in its own method, and we select the active method based on the command line
flags passed to `manage`. For MQTT, the flags take an MQTT server and topic. The
payload sent can be controlled by a pair of flags, with the on flag defaulting
to the string 'ON' and the off flag to 'OFF'.

A reset cycle consists of turning the device off, waiting 2 seconds and then
turning it back on.

An additional feature in manage mode is to automatically interrupt boot. Here we
also need to use an extensible approach. Which one is used can be controlld with
a flag --interrupt-boot=METHOD. For now, we support the following methods:

 - `uboot`: Press the Esc key and a carriage return when this prompt is seen: "Hit Esc key to stop autoboot", then wait for the prompt to appear.
 - `imi`: Press Ctrl-C, z, h in sequence when this prompt is seen: "No ethernet found.", then wait for the prompt to appear.

The interrupt boot mode is applied when the reset command is executed. If an interruption
mode is selected, the reset command will only complete after boot has interrupted. If no
mode is selected, the reset command returns immediately after the device is turned back on.

## Client commands

Client commands are all dedicated CLI commands that discover the active session in
the platformdir, connect to it and run the command via RPC. If multiple sessions
are alive, the command exits listing the available sessions. The user then must run
the CLI again with a flag indicating which session to use.

Errors returned from the background manage process need to be returned to the user.

### reset

The `reset` command triggers a reset sequence.

### lines

The `lines` command returns the first and last line numbers currently held in the buffer.

### read

The `read` command takes a starting line and an optional end line. It queries the line
range from the background process and prints them to stdout, prefixing each line with its
line number. If no end line is given, all lines in the buffer starting from the start line
are returned.

### regex

The `regex` command takes a regex. The backend filters lines against the given regex and
only returns matching lines. The client prints each line prefixed with its line number.

### send

The `send` command takes a string to be sent to the serial port. Optionally, a prompt string
can be specified. If so, the backend waits until the given prompt string is seen after sending
before returning. An optional timeout defaulting to 30s puts an upper bound on the wait time.
The purpose of this functionality is to wait for a prompt to reappear after running a command,
so that the next command can immediatly be sent.

## Implementation guidance

 - Code stlye must follow PEP8
 - Code must be thread-safe and use locking where appropriate
 - Errors should be expressive and tell the user what's wrong and how to fix it
 - CLI help texts must be self-explanatory

