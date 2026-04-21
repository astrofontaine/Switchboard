# Setup Notes

## 1. Human VM

Expected roles:

- hosts `switchboard/chat.py`
- exports `/home/longshot/shared` and `/home/longshot`
- stores shared chat history and replay files

Install:

```bash
cd switchboard
pip install -r requirements.txt
python3 chat.py
```

Apply NFS config from `configs/nfs/exports.human` and `configs/nfs/fstab.human`.

## 2. Agent VMs

For each of `keystone`, `necto`, and `vega`:

1. Copy shared scripts into `/mnt/shared`.
2. Install `python-socketio`.
3. Install the host-specific `agent_config.json` template from `configs/agents/`.
4. Apply the matching cron template from `configs/cron/`.
5. Ensure `/mnt/shared` is mounted from `human`.

## 3. netwatch

Run `netwatch/install.sh` on each VM where discovery and SSH propagation are required.

## 4. Collector

Edit `collector/collector_config.json`, then run either:

```bash
python3 collector.py --config collector_config.json
./collector.sh --config collector_config.json
```

## 5. Deployment Caveats

- The live system uses cron, not systemd units, for the agent stack.
- The live chat server currently runs as a long-lived Python process rather than a managed service.
- `keystone` requires the Codex CLI available at the configured path.
- Claude-backed hosts require their configured Claude CLI path.
