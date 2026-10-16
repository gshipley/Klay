# Game jail

Klay's optional game jail runs a local Windows game in a restricted, offline
environment on Linux. You turn it on for each game with
`Run in a jail (offline)`. If Klay cannot set up the isolation, it stops the
launch.

## What changes when you turn it on

- **Klay copies the game.** The jailed game uses its own copy of the selected
  game folder. Your original installation is left untouched.
- **It starts with its own Windows environment.** Wine and Proton call this a
  *prefix*: the folders containing Windows settings, installed dependencies,
  and many games' saves. The jail creates a separate prefix instead of reusing
  the one from Lutris or another launcher. Existing settings, dependencies,
  and saves in that prefix are not copied over automatically.
- **It gets a private home folder.** The game sees the jail's home storage
  instead of your normal personal home folder.
- **It stays offline.** The game cannot connect to the internet, your local
  network, or online game services.

The copy, prefix, and private home are kept for later launches. Changes and
saves stay there; Klay does not synchronize them with the original installation.
Turning jail mode off restores the entry's normal launch command and keeps
the jail for reuse. **Deleting a jail also deletes the saves stored inside it.**
Back up saves you want to keep first.

## Start a jailed game

1. Check the [host requirements](#requirements) below. Klay needs Python 3.11
   or newer, Bubblewrap, and a trusted installed Wine or Proton runner.
2. Edit an imported Lutris Windows game and enable `Run in a jail (offline)`.
   Wait for detection to finish, then save with `OK`. Klay reads the installed
   game and runner settings and can resolve supported Windows `.lnk` shortcuts.
   You do not need to import the game again.
3. If detection reports a problem, open `Advanced settings` and choose the
   complete game folder, its `.exe`, and an installed runner. This is also how
   to set up other local Windows games; see [detailed setup](#set-up-a-game).
4. Select `Play`. The first launch copies the game and initializes its Windows
   environment. Allow extra disk space for the full game copy, prefix, and
   saves, and expect a longer wait for a large game. Later launches reuse them.
5. Use `View Jail Log` in game details or the game's right-click menu if a
   launch fails. `Open Jail Folder` opens its storage, including saves and logs.
   Use `Stop Jail` to stop a running jail if a game gets stuck.

**Preparation cannot currently be cancelled with `Stop Jail`.** The control
becomes available after preparation finishes. If you close Klay during
preparation, it waits for the operation to finish before closing.

## What the jail cannot promise

- **Every game working.** This is for local offline Windows games. Multiplayer,
  account sign-in, online DRM, cloud saves, and downloads can fail without a
  network. Anti-cheat, graphics drivers, runner compatibility, and extra Windows
  dependencies can also prevent a game from working. A game that works through
  Lutris may rely on settings or dependencies missing from the new prefix.
- **Every controller feature.** Standard Linux gamepads using evdev/SDL are
  supported when the desktop user can access them and they are connected before
  launch. Pair Bluetooth controllers on the desktop first. If a controller is
  newly connected or reconnects with different device nodes, relaunch the game.
  Raw HID features and Steam Input integration are not provided.
- **Complete separation from your computer.** The jail shares your Linux
  kernel, GPU, audio, and desktop services. X11 access can expose other desktop
  applications. It reduces exposure, but is not a virtual machine, a malware
  scanner, or a guarantee that a game is safe. Enabling it for a game does not
  restrict Klay itself or reduce Klay's existing launcher permissions.

Use a runner from a source you trust: Wine and Proton are programs that execute
on your computer too. The [logs and failures](#logs-and-failures) section covers
diagnostics and known runner issues.

This feature is inspired by the per-game isolation idea in WineQuarantine.
Klay uses its own Bubblewrap launcher; it does not run WineQuarantine's scripts
or malware monitor.

## Requirements

Install these on the Linux host using your distribution's supported method:

- Python 3.11 or newer at `/usr/bin/python3`.
- Bubblewrap, whose executable is `bwrap`.
- System Wine installed under `/usr`, a trusted Lutris Wine build, or a trusted
  Proton build installed as a directory.

The host must allow Bubblewrap to create the required namespaces. Klay checks
the dependencies and sandbox setup before running the game. It does not install
packages or download runners. In a Flatpak installation, the checks and jail
launcher run on the host through Klay's existing host-launch permission.

Use a runner from a source you trust. A runner is executable code, and the game
folder must not contain the runner. Klay invokes Proton's wrapper to initialize
DXVK/VKD3D and the private prefix against the host libraries. When bundled
media plugins need older libraries absent from the host, Klay can find those
libraries in an already installed Steam Linux Runtime. It mounts only the
required library files read-only, with separate 32-bit and 64-bit directories.
The host's loader, C/C++ runtime and graphics providers are retained. Klay does
not download a runtime or start Steam/UMU. This media compatibility layer is
not the full container environment recommended by
[GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom#overview);
other runtime dependencies can still affect compatibility.

## Set up a game

For an imported Lutris Windows game, open Edit Game and enable
`Run in a jail (offline)`. Klay reads the matching installed Lutris entry, its
game configuration, and its effective Wine runner settings. It fills the game
folder, executable, arguments, and runner automatically, then enables OK.
Existing entries work without another import. Native and Flatpak Lutris
installations are supported. Exact selected Proton builds can also be found in
external Steam libraries declared by Steam's `libraryfolders.vdf`.
Lutris's automatic `ge-proton` selection remains unsupported because UMU chooses
and may update that build at launch. Select a concrete installed Wine or Proton
version in Lutris, or choose its directory in Advanced settings.

Installed Windows `.lnk` shortcuts, including GOG launch shortcuts, are resolved
without executing them. Klay reads the exact local executable, working folder
and arguments; shortcut arguments precede any arguments configured in Lutris.
Standard Wine `C:` paths and installation-local `Z:` paths are supported, with
case-insensitive matching. Network targets, custom drive mappings, shortcut
chains, activation/elevation requests and ambiguous targets require choosing
the local game executable under Advanced settings. The parser follows the
[Windows Shell Link format](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-shllink/747629b3-b5be-452a-8101-b9a2ec49978c).

Klay uses Lutris's game folder when it contains the executable and is separate
from the Wine prefix, or the executable's directory otherwise. It preserves an
explicit working directory inside that folder. It copies the game files rather
than the whole Lutris Wine prefix. Lutris's prefix, host environment, overlays, and launch
scripts are not reused. The jail creates its own fresh prefix, so a game that
requires extra Windows dependencies may need those installed inside its jail.

For a packaged Unreal game launched from `Project/Binaries/Win64` or `Win32`,
Klay recognizes the installation's `Engine` and `Project/Content` folders and
copies the complete installation. It also recognizes up to three nested binary
folders such as `bin/x64` when the enclosing title is directly inside `Games`,
`GOG Games`, `Program Files`, `Program Files (x86)`, or `steamapps/common`, or
contains a sibling `Data`, `Content`, `Assets`, or `Resources` directory.
Shared library roots and Wine prefixes are never inferred as game folders.
An uncertain binary-folder layout requires choosing the whole game folder in
Advanced settings. Ancestor aliases such as `/home` to `/var/home` are supported;
links within the game remain prohibited.

Earlier jails that copied only a recognized binary
folder are expanded automatically. Their existing binary-folder copy is moved
intact into the complete layout, and their prefix, private home and saves are
retained. Interrupted expansion resumes on the next launch.

If the installed entry or runner cannot be identified, Klay shows the reason
and opens `Advanced settings`. It does not choose an unrelated runner or run
the Lutris command outside the jail. Existing saved jail settings and manual
overrides are retained.

Detected Proton builds use their `proton` wrapper. Previously saved settings
that selected that build's `files/bin/wine` or `dist/bin/wine` are corrected
automatically at launch. The existing jail prefix and saves are reused through
a private compatibility link; Klay does not move or copy mutable prefix files
on the host. Proton's normal prefix updates remain confined to the jail.

Automatic setup saves the detected settings with the game. Changing the paths
under Advanced settings overrides them; normal launches keep using the saved
settings and the managed game copy.

For manual setup:

1. Open Add Game, or edit an existing game's entry.
2. Enable `Run in a jail (offline)` and open `Advanced settings`.
3. Set `Game folder` to the folder containing the complete game.
4. Set `Game .exe` to its path relative to that folder, such as `game.exe` or
   `bin/game.exe`.
5. If needed, set `Working folder` to a path relative to the game folder.
   Leave it blank to use the executable's folder, or enter `.` for the game
   folder itself.
6. Select the `Runner type`. For manual Wine setup, leave `wine` to use the
   host system command or choose its absolute executable path, such as
   `/usr/bin/wine`; it must resolve under `/usr`. Automatic Lutris detection
   can also configure a specific trusted Lutris Wine build. For Proton, choose
   the trusted Proton build directory.
7. Enter optional `Game arguments`. Quoted text is parsed into arguments;
   commands and shell substitutions are not evaluated.
8. Save the entry and launch it normally.

The game folder and its contents must contain no symlinks. Copy the actual files
into an ordinary folder if the installation uses symlinks. Klay rejects paths
that leave the selected folder and other special files such as device files.

Jail mode overrides the ordinary `Executable` command while enabled. It
requires a local `.exe`. Lutris entries resolve those files automatically;
Steam URLs, Bottles launcher links, desktop commands, cloud games, and other
application launch routes still require manual local-file setup where applicable.

## Files and saves

Each game has its own directory under the host's
`~/.local/share/klay/jails/`. Klay derives the directory name from the game's
ID and keeps game files, the prefix, home storage, and logs there. Jail storage
uses this host location even if the app runs in Flatpak or has a different
`XDG_DATA_HOME`.

The first launch copies the selected game folder. Later launches use that
managed copy, including changes made by the game. Klay leaves the original
folder untouched. Once the copy is prepared, later launches do not require the
original folder or executable to remain available. Saves written beside the
executable stay in the managed game folder; saves written to the Windows user
folders stay in the jail's prefix.

Changing the source game folder does not automatically replace a previously
created copy; launch stops if the selected folder differs from the one used to
create that jail, apart from the recognized installation layout corrections above.
To refresh an installation, first stop the game, back up any
saves you need, then remove that game's managed jail directory. The next launch
makes a new copy and prefix. Removing the directory also removes its saved games.

Disabling jail mode returns to the entry's original launch command and keeps
the managed files. Re-enabling it resumes those files and saves. Changes and
saves are not automatically synchronized with the original installation.
Re-import preserves jail settings even if the source changes the title or a
removed game returns with the same ID.

## Isolation and limitations

The jail has no network access. It exposes a minimal operating-system view,
the selected runner, the managed game files, and only the display, audio, gamepad and GPU
resources needed to play, including read-only metadata for the selected GPUs.
NVIDIA initialization also receives its exact read-only `/sys/module/nvidia`
subtree. Sharing the UVM device adds `/sys/module/nvidia_uvm`, which is needed
for Direct3D 12 device initialization. Other module directories are not shared.
Wine also receives read-only snapshots of CPU online/present masks, processor
and cache topology, NUMA node masks, and the optional hybrid performance-core
mask. Only those metadata files are copied; CPU hotplug, power and frequency
controls are excluded. Each snapshot stays fixed for an active jail, and a
later launch picks up host topology changes. This lets Windows processor APIs
report the available hardware correctly without sharing the host's sysfs tree.
A synthetic per-game machine
ID is supplied instead of the host's machine ID.
X11 cookie authentication may retain the desktop's hostname inside the jail's
separate UTS namespace; it does not share the host's UTS namespace.
Each game has its own writable storage; Klay does not
mount your Steam folder or share a writable Wine/Proton runtime between games.

The sandbox still shares the host's Linux kernel and desktop services. X11
access can expose other X11 clients, and shared audio/display services are not
a separate desktop session. The feature reduces exposure; it does not certify
that a game is safe or provide the isolation of a virtual machine.

This version supports offline games. Multiplayer, downloads, online DRM,
account sign-in, and cloud saves can fail because networking is disabled.
Gamepads connected before launch are detected using the host's joystick
classification. Only accessible gamepad event/joystick devices and their exact
read-only device metadata are exposed. Keyboard, mouse, touch and raw HID nodes
are excluded. This supports standard evdev/SDL controllers, including the
tested 8BitDo Ultimate 2 through its xpad interface. Pair Bluetooth controllers
on the desktop before launch; the jail cannot access the host Bluetooth service.
New devices or reconnects that change device nodes require relaunching the game.
Controller-specific raw HID features and Steam Input integration are not provided.
GPU drivers, runner
dependencies, anti-cheat, and games with external services can also affect
compatibility.

Development checks have verified NVIDIA RTX 5090 OpenGL and Vulkan inside the
jail. A small source-built Windows probe also created Direct3D 11 and Direct3D
12 devices on that GPU. The selected GE-Proton11-3 wrapper initialized
DXVK/VKD3D, executed a benign Windows command, and preserved the private prefix
on repeat launch. Earlier
checks also covered GE-Proton10-34 and UMU-Proton-10.0-4. These checks do not
establish compatibility for every game or verify interactive gameplay and audio.
A source-built Windows CPU probe also verified all 32 logical processors and
working logical-processor, cache and NUMA queries in the production jail. Its
source and repeatable command are in `tests/manual/jail_cpu/`.

## Logs and failures

Games with jail mode enabled show a `Jail` badge. Choose `View Jail Log` from
game details or the game's menu for the latest status, exit code, error, and
log tail. `Refresh` updates the view while a game is running.
`Open Jail Folder` opens the managed storage so you can find saves and logs.

If a game window closes but its process remains alive, choose `Stop Jail` in
the game details or right-click menu, then launch it again. This stops only
that game's isolated processes and retains its files, prefix, and saves. The
control also works for a jail started by a previous Klay session. A stopped run
is recorded as `stopped`; an already idle jail is left alone.

After a game process exits normally, Klay allows a short grace period for
launcher handoffs, then cleans up leftover Wine and private D-Bus services.
Other active game processes keep the jail running.

If no application process is observed within ten minutes of starting the
runner, Klay stops that jail and records a startup timeout in its log. This
bounds a failed or very short launch whose Wine services otherwise linger.
Initial prefix setup shares this startup budget; an observed active game has
no such runtime limit. Stopping retains the managed files and saves.

Before launch, Klay runs the host's `glxinfo -B` and `vulkaninfo --summary` inside
the same isolation boundary when those optional utilities are installed. An
available OpenGL probe that fails stops launch before the game is copied or
executed. Vulkan results and renderer names are included in the launch log and
report; missing probe utilities are reported as unavailable. A successful check
does not prove that the game itself has initialized graphics.

To inspect an enabled game's jail and available graphics APIs without running
the game:

```bash
klay --check-jail GAME_ID
```

For this source checkout, use `./build-aux/dev run --check-jail GAME_ID`.

Some GE-Proton builds attempt to download optional upscaler components while
initializing. The jail blocks those requests. Such messages can appear in the
log even when the bundled DXVK/VKD3D components initialize successfully.

`Jail game processes exited; stopping remaining Wine services.` means Klay is
cleaning up after the application has exited. The reason for an unexpected exit
can be in the game's own log or crash dump under the jail's `prefix`, rather
than in the runner's output. For Unity games, look under
`drive_c/users/<Windows user>/AppData/LocalLow/<developer>/<game>/Player.log`.
Graphics checks passing does not rule out a runner or game crash later.

GE-Proton11-3 has a reported Media Foundation queue-shutdown race affecting
Unity video playback. A matching crash has exception `0xc0000008`
(`STATUS_INVALID_HANDLE`) in `rtworkq!serial_queue_finalization_callback`, as
described in [Wine MR11605](https://gitlab.winehq.org/wine/wine/-/merge_requests/11605).
For this specific failure, a different trusted installed Proton build can be
selected for that game under Advanced settings and may work around the crash.
Compatibility depends on the game and runner; a newer build is not a guaranteed
fix. Klay retains its jail storage;
the selected runner performs its usual private prefix updates. Runner choices
are saved separately for each game.

The report records `running`, `exited`, `stopped`, or `failed` and the process result. A
new jail can show `never_run`, preparation can show `starting`, and an
unexpectedly terminated supervisor can show `interrupted`. A successful exit
is a process result, not a safety verdict. The jail does not
include a malware scanner or a behavior monitor.

`klay --launch GAME_ID` waits for a jailed game to exit and returns its launch
result. Pressing `Ctrl+C` stops its jail supervisor and game. Ordinary launch
commands keep their usual detached behavior.

If a dependency is missing, a path is invalid, the game folder contains a
symlink, or sandbox setup fails, correct the reported problem and try again.
Klay never retries the game outside its jail. If you deliberately disable jail
mode, the original launch command becomes active again.
