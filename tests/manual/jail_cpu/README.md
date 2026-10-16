This optional smoke test builds a trusted Windows executable from source and
checks `GetSystemInfo`, `GetActiveProcessorCount`, processor affinity, and
`GetLogicalProcessorInformation` through an installed Proton build. It uses the
production jail mounts and a fresh temporary game copy, prefix, home, and
metadata store. Everything is removed when the test finishes. It never launches
an installed game or reuses a game prefix.

Run on the Linux host with x86_64 GCC, GNU binutils (`ld` with `i386pep` support),
Klay's normal jail dependencies, and a trusted installed Proton build:

```bash
python3 tests/manual/jail_cpu/run.py \
  "$HOME/.local/share/Steam/compatibilitytools.d/Proton-GE Latest"
```

A successful JSON report has `ok: true`, both Windows processor counts equal to
the host count, successful logical processor enumeration, matching logical and
system masks, and `source_untouched: true`. The report also lists physical cores,
caches, NUMA nodes, and packages. Use `--expected-count N` when the selected Wine
build has a known processor-group limit; this changes only the assertion, never
the topology or process affinity. The default timeout is 150 seconds.

The executable needs no Windows SDK, MinGW installation, downloaded executable,
or C runtime. It reuses the adjacent graphics fixture's import table. Temporary
objects and executables are never stored in the repository. There is no option
to relax the jail's mounts or force a CPU count.

Wine's [logical processor enumeration source](https://github.com/ValveSoftware/wine/blob/proton_11.0/dlls/ntdll/unix/system.c)
reads CPU `online`/`present`, per-CPU topology and cache metadata, NUMA `online`
and per-node `cpumap`, plus the optional Intel `cpu_core/cpus` mask. Missing
`cpu/online` makes enumeration fail even when `GetSystemInfo` and scheduler
affinity still report the host CPUs. These checks establish processor API
behavior; they do not establish the cause of a particular game crash.
