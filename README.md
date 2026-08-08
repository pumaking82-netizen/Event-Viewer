# PC Crash Monitor v1.4

v1.4 uses LibreHardwareMonitor's native CSV logs instead of WMI or DLL integration.

## Setup
1. Run LibreHardwareMonitor as Administrator.
2. Turn on LibreHardwareMonitor logging.
3. Confirm `LibreHardwareMonitorLog-*.csv` files are being created.
4. Start PC Crash Monitor v1.4.
5. Click **Locate LHM Log Folder** and select the folder containing those CSVs.
6. Click **Test Sensors**.
7. The status should show `LibreHardwareMonitor CSV: Connected (...)`.
8. Click **Start Logging**, minimize it, and game.

v1.4 follows the newest LHM log automatically, so it continues working if LibreHardwareMonitor creates a new CSV file.

## What is merged
- CPU usage/frequency/RAM from Windows
- NVIDIA usage/temp/power/fan/clocks/VRAM/PCIe from NVML
- CPU package temperature/power/voltage from LHM when exposed
- GPU hotspot/voltage/fan RPM/LHM power
- Motherboard 12V/5V/3.3V sensors when exposed
- Pump/fan sensors when LHM names them
- Additional raw LHM sensor columns in the crash log
- Windows critical/error/WHEA/display events

## After a hard freeze
Restart Windows and send the newest folder under:
`Documents\PC_Crash_Monitor\session_*`

The logger flushes its merged telemetry row to disk every sample.

## PSU limitation
Motherboard voltage readings can help identify a large rail problem, but software cannot guarantee detection of a very short PSU transient between samples and cannot directly verify a loose PCIe power connector.
