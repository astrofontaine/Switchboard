# Inventory

This repository was curated from the live multi-VM environment on 2026-04-20.
It is intentionally a cleaned source snapshot, not a byte-for-byte filesystem export.

## Included Sources

### `human` (`192.168.2.16`)

- `/home/longshot/chat.py`
- `/home/longshot/shared/agent_*.py`
- `/home/longshot/netwatch/`

### `keystone` (`192.168.2.14`)

- `~/agent_config.json` structure and cron wiring
- NFS mounts and repo/runtime layout validation

### `necto` (`192.168.2.11`)

- live `Collector/` source
- `~/agent_config.json` structure and cron wiring

### `vega` (`192.168.2.15`)

- `~/agent_config.json` structure and cron wiring

### `proxmox` (`192.168.2.2`)

- deployment context only
- no Switchboard-specific source files were found under `/home/longshot`

## Included in Repo

- live `chat.py` from `human`
- shared agent scripts from `/mnt/shared`
- full `netwatch` source tree required by `netwatch.py`
- `Collector` source and sample config
- sanitized `agent_config.json` templates for each VM
- cron templates for `human` and the three agent VMs
- NFS `exports` and `fstab` examples

## Explicitly Excluded

- `UNIVERSAL_CHAT.log`, `CHAT_REPLAY*`, and all runtime logs
- `~/.netwatch` runtime state and encrypted vault files
- `~/.ssh` keys and SSH config
- `.git/`, `.venv/`, and `__pycache__/`
- archived pre-refactor experiments and graveyard scripts
- local wrap-up notes and one-off operator files not required to run the stack

## Canonicality Notes

- The shared `agent_*.py` scripts were byte-identical across `human`, `keystone`, `necto`, and `vega`.
- `netwatch` was byte-identical across all four VMs.
- `Collector` was byte-identical between `necto` and archived copies on the other VMs.
