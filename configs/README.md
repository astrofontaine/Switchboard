# Config Templates

These files are sanitized runtime templates derived from the live environment.

- `agents/`: per-host `agent_config.json` templates
- `cron/`: cron entries used on `human` and the three agent VMs
- `nfs/`: NFS exports and per-host `fstab` examples

The live system also creates symlinks such as:

```bash
ln -sf /home/longshot/<agent>/agent_config.json /home/longshot/agent_config.json
```

and host-visible home shortcuts such as:

```bash
ln -sf /mnt/homes/keystone /home/longshot/keystone
```

Adjust those paths to match your deployment.
