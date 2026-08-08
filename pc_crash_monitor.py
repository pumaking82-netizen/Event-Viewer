"""
PC Crash Monitor v1.2
Windows hardware logger for black-screen / hard-freeze troubleshooting.

v1.2:
- NVIDIA telemetry via NVML first, nvidia-smi fallback.
- Sensor Status panel.
- Test Sensors button.
- LibreHardwareMonitor WMI diagnostics and retry.
- Logs every detected LHM sensor to sensor_diagnostics.json.
- Standardized CPU temp/power, rail voltage, AIO/fan, GPU hotspot extraction.
- Clean GitHub artifact packaging.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:
    tk = None


APP_NAME = "PC Crash Monitor"
VERSION = "1.2.0"
LOG_ROOT = Path.home() / "Documents" / "PC_Crash_Monitor"
STATE_FILE = LOG_ROOT / "last_session.json"


def now_local():
    return dt.datetime.now().astimezone()


def iso_now():
    return now_local().isoformat(timespec="milliseconds")


def sf(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def flatten(prefix, obj, out):
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flatten(key, v, out)
        else:
            out[key] = v


class NvidiaNVML:
    def __init__(self):
        self.ok = False
        self.error = None
        self.pynvml = None
        self.handle = None
        self._connect()

    def _connect(self):
        try:
            import pynvml
            self.pynvml = pynvml
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                raise RuntimeError("No NVIDIA GPU detected by NVML")
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.ok = True
            self.error = None
        except Exception as e:
            self.ok = False
            self.error = f"{type(e).__name__}: {e}"

    def available(self):
        return self.ok

    def sample(self):
        if not self.ok:
            return {"available": False, "source": "NVML", "error": self.error}

        n = self.pynvml
        h = self.handle
        out = {"available": True, "source": "NVML"}

        def grab(name, func, div=1.0):
            try:
                val = func()
                if isinstance(val, bytes):
                    val = val.decode(errors="replace")
                if isinstance(val, (int, float)):
                    val = val / div
                out[name] = val
            except Exception:
                out[name] = None

        grab("name", lambda: n.nvmlDeviceGetName(h))
        grab("driver_version", lambda: n.nvmlSystemGetDriverVersion())
        grab("temperature_gpu", lambda: n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
        grab("power_draw", lambda: n.nvmlDeviceGetPowerUsage(h), 1000.0)
        grab("power_limit", lambda: n.nvmlDeviceGetEnforcedPowerLimit(h), 1000.0)
        grab("fan_speed", lambda: n.nvmlDeviceGetFanSpeed(h))
        grab("clocks_current_graphics", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS))
        grab("clocks_current_memory", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_MEM))

        try:
            u = n.nvmlDeviceGetUtilizationRates(h)
            out["utilization_gpu"] = float(u.gpu)
            out["utilization_memory"] = float(u.memory)
        except Exception:
            out["utilization_gpu"] = None
            out["utilization_memory"] = None

        try:
            m = n.nvmlDeviceGetMemoryInfo(h)
            out["memory_used"] = m.used / 1024 / 1024
            out["memory_total"] = m.total / 1024 / 1024
        except Exception:
            out["memory_used"] = None
            out["memory_total"] = None

        try:
            out["pstate"] = str(n.nvmlDeviceGetPerformanceState(h))
        except Exception:
            out["pstate"] = None

        try:
            out["pcie_link_gen_current"] = float(n.nvmlDeviceGetCurrPcieLinkGeneration(h))
        except Exception:
            out["pcie_link_gen_current"] = None

        try:
            out["pcie_link_width_current"] = float(n.nvmlDeviceGetCurrPcieLinkWidth(h))
        except Exception:
            out["pcie_link_width_current"] = None

        # Some NVML versions expose graphics voltage, many do not.
        try:
            if hasattr(n, "nvmlDeviceGetVoltage"):
                out["voltage_graphics"] = float(n.nvmlDeviceGetVoltage(h)) / 1000.0
            else:
                out["voltage_graphics"] = None
        except Exception:
            out["voltage_graphics"] = None

        return out


class NvidiaSMI:
    SAFE_FIELDS = [
        "name",
        "driver_version",
        "utilization.gpu",
        "utilization.memory",
        "temperature.gpu",
        "power.draw",
        "power.limit",
        "clocks.current.graphics",
        "clocks.current.memory",
        "fan.speed",
        "memory.used",
        "memory.total",
        "pstate",
        "pcie.link.gen.current",
        "pcie.link.width.current",
    ]

    def __init__(self):
        self.exe = self._find()

    def _find(self):
        candidates = [
            shutil.which("nvidia-smi"),
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
        sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        store = sysroot / "System32" / "DriverStore" / "FileRepository"
        if store.exists():
            try:
                for p in store.glob("nv*/*nvidia-smi.exe"):
                    candidates.append(str(p))
            except Exception:
                pass

        for c in candidates:
            if c and Path(c).exists():
                return str(c)
        return None

    def available(self):
        return bool(self.exe)

    def sample(self):
        if not self.exe:
            return {"available": False, "source": "nvidia-smi", "error": "nvidia-smi not found"}

        try:
            cp = subprocess.run(
                [
                    self.exe,
                    "--query-gpu=" + ",".join(self.SAFE_FIELDS),
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if cp.returncode != 0:
                return {"available": True, "source": "nvidia-smi", "error": cp.stderr.strip()}

            vals = next(csv.reader([cp.stdout.strip().splitlines()[0]], skipinitialspace=True))
            out = {"available": True, "source": "nvidia-smi"}
            for f, v in zip(self.SAFE_FIELDS, vals):
                key = f.replace(".", "_")
                v = v.strip()
                if v in ("", "N/A", "[Not Supported]"):
                    out[key] = None
                elif f in ("name", "driver_version", "pstate"):
                    out[key] = v
                else:
                    out[key] = sf(v)
            return out
        except Exception as e:
            return {"available": True, "source": "nvidia-smi", "error": f"{type(e).__name__}: {e}"}


class NvidiaReader:
    def __init__(self):
        self.nvml = NvidiaNVML()
        self.smi = NvidiaSMI()

    def status(self):
        if self.nvml.available():
            return "Connected (NVML)"
        if self.smi.available():
            return "Connected (nvidia-smi fallback)"
        return "Not connected"

    def sample(self):
        if self.nvml.available():
            x = self.nvml.sample()
            if x.get("available") and not x.get("error"):
                return x
        return self.smi.sample()


class LibreHardwareMonitorReader:
    """
    LibreHardwareMonitor must be running as Administrator and its WMI namespace
    must be available at root\\LibreHardwareMonitor.
    """

    def __init__(self):
        self.conn = None
        self.error = None
        self._connect()

    def _connect(self):
        self.conn = None
        self.error = None
        if os.name != "nt":
            self.error = "Windows only"
            return

        try:
            import wmi
            self.conn = wmi.WMI(namespace=r"root\LibreHardwareMonitor")
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def retry(self):
        self._connect()
        return self.available()

    def available(self):
        return self.conn is not None

    def all_sensors(self):
        if not self.conn:
            return []

        sensors = []
        try:
            for s in self.conn.Sensor():
                sensors.append(
                    {
                        "name": str(getattr(s, "Name", "")),
                        "sensor_type": str(getattr(s, "SensorType", "")),
                        "value": sf(getattr(s, "Value", None)),
                        "identifier": str(getattr(s, "Identifier", "")),
                        "parent": str(getattr(s, "Parent", "")),
                    }
                )
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return []
        return sensors

    def sample(self):
        if not self.conn:
            return {"available": False, "error": self.error}

        out = {"available": True}
        try:
            for s in self.conn.Sensor():
                typ = str(getattr(s, "SensorType", "Unknown"))
                name = re.sub(
                    r"[^A-Za-z0-9_.()+\- ]+",
                    "",
                    str(getattr(s, "Name", "Unnamed")).strip(),
                )
                val = sf(getattr(s, "Value", None))
                if val is None:
                    continue

                key = f"{typ}.{name}"
                if key in out:
                    i = 2
                    while f"{key}_{i}" in out:
                        i += 1
                    key = f"{key}_{i}"
                out[key] = val
            return out
        except Exception as e:
            return {"available": True, "error": f"{type(e).__name__}: {e}"}


class WindowsEventCollector:
    def collect(self, path, hours=24):
        if os.name != "nt":
            return

        ps = f"""
$start=(Get-Date).AddHours(-{int(hours)})
Get-WinEvent -FilterHashtable @{{LogName=@('System','Application'); StartTime=$start}} -ErrorAction SilentlyContinue |
Where-Object {{
  $_.LevelDisplayName -in @('Critical','Error','Warning') -or
  $_.ProviderName -in @('Microsoft-Windows-Kernel-Power','Microsoft-Windows-WHEA-Logger','Display','nvlddmkm','Microsoft-Windows-WindowsErrorReporting') -or
  $_.Id -in @(41,18,19,20,4101,1000,1001,6008,14)
}} |
Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,LogName,Message |
ConvertTo-Json -Depth 4
"""
        try:
            cp = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = cp.stdout.strip() or json.dumps(
                {"note": "No matching events", "stderr": cp.stderr.strip()},
                indent=2,
            )
            path.write_text(text, encoding="utf-8", errors="replace")
        except Exception as e:
            path.write_text(json.dumps({"error": str(e)}, indent=2), encoding="utf-8")


class HardwareMonitor:
    def __init__(self, interval, base, q):
        self.interval = max(0.25, float(interval))
        self.base = base
        self.q = q
        self.stop_evt = threading.Event()

        self.nvidia = NvidiaReader()
        self.lhm = LibreHardwareMonitorReader()
        self.events = WindowsEventCollector()

        sid = now_local().strftime("%Y%m%d_%H%M%S")
        self.dir = base / f"session_{sid}"
        self.dir.mkdir(parents=True, exist_ok=True)

        self.csv_path = self.dir / "telemetry.csv"
        self.last_path = self.dir / "last_sample.json"
        self.info_path = self.dir / "session_info.json"
        self.diag_path = self.dir / "sensor_diagnostics.json"
        self.events_path = self.dir / "recent_windows_events.json"
        self.errors_path = self.dir / "logger_errors.txt"

        self.csv_file = None
        self.writer = None
        self.fields = None
        self.count = 0

    @staticmethod
    def extract_standard_lhm(x):
        out = {}

        patterns = {
            "cpu.package_temperature_c": [
                r"Temperature\..*CPU Package",
                r"Temperature\..*CPU \(Tctl/Tdie\)",
                r"Temperature\..*Tctl",
                r"Temperature\..*Tdie",
                r"Temperature\..*CPU Die",
            ],
            "cpu.package_power_w": [
                r"Power\..*CPU Package",
                r"Power\..*Package",
            ],
            "cpu.core_voltage_v": [
                r"Voltage\..*CPU Core",
                r"Voltage\..*Vcore",
                r"Voltage\..*Core \(SVI2 TFN\)",
            ],
            "gpu.hotspot_temperature_c": [
                r"Temperature\..*GPU Hot Spot",
                r"Temperature\..*Hot Spot",
            ],
            "gpu.memory_temperature_c": [
                r"Temperature\..*GPU Memory",
            ],
            "gpu.graphics_voltage_v": [
                r"Voltage\..*GPU Core",
                r"Voltage\..*GPU",
            ],
            "psu.12v_sensor_v": [
                r"Voltage\..*\+?12V",
            ],
            "psu.5v_sensor_v": [
                r"Voltage\..*\+?5V",
            ],
            "psu.3_3v_sensor_v": [
                r"Voltage\..*(\+?3\.3V|3VCC|AVCC)",
            ],
            "aio.pump_rpm": [
                r"Fan\..*(AIO|Pump|Water Pump)",
            ],
            "cpu.fan_rpm": [
                r"Fan\..*CPU",
            ],
            "motherboard.temperature_c": [
                r"Temperature\..*Motherboard",
            ],
        }

        for dst, regexes in patterns.items():
            for k, v in x.items():
                if k in ("available", "error"):
                    continue
                if any(re.search(rx, k, re.I) for rx in regexes):
                    out[dst] = v
                    break

        return out

    def diagnostics(self):
        diag = {
            "timestamp": iso_now(),
            "app_version": VERSION,
            "platform": platform.platform(),
            "python": sys.version,
            "psutil_available": psutil is not None,
            "nvidia_status": self.nvidia.status(),
            "nvml_error": self.nvidia.nvml.error,
            "nvidia_smi_path": self.nvidia.smi.exe,
            "librehardwaremonitor_connected": self.lhm.available(),
            "librehardwaremonitor_error": self.lhm.error,
            "nvidia_test_sample": self.nvidia.sample(),
            "librehardwaremonitor_sensors": self.lhm.all_sensors(),
        }
        self.diag_path.write_text(json.dumps(diag, indent=2, default=str), encoding="utf-8")
        return diag

    def sample(self):
        row = {
            "timestamp": iso_now(),
            "epoch": time.time(),
            "sample_number": self.count + 1,
        }

        if psutil:
            try:
                row["cpu.total_usage_percent"] = psutil.cpu_percent(None)
                for i, v in enumerate(psutil.cpu_percent(None, percpu=True)):
                    row[f"cpu.core_{i}_usage_percent"] = v

                fr = psutil.cpu_freq()
                if fr:
                    row["cpu.frequency_current_mhz"] = fr.current

                mem = psutil.virtual_memory()
                row["memory.usage_percent"] = mem.percent
                row["memory.used_mb"] = mem.used / 1024 / 1024
                row["memory.total_mb"] = mem.total / 1024 / 1024
            except Exception as e:
                row["psutil_error"] = str(e)

        gpu = self.nvidia.sample()
        flatten("nvidia", gpu, row)

        mappings = {
            "gpu.core_temperature_c": "nvidia.temperature_gpu",
            "gpu.core_load_percent": "nvidia.utilization_gpu",
            "gpu.memory_load_percent": "nvidia.utilization_memory",
            "gpu.power_w": "nvidia.power_draw",
            "gpu.power_limit_w": "nvidia.power_limit",
            "gpu.fan_percent": "nvidia.fan_speed",
            "gpu.core_clock_mhz": "nvidia.clocks_current_graphics",
            "gpu.memory_clock_mhz": "nvidia.clocks_current_memory",
            "gpu.memory_used_mb": "nvidia.memory_used",
            "gpu.memory_total_mb": "nvidia.memory_total",
            "gpu.pcie_generation": "nvidia.pcie_link_gen_current",
            "gpu.pcie_width": "nvidia.pcie_link_width_current",
            "gpu.graphics_voltage_v": "nvidia.voltage_graphics",
        }

        for dst, src in mappings.items():
            if row.get(src) is not None:
                row[dst] = row[src]

        if not self.lhm.available():
            self.lhm.retry()

        lhm_data = self.lhm.sample()
        flatten("lhm", lhm_data, row)
        row.update(self.extract_standard_lhm(lhm_data))

        warnings = []
        cpu_temp = sf(row.get("cpu.package_temperature_c"))
        gpu_temp = sf(row.get("gpu.core_temperature_c"))
        hotspot = sf(row.get("gpu.hotspot_temperature_c"))

        if cpu_temp is not None and cpu_temp >= 90:
            warnings.append(f"CPU_TEMP_HIGH:{cpu_temp:.1f}C")
        if gpu_temp is not None and gpu_temp >= 83:
            warnings.append(f"GPU_TEMP_HIGH:{gpu_temp:.1f}C")
        if hotspot is not None and hotspot >= 100:
            warnings.append(f"GPU_HOTSPOT_HIGH:{hotspot:.1f}C")

        for key, lo, hi, label in [
            ("psu.12v_sensor_v", 11.4, 12.6, "12V"),
            ("psu.5v_sensor_v", 4.75, 5.25, "5V"),
            ("psu.3_3v_sensor_v", 3.135, 3.465, "3.3V"),
        ]:
            v = sf(row.get(key))
            if v is not None and not lo <= v <= hi:
                warnings.append(f"{label}_OUT_OF_RANGE:{v:.3f}V")

        row["warnings"] = " | ".join(warnings)
        return row

    def write(self, row):
        if self.writer is None:
            self.fields = list(row.keys())
            self.csv_file = self.csv_path.open(
                "w",
                newline="",
                encoding="utf-8-sig",
                buffering=1,
            )
            self.writer = csv.DictWriter(
                self.csv_file,
                fieldnames=self.fields,
                extrasaction="ignore",
            )
            self.writer.writeheader()

        self.writer.writerow(row)
        self.csv_file.flush()
        os.fsync(self.csv_file.fileno())

        self.last_path.write_text(
            json.dumps(row, indent=2, default=str),
            encoding="utf-8",
        )

    def run(self):
        LOG_ROOT.mkdir(parents=True, exist_ok=True)

        info = {
            "state": "running",
            "version": VERSION,
            "session_dir": str(self.dir),
            "last_update": iso_now(),
        }
        self.info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        STATE_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")

        self.diagnostics()
        self.events.collect(self.events_path)

        if psutil:
            psutil.cpu_percent(None)
            psutil.cpu_percent(None, percpu=True)

        try:
            while not self.stop_evt.is_set():
                started = time.monotonic()
                try:
                    row = self.sample()
                    self.write(row)
                    self.count += 1
                    self.q.put(
                        {
                            "type": "sample",
                            "row": row,
                            "count": self.count,
                            "gpu_status": self.nvidia.status(),
                            "lhm_connected": self.lhm.available(),
                            "session_dir": str(self.dir),
                        }
                    )
                except Exception:
                    with self.errors_path.open("a", encoding="utf-8") as f:
                        f.write(f"[{iso_now()}]\n{traceback.format_exc()}\n")
                    self.q.put(
                        {
                            "type": "error",
                            "message": "Sampling error; see logger_errors.txt",
                        }
                    )

                elapsed = time.monotonic() - started
                self.stop_evt.wait(max(0, self.interval - elapsed))
        finally:
            if self.csv_file:
                try:
                    self.csv_file.flush()
                    os.fsync(self.csv_file.fileno())
                    self.csv_file.close()
                except Exception:
                    pass

            info.update(state="stopped", last_update=iso_now())
            self.info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
            STATE_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
            self.q.put({"type": "stopped", "session_dir": str(self.dir)})

    def stop(self):
        self.stop_evt.set()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("1000x730")
        root.minsize(900, 650)

        LOG_ROOT.mkdir(parents=True, exist_ok=True)

        self.q = queue.Queue()
        self.monitor = None
        self.worker = None

        self.interval_var = tk.StringVar(value="1.0")
        self.folder_var = tk.StringVar(value=str(LOG_ROOT))
        self.status_var = tk.StringVar(value="Ready")
        self.gpu_status_var = tk.StringVar(value="NVIDIA GPU: Not tested")
        self.lhm_status_var = tk.StringVar(value="Hardware Sensors: Not tested")
        self.values = {}

        self._build()
        root.after(250, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(outer, text="Logging Controls", padding=10)
        controls.pack(fill="x")

        ttk.Label(controls, text="Sample interval:").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.interval_var, width=8).grid(
            row=0, column=1, padx=(5, 20)
        )

        ttk.Label(controls, text="Log folder:").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.folder_var, width=55).grid(
            row=0, column=3, padx=5, sticky="ew"
        )
        ttk.Button(controls, text="Browse", command=self._browse).grid(row=0, column=4)

        self.start_btn = ttk.Button(controls, text="Start Logging", command=self._start)
        self.start_btn.grid(row=1, column=0, columnspan=2, pady=(8, 0), sticky="ew")

        self.stop_btn = ttk.Button(
            controls,
            text="Stop Logging",
            command=self._stop,
            state="disabled",
        )
        self.stop_btn.grid(row=1, column=2, pady=(8, 0), sticky="ew")

        ttk.Button(
            controls,
            text="Test Sensors",
            command=self._test_sensors,
        ).grid(row=1, column=3, pady=(8, 0), sticky="ew")

        ttk.Button(
            controls,
            text="Open Log Folder",
            command=self._open_logs,
        ).grid(row=1, column=4, pady=(8, 0), sticky="ew")

        controls.columnconfigure(3, weight=1)

        status_box = ttk.LabelFrame(outer, text="Sensor Status", padding=10)
        status_box.pack(fill="x", pady=(10, 0))
        ttk.Label(
            status_box,
            textvariable=self.gpu_status_var,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            status_box,
            textvariable=self.lhm_status_var,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            status_box,
            text="For CPU temperature, motherboard rails, AIO pump and fan RPM, run LibreHardwareMonitor as Administrator.",
            wraplength=930,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Label(
            outer,
            textvariable=self.status_var,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=10)

        live = ttk.LabelFrame(outer, text="Live Values", padding=10)
        live.pack(fill="both", expand=True)

        fields = [
            ("CPU Usage", "cpu.total_usage_percent", "%"),
            ("CPU Temperature", "cpu.package_temperature_c", "°C"),
            ("CPU Package Power", "cpu.package_power_w", "W"),
            ("CPU Frequency", "cpu.frequency_current_mhz", "MHz"),
            ("RAM Usage", "memory.usage_percent", "%"),
            ("GPU Usage", "gpu.core_load_percent", "%"),
            ("GPU Temperature", "gpu.core_temperature_c", "°C"),
            ("GPU Hotspot", "gpu.hotspot_temperature_c", "°C"),
            ("GPU Power", "gpu.power_w", "W"),
            ("GPU Voltage", "gpu.graphics_voltage_v", "V"),
            ("GPU Fan", "gpu.fan_percent", "%"),
            ("GPU Core Clock", "gpu.core_clock_mhz", "MHz"),
            ("GPU VRAM Used", "gpu.memory_used_mb", "MB"),
            ("PCIe Generation", "gpu.pcie_generation", ""),
            ("PCIe Width", "gpu.pcie_width", ""),
            ("12V Motherboard Sensor", "psu.12v_sensor_v", "V"),
            ("5V Motherboard Sensor", "psu.5v_sensor_v", "V"),
            ("3.3V Motherboard Sensor", "psu.3_3v_sensor_v", "V"),
            ("AIO Pump", "aio.pump_rpm", "RPM"),
            ("CPU Fan", "cpu.fan_rpm", "RPM"),
            ("Warnings", "warnings", ""),
        ]

        for i, (label, key, suffix) in enumerate(fields):
            row = i // 2
            col = (i % 2) * 2
            ttk.Label(live, text=label + ":").grid(
                row=row, column=col, sticky="w", padx=(0, 8), pady=4
            )
            var = tk.StringVar(value="—")
            self.values[key] = (var, suffix)
            ttk.Label(live, textvariable=var, font=("Consolas", 10, "bold")).grid(
                row=row, column=col + 1, sticky="w", pady=4
            )

        live.columnconfigure(1, weight=1)
        live.columnconfigure(3, weight=1)

        ttk.Label(
            outer,
            text="Every sample is flushed to telemetry.csv immediately. After a hard freeze, restart Windows and send the newest session folder.",
            wraplength=940,
        ).pack(fill="x", pady=(8, 0))

    def _browse(self):
        x = filedialog.askdirectory(initialdir=self.folder_var.get())
        if x:
            self.folder_var.set(x)

    def _new_monitor_for_test(self):
        base = Path(self.folder_var.get()).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        return HardwareMonitor(1.0, base, self.q)

    def _test_sensors(self):
        self.status_var.set("Testing sensors...")

        def work():
            mon = self._new_monitor_for_test()
            diag = mon.diagnostics()
            row = mon.sample()
            self.q.put(
                {
                    "type": "sensor_test",
                    "diag": diag,
                    "row": row,
                    "gpu_status": mon.nvidia.status(),
                    "lhm_connected": mon.lhm.available(),
                    "diag_path": str(mon.diag_path),
                }
            )

        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        if self.worker and self.worker.is_alive():
            return

        try:
            interval = float(self.interval_var.get())
            if not 0.25 <= interval <= 60:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid interval", "Use a value from 0.25 to 60 seconds.")
            return

        base = Path(self.folder_var.get()).expanduser()
        base.mkdir(parents=True, exist_ok=True)

        self.monitor = HardwareMonitor(interval, base, self.q)
        self.worker = threading.Thread(target=self.monitor.run, daemon=True)
        self.worker.start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Logging in progress — safe to minimize.")

    def _stop(self):
        if self.monitor:
            self.monitor.stop()

    def _open_logs(self):
        p = Path(self.folder_var.get()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    def _update_values(self, row):
        for key, (var, suffix) in self.values.items():
            val = row.get(key)
            if val is None or val == "":
                txt = "—"
            elif isinstance(val, float):
                txt = f"{val:.2f}"
            else:
                txt = str(val)

            if suffix and txt != "—":
                txt += " " + suffix
            var.set(txt)

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                typ = item.get("type")

                if typ == "sample":
                    self.gpu_status_var.set("NVIDIA GPU: " + item["gpu_status"])
                    self.lhm_status_var.set(
                        "Hardware Sensors: "
                        + ("Connected" if item["lhm_connected"] else "Not connected")
                    )
                    self.status_var.set(
                        f"Logging sample {item['count']:,} — {item['session_dir']}"
                    )
                    self._update_values(item["row"])

                elif typ == "sensor_test":
                    self.gpu_status_var.set("NVIDIA GPU: " + item["gpu_status"])
                    self.lhm_status_var.set(
                        "Hardware Sensors: "
                        + ("Connected" if item["lhm_connected"] else "Not connected")
                    )
                    self._update_values(item["row"])
                    self.status_var.set(
                        "Sensor test complete. Diagnostics saved to: " + item["diag_path"]
                    )

                elif typ == "stopped":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status_var.set("Logging stopped.")

                elif typ == "error":
                    self.status_var.set(item.get("message", "Logger error"))

        except queue.Empty:
            pass

        self.root.after(250, self._poll)

    def _close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Logger running",
                "Stop logging and close? Minimize instead during crash testing.",
            ):
                return
            self.monitor.stop()
        self.root.destroy()


def main():
    if tk is None:
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
