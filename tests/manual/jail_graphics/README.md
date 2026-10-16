This optional smoke test creates real Direct3D 11 and 12 devices through the
selected Proton build, reports the DXGI adapter name and vendor, and confirms
that the original probe files remain unchanged. It uses production jail mounts
and a fresh temporary game copy, prefix, home, and metadata store. Everything is
removed when the test finishes. It never launches an installed game or reuses an
existing game prefix.

Run on the Linux host with x86_64 GCC, GNU binutils (`ld` with `i386pep` support),
Klay's normal graphics dependencies, and a trusted installed Proton build:

```bash
python3 tests/manual/jail_graphics/run.py \
  "$HOME/.local/share/Steam/compatibilitytools.d/Proton-GE Latest" \
  --expected-vendor 0x10de
```

The vendor check is optional; `0x10de` selects NVIDIA, `0x1002` AMD, and `0x8086`
Intel. A successful JSON report has `ok: true`, both D3D12 results and the D3D11
result equal to `0x00000000`, and `source_untouched: true`. Failure includes a
bounded diagnostic tail. The default timeout is 150 seconds.

The tiny Windows executable is built locally from the C and assembly sources in
this directory using the host compiler and linker's PE target; it needs no
Windows SDK, MinGW installation, downloaded executable, or C runtime. Temporary
object files and the executable are never stored in the repository. There is no
option to weaken the jail's mounts for this test. Device creation does not test
frame rendering, presentation, audio, or a particular game's requirements.
