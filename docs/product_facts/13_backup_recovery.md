# F-Pulse backup, restore, disaster recovery

Per `edition-matrix.md` line 73, backup capabilities split as follows.

## OSS Free includes

- **Manual backup** — operator-triggered backup of the SQLite database + data dir
- **Cloud destinations** — S3, Azure Blob, GCS as backup targets (configured via the Settings → Backup page)
- **Restore** — operator-triggered restore from a previously-saved backup

**Where to find it:** Settings → Backup → "Create backup now". Pick destination (local filesystem, S3, Azure, GCS). Authentication via the regular Credentials page (S3 needs an `s3` credential type, etc.).

**What gets backed up:**
- The SQLite database (`fpulse.db`) — pipelines, projects, schedules, alerts, connections, credentials (encrypted ciphertext), executions, audit log, RAG index
- The `data/` directory — Parquet checkpoints, eval results, staging files

**What does NOT get backed up automatically:**
- The master encryption key (`~/.fpulse/secret.key`) — backup separately
- The `.env` file — backup separately
- Ollama's downloaded models — these can be re-pulled

## F-Pulse+ adds

- **Scheduled backups** — daily/weekly/monthly schedule (set inside F-Pulse, no separate OS task needed), with the same destination options
- **Retention policy** — keep N daily, M weekly, K monthly tarballs (configurable)
- **Parquet archive** — long-term archive of executions to a Parquet table on S3/GCS for cost-efficient storage. Lets operators drop the SQLite execution rows after archive.

## Disaster recovery

Per `docs/deployment.md`:

### Three escalation tiers

**Container died** — `docker compose up -d`. Volume data intact, just bring back up.

**Volume corrupted** — restore from the most recent tarball. Standard flow:
1. Stop F-Pulse: `docker compose down`
2. Restore the data tarball: `docker run --rm -v fpulse_data:/data -v $(pwd):/backup alpine tar xzf /backup/fpulse-20260504.tar.gz -C /data`
3. Restore the master key file to `~/.fpulse/secret.key` (or wherever `FPULSE_MASTER_KEY_FILE` points)
4. Start: `docker compose up -d`

**Whole host gone** — need three things to rebuild:
- The data tarball
- The master key file (without it, encrypted credentials are unrecoverable)
- The pinned `FPULSE_IMAGE_TAG` from `.env` so the new install matches the schema version

### What you cannot recover without the master key

If the master key is lost, the encrypted credential blobs in the SQLite database are unrecoverable. Operators must:
1. Re-enter every credential via the Credentials page
2. The pipelines themselves still work — they reference credentials by ID, not by value

This is the trade-off of local-first encryption: no central key recovery service. Back up the key.

## Recommended backup schedule (OSS Free) — Linux example using cron

```bash
# 03:30 daily — full data tarball
0 3 * * * docker run --rm -v fpulse_data:/data -v /backups:/backup alpine \
    tar czf /backup/fpulse-$(date +\%Y\%m\%d).tar.gz -C /data .

# 04:00 daily — master key copy
0 4 * * * cp ~/.fpulse/secret.key /backups/secret.key.$(date +\%Y\%m\%d)

# 05:00 weekly — push to remote
0 5 * * 0 aws s3 sync /backups/ s3://fpulse-backups/
```

Plus customers get this scheduled inside F-Pulse via the Backup page UI; OSS users set this up in their operating system's task scheduler — cron on Linux/macOS, Task Scheduler on Windows.

## Anti-patterns

- ❌ "Just `docker volume rm`" without first taking a backup — destructive.
- ❌ Storing the master key file inside the data tarball — defeats the purpose if a backup is exfiltrated. Keep the master key separate, with its own access controls.
- ❌ Assuming an Ollama model directory backup brings back F-Pulse — Ollama models can always be re-pulled. The data volume + master key are what matter.
- ❌ Running the agent's `compose_report` tool and assuming it captured every credential — reports redact secrets by design. They are not a backup mechanism.
