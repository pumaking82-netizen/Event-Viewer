# PC Crash Monitor v1.3

## Major change
v1.3 no longer depends on LibreHardwareMonitor WMI as the primary interface.

It loads the official `LibreHardwareMonitorLib.dll` directly using pythonnet and reads the same hardware sensor library LibreHardwareMonitor itself uses. LibreHardwareMonitor provides a library specifically for application integration. Administrator privileges are requested by the EXE because some sensors require them.

## How to use
1. Extract the official LibreHardwareMonitor ZIP.
2. Run LibreHardwareMonitor.exe as Administrator.
3. Run PC_Crash_Monitor.exe.
4. Click Test Sensors.
5. If Hardware Sensors says Direct DLL, you're ready.
6. If it says Not connected, click Locate LibreHardwareMonitor Folder and select the folder containing both LibreHardwareMonitor.exe and LibreHardwareMonitorLib.dll.
7. Click Start Logging and game until the crash.
8. After restart, send the newest Documents\PC_Crash_Monitor\session_* folder.

The sensor diagnostics file records every hardware sensor detected and its exact name.
