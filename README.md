# PC Crash Monitor v1.2

## What's new
- Sensor Status panel.
- Test Sensors button.
- NVIDIA GPU telemetry via NVML with nvidia-smi fallback.
- Automatic retry for LibreHardwareMonitor WMI.
- `sensor_diagnostics.json` now lists every hardware sensor LibreHardwareMonitor exposes.
- Improved CPU temperature, CPU package power, GPU hotspot/voltage, 12V/5V/3.3V and AIO/fan sensor matching.
- GitHub artifact no longer contains a nested ZIP/release folder.

## Best setup for your son's PC
1. Run LibreHardwareMonitor as Administrator.
2. Leave LibreHardwareMonitor open.
3. Run PC_Crash_Monitor.exe.
4. Click **Test Sensors**.
5. Confirm NVIDIA GPU says Connected.
6. If Hardware Sensors says Connected, CPU/motherboard/AIO readings should populate when the board exposes them.
7. Click Start Logging.
8. Play until the black-screen freeze happens.
9. Restart Windows.
10. Zip the newest folder under:
   `Documents\PC_Crash_Monitor`
11. Send that ZIP for analysis.

## PSU note
The app can log motherboard-reported 12V/5V/3.3V rails when available, but software cannot capture every sub-second PSU transient or directly verify a loose PCIe power connector.
