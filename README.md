# PC Crash Monitor

Windows hardware telemetry logger for diagnosing black screens, hard freezes, unexpected restarts, and gaming crashes.

## What it records

- CPU total/per-core utilization and clock speed
- CPU temperature, voltage, package power, and fan/pump readings when exposed
- RAM and pagefile usage
- NVIDIA GPU utilization, temperature, power, voltage, clocks, fan speed, VRAM usage, and PCIe link state
- Motherboard 12V, 5V, and 3.3V sensor readings when available
- Recent Windows Kernel-Power, WHEA, NVIDIA/display, and application errors
- A continuously flushed CSV and `last_sample.json`, preserving the final completed sample before a hard freeze

## Important PSU limitation

Software cannot normally read a power supply's true internal output. The app can record motherboard-reported voltage rails when the board exposes them. Those readings are useful clues but are not as accurate as a multimeter or dedicated PSU tester.

## Using the built EXE

1. Download the GitHub Actions artifact.
2. Extract `PC_Crash_Monitor_Windows.zip`.
3. For expanded motherboard, voltage, temperature, and fan coverage, run LibreHardwareMonitor as Administrator and leave it open.
4. Run `PC_Crash_Monitor.exe`.
5. Leave the interval at **1 second** and click **Start Logging**.
6. Minimize the app and play until the PC crashes.
7. After restarting Windows, open:

```text
Documents\PC_Crash_Monitor
```

8. Zip the newest `session_YYYYMMDD_HHMMSS` folder for analysis.

## Building through GitHub Actions

1. Create a new GitHub repository.
2. Upload the **contents** of this package to the repository root. After upload, `pc_crash_monitor.py`, `requirements.txt`, and `.github` must appear at the top level—not inside another folder.
3. Commit to `main` or `master`.
4. Open the repository's **Actions** tab.
5. Select **Build Windows EXE**.
6. Click **Run workflow**, or allow the push-triggered build to finish.
7. Open the completed run and download the **PC-Crash-Monitor-Windows** artifact.

The artifact contains:

- `PC_Crash_Monitor.exe`
- `PC_Crash_Monitor_Windows.zip`
- `README.md`

## Source setup for local testing

```bat
py -m pip install -r requirements.txt
py pc_crash_monitor.py
```

## LibreHardwareMonitor integration

The logger attempts to connect to LibreHardwareMonitor through its WMI namespace. To expose those sensors:

1. Download LibreHardwareMonitor from its official project page.
2. Run it as Administrator.
3. Keep it open while PC Crash Monitor logs.

Without LibreHardwareMonitor, NVIDIA readings and standard Windows CPU/RAM readings still work, but PSU rails, motherboard sensors, CPU package temperature, and pump RPM may be unavailable.
