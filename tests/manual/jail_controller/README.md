# Windows controller probe

This builds the included trusted C source as a tiny Windows executable using
native x86_64 Linux GCC and GNU ld. It runs through Klay's production jail mount
and Proton paths in fresh temporary storage. No game, prefix, or save is opened.
The executable queries `XInputGetCapabilities` for connection and device type;
it never reads buttons, axes, or triggers and never sends vibration.

Connect the controller before running. Run on the native desktop host:

```sh
python3 tests/manual/jail_controller/run.py "/path/to/installed/Proton"
```

From a Toolbox/Distrobox checkout, forward the desktop display variables:

```sh
host-spawn -no-pty -env DISPLAY,WAYLAND_DISPLAY,XAUTHORITY,XDG_RUNTIME_DIR \
  /usr/bin/python3 /home/gshipley/Documents/Klay/tests/manual/jail_controller/run.py \
  "/path/to/installed/Proton"
```

`"ok": true` requires at least one connected Windows XInput controller and an
untouched source directory. A successful result confirms enumeration and XInput
translation, not physical button response or a particular game's input support.
Temporary game data and the prefix are discarded after the probe, including on
timeout. The probe does not change host device permissions or expose extra devices.
