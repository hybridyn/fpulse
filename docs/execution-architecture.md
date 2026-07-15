# F-Pulse Execution Architecture — Enterprise Workload Design

## The Question

> We charge $100/month. The customer provides their own infrastructure (server/VM).
> F-Pulse must process ANY number of pipelines, ANY data volume, on ANY schedule.
>
> Real scenario:
> - Pipeline A: 1 GB every 3 hours
> - Pipeline B: 100 MB every 10 minutes
> - Pipeline C: 2 GB every 6 hours
> - 50+ more pipelines, mixed schedules
> - Many pipelines trigger at the same wall-clock time
> - Some finish in 5 seconds, some run for 3 hours
> - Downstream pipelines depend on upstream completions
> - Dependency chains can miss their scheduled windows
>
> A simple queue doesn't solve this. How does F-Pulse actually work?

---

## The Answer

### 1. Core Principle: Worker Pool, Not a Queue

A queue is serial — one job finishes, next one starts. That's useless for enterprise.

F-Pulse uses a **concurrent worker pool**:

```
                        ┌─────────────────────────────┐
                        │       SCHEDULER              │
                        │  (cron + dependency triggers) │
                        └─────────┬───────────────────┘
                                  │
                        ┌─────────▼───────────────────┐
                        │     EXECUTION DISPATCHER     │
                        │  • Priority sorting          │
                        │  • Resource check            │
                        │  • Slot allocation            │
                        └─────────┬───────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                    │
        ┌─────▼─────┐     ┌──────▼─────┐      ┌──────▼─────┐
        │  Worker 1  │     │  Worker 2  │ ...  │  Worker N  │
        │ Pipeline A │     │ Pipeline B │      │ Pipeline X │
        │ (process)  │     │ (process)  │      │ (process)  │
        └────────────┘     └────────────┘      └────────────┘
              │                   │                    │
              ▼                   ▼                    ▼
         Own memory          Own memory           Own memory
         Own CPU slice       Own CPU slice         Own CPU slice
         Own I/O channel     Own I/O channel       Own I/O channel
```

**Each pipeline runs in its own OS process.** Not a thread — a full process with its own memory space. This means:

- Pipeline A can't crash Pipeline B
- Pipeline A running for 3 hours doesn't block Pipeline B from starting
- 10 pipelines can run truly in parallel on a 10-core machine
- One pipeline's memory leak doesn't kill the server

**Worker count = configurable based on customer's hardware:**

| Customer Server | CPU | RAM | Recommended Workers | Concurrent Pipelines |
|-----------------|-----|-----|--------------------|--------------------|
| Small VM        | 4 cores | 8 GB | 4 workers | 4 simultaneous |
| Medium VM       | 8 cores | 16 GB | 8-12 workers | 8-12 simultaneous |
| Large VM        | 16 cores | 32 GB | 16-24 workers | 16-24 simultaneous |
| Beefy server    | 32 cores | 64 GB | 32-48 workers | 32-48 simultaneous |

The customer bought the hardware. F-Pulse uses ALL of it.

---

### 2. How Your Exact Scenario Runs

Let's walk through every pipeline you described, minute by minute:

```
TIME    EVENT                                    WORKERS IN USE
─────── ──────────────────────────────────────── ──────────────
00:00   Pipeline B starts (100MB, ~2 min)        [B] = 1/8
00:00   Pipeline A starts (1GB, ~15 min)         [B,A] = 2/8
00:00   Pipeline C starts (2GB, ~25 min)         [B,A,C] = 3/8
00:00   Pipelines D-H start (small, 30s each)    [B,A,C,D,E,F,G,H] = 8/8
00:00   Pipeline I ready → waits for free slot   [queued: I]
00:01   D finishes → slot freed → I starts       [B,A,C,E,F,G,H,I] = 8/8
00:01   E finishes → slot freed → J starts       [B,A,C,F,G,H,I,J] = 8/8
00:02   B finishes → slot freed                  7/8
...
00:10   Pipeline B triggers again (every 10min)  gets a free slot instantly
00:15   Pipeline A finishes                      slot freed
00:25   Pipeline C finishes                      slot freed
...
03:00   Pipeline A triggers again (every 3 hrs)
```

**Key insight:** Workers are REUSED the instant a pipeline finishes. There's no "wait for the whole batch". A 30-second pipeline frees its worker for the next job immediately.

---

### 3. What Happens When ALL Slots Are Full?

This WILL happen. 50+ pipelines, some running for hours, many triggering at midnight. Here's the design:

#### Priority-Based Admission

Every pipeline has a priority (P1-P5, configurable per pipeline):

```
P1 — Critical (SLA-bound, revenue-impacting)     → preempts P4/P5
P2 — High (business reporting, daily aggregates)  → queued ahead of P3-P5
P3 — Normal (default for all pipelines)           → fair scheduling
P4 — Low (backfills, exploratory, one-offs)       → runs when capacity exists
P5 — Background (housekeeping, archival)          → only runs when idle
```

When all 8 workers are busy and Pipeline X (P1) triggers:

```
Dispatcher logic:
  1. Any free worker? → Use it. Done.
  2. No free workers? → Check priority.
  3. Is any running pipeline lower priority than X?
     YES → Pause the lowest-priority pipeline, give slot to X.
     NO  → Queue X at the front. It runs the instant ANY worker frees.
```

**Pause, not kill.** The paused P5 pipeline resumes when capacity returns. No data loss.

#### Overflow Queue (NOT a serial queue)

The overflow queue is a **priority queue with parallel drain**:

```
Queue: [P1:X, P2:Y, P3:Z, P3:W, P4:V]
                    │
Worker 3 frees  ────┤───→ P1:X starts immediately
Worker 7 frees  ────┤───→ P2:Y starts immediately
Worker 1 frees  ────┘───→ P3:Z starts immediately
                          (Z and W don't wait for X or Y to finish)
```

Multiple workers drain the queue in parallel. The queue is only the holding area for the brief moment between "I'm ready" and "a worker is free." It's not a single-file line.

---

### 4. Streaming Execution — Not "Load Into Memory"

A 2 GB CSV pipeline does NOT load 2 GB into RAM. That would require 6-8 GB per worker (raw + parsed + transformed + output buffer). Instead:

#### Chunked Streaming Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  SOURCE NODE │     │ TRANSFORM    │     │   SINK NODE  │
│              │     │              │     │              │
│ Read 64MB    │────→│ Process 64MB │────→│ Write 64MB   │
│ Read 64MB    │────→│ Process 64MB │────→│ Write 64MB   │
│ Read 64MB    │────→│ Process 64MB │────→│ Write 64MB   │
│ ...          │     │ ...          │     │ ...          │
│ (32 chunks)  │     │ (32 chunks)  │     │ (32 chunks)  │
└──────────────┘     └──────────────┘     └──────────────┘

Peak memory: ~200 MB (3 chunks in flight)
NOT: 2 GB + 2 GB + 2 GB = 6 GB
```

**Chunk size is configurable** per pipeline: 16 MB for constrained environments, 256 MB for fast servers with lots of RAM.

This is how every serious data tool works (Spark, Flink, dbt, Airbyte). F-Pulse does the same at the node level.

#### Memory Budget Per Worker

```python
WORKER_MEMORY_BUDGET = total_ram / max_workers * 0.7

# Example: 16 GB RAM, 8 workers
# Budget per worker: 16 / 8 * 0.7 = 1.4 GB
# Chunk size auto-tuned to stay within budget
```

If a pipeline tries to exceed its budget (e.g., a SQL query returns an unexpected 5 GB result set), the engine:
1. Spills to disk (temp files in data directory)
2. Continues processing from disk
3. Logs a warning: "Pipeline X spilled 3.6 GB to disk — consider adding a LIMIT or filter"

**No OOM crash. No killed pipeline. Graceful degradation.**

---

### 5. Dependency Chains — The Hard Problem

Your concern is valid: Pipeline C depends on Pipeline A. Pipeline A was supposed to finish at 03:15 but ran until 03:45 because the source database was slow. Now Pipeline C's 03:30 schedule was missed.

#### Event-Driven Triggers Replace Time-Based Schedules

For dependent pipelines, **don't use cron schedules**. Use completion triggers:

```
Pipeline A (1 GB ingest)
    │
    ├── on_success ──→ Pipeline C (2 GB transform) starts IMMEDIATELY
    │                       │
    │                       └── on_success ──→ Pipeline D (aggregate)
    │
    └── on_failure ──→ Alert admin, DO NOT start C
```

This is a DAG (Directed Acyclic Graph) of pipelines — the same model used by Airflow, Dagster, Prefect, and every serious orchestrator.

**Pipeline C doesn't have a cron schedule at all.** It's triggered by Pipeline A completing. If A runs late, C runs late — but it ALWAYS runs with correct data. No stale reads, no missing windows.

#### Mixed Mode: Cron + Dependency

Real-world pipelines use both:

```
Pipeline B: cron("*/10 * * * *")              ← Pure time-based (independent)
Pipeline A: cron("0 */3 * * *")               ← Time-based (source ingest)
Pipeline C: depends_on(A) + cron("0 */6 * * *") ← Whichever fires LAST
Pipeline E: depends_on(C, D)                  ← Pure dependency (no cron)
```

For Pipeline C: `depends_on(A) + cron(every 6h)` means:
- C runs only when BOTH conditions are met: the 6-hour window has passed AND A has completed successfully since the last C run.
- If A is late, C waits. If A finished early, C waits for the 6-hour mark.
- The admin sees: "Pipeline C: waiting for dependency A (last success: 02:45, current run: in progress)"

#### SLA Tracking

Every pipeline can have an SLA:

```
Pipeline A:
  schedule: every 3 hours
  sla: must_complete_within: 30 minutes
  sla_breach_action: alert_admin + escalate_priority_to_P1
```

If Pipeline A normally takes 15 minutes but today it's been running for 25 minutes:
- At 25 min: WARNING — "Pipeline A at 83% of SLA window"
- At 30 min: SLA BREACH — alert fires, priority escalates, admin notified
- Pipeline keeps running (killing it would make things worse)

---

### 6. The Real Numbers — Capacity Planning

Let's size the exact scenario you described:

#### Workload Profile

| Pipeline | Data Volume | Schedule | Est. Duration | Daily Runs | Daily Data |
|----------|-----------|----------|--------------|-----------|-----------|
| A | 1 GB | Every 3h | 15 min | 8 | 8 GB |
| B | 100 MB | Every 10m | 2 min | 144 | 14.4 GB |
| C | 2 GB | Every 6h | 25 min | 4 | 8 GB |
| D-Z (23) | 50-500 MB avg | Various | 1-10 min | ~200 total | ~40 GB |
| AA-AZ (26) | 10-100 MB avg | Various | 30s-5 min | ~150 total | ~5 GB |
| **TOTAL** | | | | **~506 runs/day** | **~75.4 GB/day** |

#### Peak Concurrency Analysis

Worst case: midnight, when most daily schedules fire together.

```
00:00 triggers: A, C, and ~15 others = 17 pipelines
Within 00:00-00:10: B also triggers = 18 pipelines

But most of the 15 "others" finish in 1-5 minutes.
By 00:05, ~10 have finished, freeing workers for the queue.

Peak concurrent = ~18
Sustained concurrent (after 5 min) = ~8
```

#### Infrastructure Recommendation

```
MINIMUM (works, some queuing at peak):
  CPU: 8 cores
  RAM: 16 GB
  Disk: 500 GB SSD (for temp spill + data staging)
  Workers: 8
  Queue depth at midnight peak: ~10 jobs waiting ~2-3 min each
  Monthly cost: ~$80-120 (Azure/AWS VM)

COMFORTABLE (no queuing, headroom for growth):
  CPU: 16 cores
  RAM: 32 GB
  Disk: 1 TB SSD
  Workers: 16
  Queue depth at peak: 0-2 jobs, <30s wait
  Monthly cost: ~$200-300

OVERKILL (but future-proof for 200+ pipelines):
  CPU: 32 cores
  RAM: 64 GB
  Disk: 2 TB NVMe
  Workers: 32
  Monthly cost: ~$500-600
```

**The $100/month F-Pulse license is separate from infra cost.** Customer provides the VM. We provide the engine that uses every core they give us.

---

### 7. What If the Customer's Server Is Too Small?

F-Pulse detects this and tells the admin exactly what to do:

#### Resource Monitor (already built — `/api/system/resource-alerts`)

```json
{
  "violations": [
    {
      "resource": "cpu",
      "current": 94.2,
      "threshold": 90,
      "severity": "P1",
      "message": "CPU at 94.2% — 12 pipelines running, 7 queued. Consider upgrading to 16+ cores."
    },
    {
      "resource": "memory",
      "current": 91.0,
      "threshold": 85,
      "severity": "P1",
      "message": "Memory at 91% (14.5GB / 16GB) — 3 pipelines spilling to disk."
    }
  ],
  "running_pipelines": 12,
  "queued_pipelines": 7,
  "recommendation": "Current workload needs 16 cores / 32 GB. You have 8 cores / 16 GB."
}
```

The admin sees this on the Dashboard:

```
┌─────────────────────────────────────────────────────┐
│  ⚠  INFRASTRUCTURE ALERT                           │
│                                                     │
│  Your server is undersized for the current workload │
│                                                     │
│  Current: 8 cores / 16 GB RAM                       │
│  Needed:  16 cores / 32 GB RAM                      │
│                                                     │
│  Impact:                                            │
│  • 7 pipelines waiting in queue (avg wait: 4 min)   │
│  • 3 pipelines spilling to disk (2x slower)         │
│  • Pipeline B missed 2 scheduled windows today      │
│                                                     │
│  Options:                                           │
│  1. Upgrade VM to 16 cores / 32 GB                  │
│  2. Reduce concurrent worker count to 6             │
│  3. Stagger pipeline schedules (avoid midnight peak)│
│  4. Lower priority on non-critical pipelines        │
└─────────────────────────────────────────────────────┘
```

**F-Pulse never silently fails.** It tells you exactly what's happening and what to do about it.

---

### 8. The Complete Execution Engine — Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                    F-PULSE+ EXECUTION ENGINE                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                     SCHEDULER LAYER                       │   │
│  │                                                           │   │
│  │  Cron Engine ──────→ Time-based triggers                  │   │
│  │  DAG Engine ───────→ Dependency-based triggers            │   │
│  │  Event Engine ─────→ Webhook / file-watch / API triggers  │   │
│  │  SLA Monitor ──────→ Breach detection + escalation        │   │
│  └────────────────────────────┬──────────────────────────────┘   │
│                               │                                  │
│  ┌────────────────────────────▼──────────────────────────────┐   │
│  │                   DISPATCHER LAYER                         │   │
│  │                                                            │   │
│  │  Priority Queue ──→ P1 > P2 > P3 > P4 > P5               │   │
│  │  Resource Check ──→ CPU / RAM / Disk within budget?       │   │
│  │  Slot Allocator ──→ Assign to free worker or queue        │   │
│  │  Preemption ──────→ Pause P5 for incoming P1 if needed   │   │
│  │  Backpressure ────→ Reject new runs if critically full    │   │
│  └────────────────────────────┬──────────────────────────────┘   │
│                               │                                  │
│  ┌────────────────────────────▼──────────────────────────────┐   │
│  │                    WORKER POOL LAYER                        │   │
│  │                                                             │   │
│  │  Worker 1 ─── [Process] ─── Pipeline A (1 GB, chunked)    │   │
│  │  Worker 2 ─── [Process] ─── Pipeline B (100 MB, fast)     │   │
│  │  Worker 3 ─── [Process] ─── Pipeline C (2 GB, chunked)    │   │
│  │  Worker 4 ─── [Process] ─── Pipeline D (50 MB, quick)     │   │
│  │  ...                                                        │   │
│  │  Worker N ─── [Process] ─── Pipeline X                     │   │
│  │                                                             │   │
│  │  Each worker: own memory, own CPU slice, crash-isolated    │   │
│  └────────────────────────────┬──────────────────────────────┘   │
│                               │                                  │
│  ┌────────────────────────────▼──────────────────────────────┐   │
│  │                   STREAMING ENGINE                          │   │
│  │                                                             │   │
│  │  Source ──→ Chunk (64MB) ──→ Transform ──→ Sink            │   │
│  │  Source ──→ Chunk (64MB) ──→ Transform ──→ Sink            │   │
│  │  (pipeline: process chunks, not entire dataset)             │   │
│  │                                                             │   │
│  │  Memory budget per worker: total_ram / workers * 0.7       │   │
│  │  Overflow: spill to disk (SSD temp), continue processing   │   │
│  └────────────────────────────┬──────────────────────────────┘   │
│                               │                                  │
│  ┌────────────────────────────▼──────────────────────────────┐   │
│  │                   OBSERVABILITY LAYER                       │   │
│  │                                                             │   │
│  │  Per-pipeline: rows/sec, bytes/sec, memory, CPU, duration  │   │
│  │  Per-worker: utilization %, idle time, job count            │   │
│  │  System: queue depth, wait time p50/p95/p99, SLA status    │   │
│  │  Alerts: resource violations, SLA breaches, failures       │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

### 9. Comparison: How Other Tools Solve This Same Problem

| Feature | Airflow | Dagster | Prefect | **F-Pulse** |
|---------|---------|---------|---------|-------------|
| Worker model | Celery workers (separate infra) | Per-run process | Agents + work pools | **Built-in process pool** |
| Setup complexity | High (Redis + Celery + Flower) | Medium (daemon) | Medium (server + agent) | **Zero (single binary)** |
| Streaming/chunked | No (full dataset in memory) | No (asset-level) | No (task-level) | **Yes (node-level chunks)** |
| Priority preemption | No (FIFO pools) | No | Yes (work pools) | **Yes (P1-P5)** |
| Dependency triggers | Yes (DAG) | Yes (assets) | Yes (automations) | **Yes (DAG + event)** |
| SLA monitoring | Yes (basic) | Yes (freshness) | No | **Yes (per-pipeline)** |
| Infrastructure needed | 3+ services | 1 daemon + DB | 1 server + agent | **1 process** |
| Cost | Free + infra | Free + infra | $0-500/mo + infra | **$100/mo + infra** |

F-Pulse advantage: **everything runs in one process with zero external dependencies.** No Celery, no Redis queue, no Kubernetes, no separate scheduler service. The customer installs one thing.

---

### 10. FAQ — Edge Cases

**Q: What if a pipeline runs for 8 hours and blocks a worker the entire time?**

That's fine. That worker is dedicated to that pipeline. The other N-1 workers handle everything else. If you have 50 pipelines and 8 workers, the 8-hour pipeline takes 1 worker (12.5% capacity), leaving 7 workers for the other 49 pipelines. If your workload regularly has long-running pipelines, increase worker count.

**Q: What if ALL pipelines trigger at exactly midnight?**

The dispatcher starts the top-priority ones immediately (filling all workers), queues the rest by priority. On an 8-worker system with 50 midnight pipelines:
- 8 start immediately (P1 and P2 first)
- 42 queued, sorted by priority
- Most small pipelines finish in 1-5 minutes
- By 00:05, ~30 of the 42 queued jobs have already run
- By 00:15, all 50 have completed
- Admin recommendation: stagger schedules (00:00, 00:05, 00:10) to reduce peak queue

**Q: What if a pipeline fails mid-way through a 2 GB transfer?**

Checkpoint-based recovery:
- Each chunk writes a checkpoint after successful processing
- On retry, the pipeline resumes FROM THE LAST CHECKPOINT
- 2 GB file, failed at chunk 20/32 → retry starts at chunk 21, not chunk 1
- No duplicate data in the sink (idempotent writes with dedup keys)

**Q: What if the source database is slow and the pipeline takes 10x longer than expected?**

SLA monitor detects it:
1. At 80% of SLA: warning notification
2. At 100% of SLA: breach alert + priority escalation
3. Pipeline continues running (killing it wastes the work already done)
4. Dependent pipelines see "upstream running, waiting..." (not "failed")
5. Admin can manually cancel if needed

**Q: What if the disk fills up during processing?**

Disk pressure detector:
1. At 85%: warning, pause P4/P5 pipelines
2. At 90%: pause all new pipeline starts, alert admin
3. At 95%: force-complete running pipelines (flush buffers, close files)
4. Auto-cleanup: remove temp spill files older than 1 hour
5. Never corrupt data — always flush before stopping

**Q: What if the customer has 500 pipelines?**

Same architecture, bigger machine:
- 500 pipelines don't mean 500 concurrent runs
- If schedules are spread across 24 hours, peak concurrent might be 30-50
- A 32-core / 64 GB machine handles this comfortably
- For 500+ with high concurrency: recommend 2 F-Pulse instances behind a load balancer (shared database)

**Q: What about data consistency across dependent pipelines?**

Each pipeline execution gets a "logical timestamp" (execution_id + started_at):
- Pipeline A writes to `bronze/orders/batch_20260410_030000/`
- Pipeline C reads specifically from that batch path
- C never accidentally reads a half-written batch from the NEXT A run
- This is the Medallion architecture pattern (Bronze → Silver → Gold)

---

### 11. Business Model Clarity

```
┌──────────────────────────────────────────────────┐
│                                                  │
│   CUSTOMER PAYS:                                 │
│   ├── $100/month → F-Pulse license              │
│   │   (unlimited pipelines, unlimited data)       │
│   │                                              │
│   └── $80-600/month → Their own VM/server         │
│       (sized to their workload)                   │
│                                                  │
│   CUSTOMER GETS:                                 │
│   ├── Process-isolated parallel execution         │
│   ├── Priority-based scheduling (P1-P5)          │
│   ├── Dependency-aware DAG triggers              │
│   ├── Chunked streaming (low memory footprint)    │
│   ├── SLA monitoring + breach alerts             │
│   ├── Resource advisor (tells them when to scale)│
│   ├── RBAC, audit trail, PROD environment        │
│   └── No pipeline count limits, no data limits    │
│                                                  │
│   WE GUARANTEE:                                  │
│   ├── F-Pulse will USE whatever hardware they    │
│   │   give it — every core, every GB of RAM      │
│   ├── It will TELL THEM when their hardware is    │
│   │   undersized (not silently fail)             │
│   ├── It will PRIORITIZE correctly when capacity  │
│   │   is constrained (P1 before P5)              │
│   └── It will NEVER lose data (checkpoint,       │
│       idempotent writes, crash recovery)          │
│                                                  │
│   WE DO NOT GUARANTEE:                           │
│   └── That a 4-core VM can run 500 concurrent    │
│       pipelines. Physics still applies.           │
│       But we TELL THEM what they need.            │
│                                                  │
└──────────────────────────────────────────────────┘
```

---

### 12. Implementation Status in F-Pulse

| Component | Status | Location |
|-----------|--------|----------|
| Scheduler (cron-based) | BUILT | `fpulse/scheduling/scheduler.py` |
| Execution engine | BUILT | `fpulse/engine/executor.py` |
| Worker pool (process isolation) | DESIGN READY | Needs `ProcessPoolExecutor` integration |
| Priority queue (P1-P5) | DESIGN READY | Extend `ScheduleStore` with priority field |
| Dependency triggers (DAG) | DESIGN READY | Add `depends_on` to pipeline schema |
| Chunked streaming | PARTIAL | DuckDB already streams; need node-level chunking |
| Resource monitor | BUILT | `/api/system/resource-alerts` |
| SLA tracking | DESIGN READY | Add SLA fields to pipeline + monitor |
| Checkpoint recovery | DESIGN READY | Add checkpoint table to execution store |
| Disk pressure handler | DESIGN READY | Extend resource monitor |
| Infrastructure advisor | BUILT | Dashboard resource alerts |

**Current state:** The scheduler, executor, and resource monitor are built and running. The advanced features (worker pool, priority, DAG triggers, checkpoints) are architecturally designed and ready for implementation — they plug into the existing execution engine as extensions, not rewrites.

---

*Document version: April 10, 2026*
*Product: F-Pulse by Hybridyn*
