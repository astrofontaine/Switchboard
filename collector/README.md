# Collector

Collector provides two command-line programs that connect to a remote system, run configured commands, stream output in real time, and save a full session log to a file.

## Programs

### `collector.py` (Python)
- Reads `collector_config.json`.
- Supports `ssh` and `telnet`.
- Connects, runs each configured command, prints output live, writes session output to a timestamped file, then disconnects.
- SSH mode uses `paramiko` (`pip install paramiko`).

### `collector.sh` (Bash)
- Reads `collector_config.json`.
- Supports `ssh` only (native `ssh` in `PATH`).
- Opens one shared SSH session so login is not repeated for every command.
- Streams output live and writes session output to a timestamped file.
- Handles `sudo` commands with password retry/fallback logic during the same run.

## Shared Configuration File

Both programs use `collector_config.json`.

Example:

```json
{
  "connection": {
    "access_method": "ssh",
    "host": "192.168.1.10",
    "port": 22,
    "username": "admin",
    "password": "change_me",
    "timeout_seconds": 20,
    "prompt": "$"
  },
  "commands": [
    "ipconfig",
    "arp -a",
    "sudo iptables -L"
  ],
  "output": {
    "path": "./output",
    "default_filename": "collector_output.txt"
  }
}
```

### Config fields
- `connection.access_method`: `ssh` or `telnet` (Bash script supports `ssh` only).
- `connection.host`: target hostname or IP.
- `connection.port`: remote port (`22` for SSH, `23` for Telnet by convention).
- `connection.username`: login user.
- `connection.password`: login password (and sudo fallback in Bash).
- `connection.timeout_seconds`: network timeout.
- `connection.prompt`: prompt marker for Telnet reads.
- `commands`: ordered list of commands to execute remotely.
- `output.path`: directory for output logs.
- `output.default_filename`: base filename for logs.

## Output Files

Output filenames include host and timestamp:

`<default_stem>_<host>_<YYYYMMDD_HHMM>.<ext>`

Example:

`collector_output_192.168.1.10_20260227_0745.txt`

## Usage

```bash
python3 collector.py --config collector_config.json
./collector.sh --config collector_config.json
```
