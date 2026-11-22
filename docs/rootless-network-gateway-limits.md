# Gateway process resource limits

Each VM's gateway runs as two host processes: a Go **worker** that carries packets, and a Python **mediator** (mitmproxy) that parses and filters guest HTTP traffic. Both handle untrusted guest input, so each one caps its own resources at startup, and the Python **supervisor** in `sbx` watches the pair from outside.

This page records each limit, where it is set, what enforces it, and why it has that value. For policy, audit and recovery behavior, see the [gateway guide](rootless-network-gateway.md).

## Three layers

| Layer | Set in | Enforced by | Reaction when exceeded |
| --- | --- | --- | --- |
| Mediator self-limits | `gateway/mediator/hardening.py` (Python) | Linux kernel and glibc | The failing call errors or the process dies. |
| Worker self-limits | `gateway/internal/hardening/hardening_linux.go` (Go) | Linux kernel and the Go runtime | The failing call errors; the memory limit only makes the garbage collector work harder. |
| Supervisor ceilings | `src/sbx/gateway.py` (Python) | The supervisor, by polling `/proc` | The data plane is terminated and networking fails closed. |

The self-limits are instant but coarse. The supervisor ceilings measure what the design actually cares about, but only at polling intervals. The two layers are meant to overlap.

Self-limits apply on Linux. On macOS the mediator still sets its rlimits, but the worker applies none and neither process has seccomp or Landlock.

## Mediator limits

| Limit | Value | Reason |
| --- | --- | --- |
| Core dumps (`RLIMIT_CORE`) | 0 | The mediator holds keys and credentials in memory; a core file would write them to disk. |
| Open files (`RLIMIT_NOFILE`) | Half of `[network.quotas] open_files`; 2,048 by default | Reject new work before the host runs out of descriptors. The worker gets the other half. |
| File size (`RLIMIT_FSIZE`) | 100 MiB | Matches the 100 MiB active-log budget, so a guest cannot fill the host disk through log volume. |
| Address space (`RLIMIT_AS`) | 512 MiB | The only memory bound the kernel enforces instantly on the mediator. See [the address-space limit](#the-address-space-limit). |
| glibc arenas (`MALLOC_ARENA_MAX`) | 2 | Not a security limit. Stops glibc reserving up to 64 MiB of address space per thread. |
| Thread stack size | 1 MiB | Not a security limit. Stops each thread reserving the host's stack limit of address space. |

The mediator also sets no-new-privileges, marks itself non-dumpable, installs a seccomp filter that denies a short list of privileged system calls, and applies Landlock filesystem rules when the kernel supports them. A kernel without Landlock is reported as reduced hardening rather than treated as an error.

## Worker limits

| Limit | Value | Reason |
| --- | --- | --- |
| Core dumps (`RLIMIT_CORE`) | 0 | Same as the mediator. |
| Open files (`RLIMIT_NOFILE`) | The other half of `[network.quotas] open_files`; 2,048 by default | Same as the mediator. |
| File size (`RLIMIT_FSIZE`) | 100 MiB | Same as the mediator. |
| Go memory limit (`debug.SetMemoryLimit`) | 384 MiB | A soft target that makes the garbage collector reclaim earlier. It is not a hard cap. |

The worker has no address-space limit, so its hard memory bound is the supervisor ceiling below. It applies the same no-new-privileges, non-dumpable, seccomp and Landlock controls as the mediator; its Landlock rules also restrict outbound TCP ports.

## Supervisor ceilings

The supervisor sums usage across the worker and mediator process trees. Exceeding any ceiling terminates the data plane.

| Ceiling | Value | Reason |
| --- | --- | --- |
| Memory in use (RSS) | 512 MiB | The design's hard ceiling for the pair; the idle target is 256 MiB at p95. |
| Open descriptors | `[network.quotas] open_files`; 4,096 by default | Whole-pair counterpart of the per-process descriptor limits. |
| Processes | 16 | Neither process should spawn more than a few helpers. |
| Threads | 256 | Bounds runaway thread creation across the pair. |
| Log bytes | 100 MiB | Total size of `.log` files in the gateway state directory. |

`open_files` accepts 64 to 4,096 and is the only one of these values that is configurable.

## The address-space limit

The design's 512 MiB ceiling is about memory in use. `RLIMIT_AS` counts reserved address space, which is a larger number: a process can reserve far more than it touches. Two glibc defaults reserve heavily per thread and have each broken the mediator under this limit while it was using little real memory:

- **Allocator arenas.** glibc gives threads their own arenas, each reserving up to 64 MiB. `MALLOC_ARENA_MAX=2` caps this.
- **Thread stacks.** glibc sizes each new thread's stack from the host's `ulimit -s`, commonly 8 MiB and larger on some CI runners. At 16 MiB the mediator could not start 32 threads. Pinning the stack to 1 MiB makes the thread budget the same on every host.

With both pinned, the mediator reserves about 105 MiB after importing mitmproxy, and 32 threads add about 32 MiB. That leaves roughly 375 MiB for the design's buffer budgets, the largest being 64 MiB of hook bodies and 64 MiB of streaming queues.

The limit stays because the supervisor's memory check is a poll and a fast allocation burst could get past it.

### Choosing the stack size

1 MiB matches the default on Windows and the JVM. 8 MiB is usual only on Linux with glibc, where it is inherited from the shell rather than chosen. Raising it costs address space for every thread: at 8 MiB, 32 threads reserve 256 MiB and leave too little for the buffer budgets.

The trade-off is that very deep recursion inside C code could overflow a 1 MiB stack and crash the thread. Pure Python recursion hits the interpreter's recursion limit first. A mediator crash fails closed.

## Changing a limit

- Change mediator limits in `gateway/mediator/hardening.py`, then run `gateway/scripts/build_release_artifacts.sh` so the packaged copy under `src/sbx/gateway_runtime/` matches. CI rejects a mismatch.
- `gateway/mediator/test_hardening.py` starts 32 allocating threads under the unchanged address-space limit, with a 64 MiB host stack limit so the result does not depend on the machine.
- If another reservation-only failure appears, prefer raising `RLIMIT_AS` to a pure runaway backstop over adding a third glibc workaround. The supervisor's 512 MiB check remains the real ceiling.
