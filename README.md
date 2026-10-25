# Custom Fan Controller

Dual-fan control for a ThinkPad on Windows: slider-based temperature curves, a
tray icon, and failsafes that hand the fans back to the BIOS when anything
looks wrong.

> # ⚠️ Tested on ONE laptop. Not a universal tool.
>
> This program writes to your laptop's **embedded controller (EC)** through a
> kernel driver. The register addresses it uses were verified on one machine
> only. On any other model it may do nothing, may report wrong temperatures or
> RPM, or may set fan levels you did not intend. It comes with **no warranty**
> (see `LICENSE`). You use it at your own risk.
>
> | | |
> |---|---|
> | **Laptop** | Lenovo ThinkPad P1 Gen 6 (machine type 21FW) |
> | **BIOS** | N3ZET57W (1.44), dated 27 Aug 2026 (as the Windows registry reports it) |
> | **Fans** | 2 (both controlled and read back) |
> | **OS** | Windows 11 Pro, build 26300 |
> | **Driver** | [PawnIO](https://github.com/namazso/PawnIO) with the `LpcACPIEC` module (access limited to EC ports `0x62`/`0x66`) |
> | **Version tested** | 1.4.0. The window is new; the EC access layer is unchanged from 1.3.0. Run on this laptop under game load, overnight with several standby cycles, and through lid-close and sleep tests. See [Known limitations](#known-limitations-140). |
>
> **Verified on that machine (1.3.0):** reading both fans' level and RPM; setting levels
> 0–7 and handing back to EC auto, with read-back; sustained game load; start
> at logon from a scheduled task; one sleep/wake cycle (fan register survived
> it); running on battery; pausing while HWiNFO is open and resuming when it
> closes; several days of daily use.
>
> **Not tested:** any other model or generation, single-fan ThinkPads, Windows
> 10, hibernate, docking, uptimes of weeks, any BIOS other than the one used
> in development.
>
> **If your laptop is different:**
> 1. Run with `--readonly` first. It reads everything and never writes.
> 2. Check that its temperatures and RPM are believable against an independent
>    source that is not reading the EC at the same moment (the BIOS hardware
>    monitor, or HWiNFO with its EC support turned off; this program pauses
>    itself while HWiNFO is reading the EC). If they disagree, **stop**: this
>    register map is not valid for your machine.
> 3. Only then enable control, and watch it hold sensible fan levels under
>    load before you leave it unattended.
> 4. Please open an issue with your model, BIOS version and the `--readonly`
>    output, whether it worked or not. That is how this list grows.
>
> Not affiliated with or endorsed by Lenovo.

## About this project

**Who wrote it.** The code was written by Claude (Anthropic) in a long working
session with the repository owner, who did not write it. The owner set the
goals (both fans visible and controllable, slider-based curves, a tray icon
that stays on the taskbar, no hangs at boot), ran every build on their own
laptop, reported exactly what they saw, and found problems the tests could not.
The tests in this repository run against a simulated EC; they cannot show that
real hardware behaves the same way.

**What testing on the real laptop caught:**

- Sensors with no data read as `0x80`, which some tools show as 128 °C. This
  program treats that as "missing", never as a temperature.
- Fan RPM of 37,008 appeared when HWiNFO and this program read the EC at the
  same time. Reads that implausible are now rejected, and the program pauses
  while HWiNFO is open.
- A stale byte left in the EC's output buffer by Windows was being read as an
  answer. The EC handshake now follows TPFanCtrl2's and waits for that buffer
  to be empty.
- The RPM at each fan level, measured from `cycles.csv`, differed from what
  was first assumed, so tuning is done from the logs, not from guesses.

**Relation to TPFanCtrl2.** This is not a criticism of
[TPFanCtrl2](https://github.com/Tinnci/TPFanCtrl2). Its fan control worked on
this laptop, its EC handshake is the reference this program follows, and it
supplies the PawnIO module this program loads. A different front end was
built because the owner wanted sliders instead of editing curves in a file,
both fans' RPM always on screen, a tray icon pinned to the taskbar, and
fallback rules that could be read and tested.

## Known limitations (1.4.0)

These are things observed on one laptop. None of them is explained.

- **Standby.** The program does not run while Windows is in Modern
  Standby (lid close, Start > Sleep and screen-off timeouts all enter it).
  In my tests, with the fans at a high level before standby, they kept
  running through it, judged by ear (RPM was not logged). Each test lasted
  one to two minutes, so I do not know how long they would hold that level.
  **Exit the program from the tray before closing the lid or leaving the
  laptop with a heavy load running.** Exiting hands both fans back to the
  EC. Handing them back automatically when the display turns off is
  planned, not built.
- **After waking.** Once, fan 1 was found at level 0 when it should have
  been 3, about 25 seconds after the first reading following a wake. The
  program noticed on its next check and re-applied the level. I do not know
  what changed it. Lenovo Vantage was running, but I did not isolate it.
- **Occasional bad fan readings.** In one session, fan 2 twice reported
  about 36,900 RPM at a valid level. The sanity check rejected both and the
  next reading was normal. The cause is not known.
- **Defaults.** The shipped defaults are hand-over at 92 °C, take back
  at 85 °C and a 15 s step-down delay. I run 97/90/0 on my own laptop.
- **Not tested:** other models or BIOS versions, and standby with the fans
  running for longer than about two minutes.

## What it does

- **A Task Manager style window** (dark, no gridlines, smooth graphs), with a
  side bar:

  | Page | What it shows |
  |---|---|
  | Overview | CPU, GPU, both fans and controller state with 5-minute sparklines; temperature and fan-speed history (5 min / 30 min / 2 h); time spent at each fan level; statistics |
  | Fan curves | Each curve as a chart, with a marker for where the laptop is right now, the temperature the fans step *down* at, and the hand-over temperature. The sliders sit underneath and the chart follows them as you drag |
  | Sensors | Every EC temperature sensor: now, lowest and highest since start, what it is used for, and whether it is ignored |
  | Events | The program's log, filterable (all / changes / problems), with **Copy report** and **Open log folder** |
  | Settings | Deadband, delay, hand-over and take-back temperatures, stall alarm, HWiNFO option |

  The graphs are drawn slightly behind real time so the lines glide between
  readings instead of stepping, and nothing extra is read from the EC to do
  it. They only redraw while the window is on screen.
- **Copy report** (Events page) puts a text report on the clipboard: program
  version, laptop model and BIOS, whether the EC module matches the tested
  one, your settings, current readings, a few statistics and the newest log
  lines. Your user name is removed from paths. Read it before posting it in an
  issue.
- **Two curves.** A CPU curve (EC sensor `0x78`) and a GPU curve (`0x7B`).
  Both fans get whichever level is higher.
- **Sliders** set the temperature at which each fan level engages. Fans step
  up immediately. They step down once the temperature has fallen a set
  "deadband" below the step, and (optionally) stayed there for a step-down
  delay, so short dips don't make them cycle.
- **Tray dot colours:**

  | Colour | Meaning |
  |---|---|
  | Green | Your curve is running the fans |
  | Blue | EC Auto (BIOS curve) is selected |
  | Orange | The EC has temporarily taken over (hot, the CPU sensor dropped out, or paused for HWiNFO) |
  | Red | A fan isn't spinning when commanded, or the EC stopped responding |
  | Grey | The control loop isn't responding |
  | Purple | Read-only test mode |

## Requirements and setup

You need: Windows 11, Python 3 (for the script or for building the exe), an
administrator account (EC access requires it), and the PawnIO driver.

1. **Install the PawnIO driver:** `winget install namazso.PawnIO`
2. **Get the `LpcACPIEC.bin` module** and put it in this folder. It is **not
   included in this repository**, because I could not confirm its licence for
   redistribution. It is the same file shipped with
   [TPFanCtrl2 v2.8.2](https://github.com/Tinnci/TPFanCtrl2/releases). Check
   it before use:

   ```powershell
   (Get-FileHash .\LpcACPIEC.bin -Algorithm SHA256).Hash
   # expected: C38FD116E7AFF4D1FDB0A494E296BE0A6708E5A22FC72F14587442FB7F8F7906
   ```
   The program also checks this hash on startup and logs a warning if it
   differs. A file you cannot account for is a file you should not load into a
   kernel driver.
3. **Install the Python packages:** `py -3 -m pip install -r requirements.txt`
4. **Verify before you trust it.** Do these in order and **stop at the first
   one that doesn't look right**. Steps c and d need an *administrator*
   PowerShell in this folder.

   | Step | Command | What you should see |
   |---|---|---|
   | a. Tests (no hardware) | `py -3 -m unittest discover -s tests` | `OK` |
   | b. Simulator (no hardware) | `py -3 CustomFanController.pyw --show --simulate` | The window opens and the simulated fans respond |
   | c. Read-only, on your laptop | `py -3 CustomFanController.pyw --show --readonly` | Purple icon. Temperatures and both fans' RPM are believable (see below). No `EC error` lines in `logs\CustomFanController.log` |
   | d. Live, watched | `py -3 CustomFanController.pyw --show` | Green icon. Under load the fans follow your curve and the read-back matches. Choosing **Exit** in the tray returns the fans to the BIOS |

   "Believable" in step c means: the CPU temperature agrees closely with a
   source that is not reading the EC at the same moment (the BIOS hardware
   monitor, or HWiNFO with its EC support turned off), fan RPM is 0 when a fan
   is off, and neither value jumps around or reads something impossible. Any
   mismatch means this register map is not valid for your machine: **stop and
   do not continue to step d.**

5. **Only after step d** should you build the exe or set up autostart (below).
   Both are conveniences. Neither is needed to use the program.

Exit from the tray menu. Both fans are handed back to the BIOS when the
program exits.

### Building the exe

**There is deliberately no prebuilt `.exe`.** This program runs as administrator
and talks to a kernel driver, so you should be able to read what you are
running, and a binary from a stranger is the opposite of that. Unsigned
PyInstaller executables are also often flagged by antivirus products. Build it
yourself, after step d above:

```powershell
pwsh -ExecutionPolicy Bypass -File .\build.ps1
```

This produces `dist\CustomFanController\CustomFanController.exe` and copies
`LpcACPIEC.bin` next to it.

### Starting at logon (optional)

Run from an administrator PowerShell, adjusting `$dir`. The 60 s delay lets
Windows finish its own EC activity at boot first.

```powershell
$dir = "C:\Tools\CustomFanController\dist\CustomFanController"
$a = New-ScheduledTaskAction -Execute "$dir\CustomFanController.exe" -WorkingDirectory $dir
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t.Delay = "PT60S"
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName "CustomFanController" -Action $a -Trigger $t -Principal $p -Settings $s
```

Don't run it alongside another EC fan tool (TPFanCtrl2, TPFanControl, ...).
It refuses to start if it finds one.

## Keeping the icon on the taskbar

On its first run as the built exe, the app pins its tray icon so it sits next
to the clock instead of in the ^ overflow. It does this once per program. If
you later hide it in Windows Settings, it won't fight you. This needs
Windows 11 22H2 or newer.

Windows remembers tray icons **per program**, so build the exe:

| How you run it | What Windows sees |
|---|---|
| `CustomFanController.exe` (built with `build.ps1`) | Listed as "Custom Fan Controller" with its own pin setting |
| `CustomFanController.pyw` (script, for testing) | Listed as Python. The app does **not** auto-pin here, because that would pin every Python tray icon |

To pin or unpin it manually: **Settings → Personalization → Taskbar → Other
system tray icons**, or drag the icon out of the ^ overflow onto the taskbar.

## Safety

The only EC registers this program ever **writes** are the fan selector
(`0x31`) and the fan level (`0x2F`). Everything else is read-only.

Both fans are handed back to the EC (`0x80`) when any of these happen:

- you pick EC Auto;
- any monitored sensor reaches the hand-over temperature (set on the Settings
  page; control resumes at the lower "take back" temperature);
- the CPU sensor stops reporting;
- 5 consecutive EC errors;
- an unexpected error;
- the program exits.

Other protections:

- Only levels 0–7 and `0x80` can ever be written. Disengaged mode (`0x40`,
  which removes the regulation limit) is refused.
- The EC handshake follows TPFanCtrl2 v2.8.2 step for step. In particular it
  waits until the EC's output buffer is empty before starting, so a stale
  byte left there by Windows' own ACPI driver is never mistaken for an answer.
- Every write is select-fan → write → read-back, done while holding the
  system `Access_EC` lock. The fan selector is left on fan 1 afterwards.
- Ignored sensors aren't read at all, so there's less EC traffic to collide
  with.
- Each cycle reads both fans back. If the EC changed a fan (for example after
  sleep on some machines), the level is re-applied and logged.
- It refuses to start while another fan controller is running, or if a copy
  of itself is already running.
- **HWiNFO:** HWiNFO reads the EC without the shared lock, so its reads
  interleave with this app's and corrupt both (seen in testing: fan 2 read as
  level `0x90` / 37008 RPM). While HWiNFO is open, the app hands the fans to
  the EC, stops polling, and shows an orange "Paused" status. It resumes on
  its own when HWiNFO closes. If you turn off EC support in HWiNFO's
  settings, tick "Keep running while HWiNFO is open" on the Settings page.
- **Corrupted reads are rejected, not believed.** A fan reading above 8,000
  RPM counts as an EC error. A temperature that jumps more than 15 °C in one
  read is only accepted if the next read agrees.

## Files

| File | Purpose |
|---|---|
| `CustomFanController.pyw` | Run as a script (testing) |
| `requirements.txt` | Python packages (`pystray`, `pillow`) |
| `customfan/` | The program. `service.py`/`control.py`/`thinkpad.py`/`ec.py`/`pawnio.py` talk to the hardware; `gui.py`, `pages.py`, `dashboard.py`, `widgets.py`, `theme.py` are the window; `history.py`, `events.py`, `report.py` hold the data behind it and have no Tk or hardware in them |
| `build.ps1` | Builds `dist\CustomFanController\CustomFanController.exe` (daily use) |
| `LpcACPIEC.bin` | **You supply this** (see Setup). Not in the repository |
| `config.json` | Your settings. Created next to whichever you run, the .pyw or the .exe |
| `config.simulate.json` | Settings used by `--simulate`, kept separate so experiments never change your real curve |
| `logs\CustomFanController.log` | Mode changes, level changes, safety events and errors |
| `logs\cycles.csv` | One row per cycle: temperatures, requested levels, both fans' levels and RPM. Open in Excel to tune the curve from real data. Capped at about 2 MB |
| `tests\` | Runs against a simulated EC; never touches hardware |

Run the tests (any OS, no hardware needed): `python -m unittest discover -s tests -v`

## Command-line options

These work for both the .pyw and the .exe.

| Option | Effect |
|---|---|
| `--show` | Open the window at start (default is tray only) |
| `--readonly` | Read everything, never write. Use for the first hardware test |
| `--simulate` | No hardware at all |
| `--debug` | Verbose log |

## Licence and contributing

MIT, see [`LICENSE`](LICENSE). It covers the code in this repository only. It
does not cover [PawnIO](https://github.com/namazso/PawnIO) or the `LpcACPIEC`
module, which are not included here and have their own terms.

Issues are welcome, especially reports from other ThinkPad models (see the
warning at the top). Pull requests are not being accepted for now.

## Acknowledgements

- [PawnIO](https://github.com/namazso/PawnIO) by namazso: the signed driver
  that makes port access possible without an unsigned or blocklisted one.
- [TPFanCtrl2](https://github.com/Tinnci/TPFanCtrl2) (Unlicense, per its
  README), whose EC handshake this program follows and whose releases supply
  the `LpcACPIEC` module.
