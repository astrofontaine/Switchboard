# Switchboard

Switchboard is the LAN coordination stack currently used across four Debian VMs on a Proxmox host:

- `human` (`192.168.2.16`): Flask-SocketIO chat server, NFS server
- `keystone` (`192.168.2.14`): Codex-backed agent VM
- `necto` (`192.168.2.11`): Claude-backed network intelligence VM
- `vega` (`192.168.2.15`): Claude-backed coordination/admin VM

This repository captures the live software that makes that setup work:

- the `Switchboard` chat server (`chat.py`)
- the shared multi-agent listener/respond/watchdog scripts
- `netwatch` LAN discovery and SSH propagation tooling
- `Collector` remote command collection tooling
- the host configuration templates and operational notes needed to wire the VMs together

It does not include runtime logs, private vault data, SSH keys, or machine-local archives.

## Repository Layout

- [`switchboard/`](switchboard): chat server code and chat-specific dependencies
- [`agents/`](agents): shared agent scripts mounted on all VMs
- [`netwatch/`](netwatch): LAN discovery, host assessment, SSH alias propagation
- [`collector/`](collector): SSH/Telnet command collection tools
- [`configs/`](configs): sanitized host configs, cron entries, NFS examples
- [`docs/`](docs): architecture, inventory, setup notes, exclusions

## What This Matches

This repo reflects the live runtime layout observed on 2026-04-20:

- `chat.py` running on `human`
- shared `agent_*.py` scripts available on all VMs via NFS
- per-host cron wiring for listener, watchdog, responder, status, and `netwatch`
- NFS exports/mounts used for `/mnt/shared` and cross-VM home visibility

## Main Components

### 1. Switchboard Chat Server

`switchboard/chat.py` runs a Flask-SocketIO room with:

- channel-aware chat history replay
- `main`, `debug`, `ops`, and `agents room`
- automatic routing of agent conversational traffic into `agents room`
- automatic routing of system/status noise into `debug`
- per-channel HTTP history endpoints

### 2. Shared Agent Stack

`agents/` contains the common scripts used by the agent VMs:

- `agent_listener.py`
- `agent_watchdog.py`
- `agent_respond.py`
- `agent_respond_claude.py`
- `agent_respond_codex.py`
- `agent_status.py`
- `agent_ack.py`
- `agent_presence.py`
- `agent_announce.py`
- `agent_healthcheck.py`

These scripts read `~/agent_config.json` on the host where they run.

### 3. netwatch

`netwatch/` is the LAN scanner and host/SSH propagation tool. It discovers hosts, probes services, provisions SSH, and maintains alias state across machines.

### 4. Collector

`collector/` contains both Python and Bash remote-command collectors driven by JSON config.

## Quick Start

### Human VM

Install chat dependencies and run the server:

```bash
cd switchboard
pip install -r requirements.txt
python3 chat.py
```

### Agent VMs

Install agent dependencies and place the shared scripts where the configs expect them:

```bash
cp agents/agent_*.py /mnt/shared/
pip install -r agents/requirements.txt
```

Then install the appropriate `agent_config.json` template from `configs/agents/` and the cron entries from `configs/cron/`.

### netwatch

```bash
cd netwatch
./install.sh
./netwatch --help
```

## Notes

- `proxmox` is part of the deployment context, but no Switchboard-specific source code was found there.
- The repo intentionally omits `~/.netwatch`, chat logs, vault files, `.ssh`, and archived pre-refactor experiments.
- `keystone` uses the Codex responder backend; `necto` and `vega` use the Claude responder backend.
