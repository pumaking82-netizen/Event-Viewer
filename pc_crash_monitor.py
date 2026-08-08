"""
PC Crash Monitor
Windows-focused hardware logger for troubleshooting black-screen / hard-freeze crashes.

What it logs:
- CPU total and per-core usage, frequency, RAM/pagefile usage
- NVIDIA GPU utilization, temperatures, clocks, fan, voltage, power draw, PCIe state
- Motherboard/CPU/GPU sensors exposed by LibreHardwareMonitor (optional)
- 12V / 5V / 3.3V rails if the motherboard exposes them
- Windows event-log entries from the previous boot after the PC restarts
- A continuously flushed CSV so the last completed sample survives a hard freeze

Important:
Software cannot measure the PSU's true output unless the motherboard exposes rail sensors.
Motherboard 12V/5V/3.3V readings are useful clues, but a multimeter or PSU tester is more accurate.
"""

from __future__ import annotations

import csv
import ctypes
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
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:
    tk = None

APP_NAME = "PC Crash Monitor"
VERSION = "1.1.0"
DEFAULT_INTERVAL = 1.0
LOG_ROOT = Path.home() / "Documents" / "PC_Crash_Monitor"
STATE_FILE = LOG_ROOT / "last_session.json"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="milliseconds")


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def flatten_dict(prefix: str, obj: Dict[str, Any], out: Dict[str, Any]) -> None:
    for key, value in obj.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flatten_dict(name, value, out)
        else:
            out[name] = value


class NvidiaSmi:
    """Reads NVIDIA telemetry without requiring Python NVIDIA packages."""

    QUERY_FIELDS = [
        "timestamp",
        "name",
        "driver_version",
        "pci.bus_id",
        "utilization.gpu",
        "utilization.memory",
        "temperature.gpu",
        "power.draw",
        "power.limit",
        "clocks.current.graphics",
        "clocks.current.memory",
        "clocks.current.sm",
        "fan.speed",
        "memory.used",
        "memory.total",
        "pstate",
        "pcie.link.gen.current",
        "pcie.link.width.current",
    ]

    def __init__(self) -> None:
        self.exe = self._find_nvidia_smi()

    @staticmethod
    def _find_nvidia_smi() -> Optional[str]:
        candidates = [
            shutil.which("nvidia-smi"),
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None

    def available(self) -> bool:
        return bool(self.exe)

    def sample(self) -> Dict[str, Any]:
        if not self.exe:
            return {"available": False, "error": "nvidia-smi not found"}

        fields = ",".join(self.QUERY_FIELDS)
        cmd = [
            self.exe,
            f"--query-gpu={fields}",
            "--format=csv,noheader,nounits",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if proc.returncode != 0:
                return {
                    "available": True,
                    "error": proc.stderr.strip() or f"nvidia-smi exited {proc.returncode}",
                }

            first_line = proc.stdout.strip().splitlines()[0]
            values = next(csv.reader([first_line], skipinitialspace=True))
            result: Dict[str, Any] = {"available": True}

            for field, raw in zip(self.QUERY_FIELDS, values):
                raw = raw.strip()
                key = field.replace(".", "_")
                if raw in {"N/A", "[Not Supported]", "Not Supported", ""}:
                    result[key] = None
                    continue

                if field in {
                    "utilization.gpu",
                    "utilization.memory",
                    "temperature.gpu",
                                "fan.speed",
                    "memory.used",
                    "memory.total",
                    "pcie.link.gen.current",
                    "pcie.link.width.current",
                }:
                    result[key] = safe_float(raw)
                elif field in {
                    "power.draw",
                    "power.limit",
                    "clocks.current.graphics",
                    "clocks.current.memory",
                    "clocks.current.sm",
                            }:
                    result[key] = safe_float(raw)
                else:
                    result[key] = raw

            return result
        except Exception as exc:
            return {"available": True, "error": f"{type(exc).__name__}: {exc}"}


class LibreHardwareMonitorWMI:
    """
    Optional sensor source.

    To enable:
    1. Run LibreHardwareMonitor.exe as administrator.
    2. In Options, enable "Remote Web Server" is NOT required.
    3. Keep LibreHardwareMonitor open while this logger runs.
    4. Install Python packages: pip install wmi pywin32
    """

    def __init__(self) -> None:
        self.error: Optional[str] = None
        self.connection = None
        self._connect()

    def _connect(self) -> None:
        if os.name != "nt":
            self.error = "LibreHardwareMonitor WMI is Windows-only"
            return
        try:
            import wmi  # type: ignore

            # LibreHardwareMonitor commonly publishes ROOT\LibreHardwareMonitor.
            self.connection = wmi.WMI(namespace=r"root\LibreHardwareMonitor")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.connection = None

    def available(self) -> bool:
        return self.connection is not None

    @staticmethod
    def _normalize_sensor_name(name: str) -> str:
        name = re.sub(r"\s+", " ", name.strip())
        name = name.replace("/", "_")
        name = re.sub(r"[^A-Za-z0-9_.()+\- ]+", "", name)
        return name

    def sample(self) -> Dict[str, Any]:
        if not self.connection:
            return {"available": False, "error": self.error}

        out: Dict[str, Any] = {"available": True}
        try:
            sensors = self.connection.Sensor()
            for sensor in sensors:
                sensor_type = str(getattr(sensor, "SensorType", "Unknown"))
                name = self._normalize_sensor_name(str(getattr(sensor, "Name", "Unnamed")))
                value = safe_float(getattr(sensor, "Value", None))
                if value is None:
                    continue

                # Keep names understandable and avoid collisions.
                key = f"{sensor_type}.{name}"
                if key in out:
                    suffix = 2
                    while f"{key}_{suffix}" in out:
                        suffix += 1
                    key = f"{key}_{suffix}"
                out[key] = value

            return out
        except Exception as exc:
            return {"available": True, "error": f"{type(exc).__name__}: {exc}"}


class WindowsEventCollector:
    """Collects useful events after a reboot using PowerShell/Get-WinEvent."""

    PROVIDERS = [
        "Microsoft-Windows-Kernel-Power",
        "Microsoft-Windows-WHEA-Logger",
        "Display",
        "nvlddmkm",
        "Microsoft-Windows-Kernel-PnP",
        "Microsoft-Windows-WindowsErrorReporting",
    ]

    def collect_recent(self, output_file: Path, hours: int = 24) -> None:
        if os.name != "nt":
            return

        provider_array = ",".join(f"'{p}'" for p in self.PROVIDERS)
        ps = f"""
$start=(Get-Date).AddHours(-{int(hours)})
$providers=@({provider_array})
$events = Get-WinEvent -FilterHashtable @{{LogName=@('System','Application'); StartTime=$start}} -ErrorAction SilentlyContinue |
    Where-Object {{
        $_.LevelDisplayName -in @('Critical','Error','Warning') -or
        $_.ProviderName -in $providers -or
        $_.Id -in @(41,18,19,20,4101,1000,1001,6008,14)
    }} |
    Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, LogName, Message
$events | ConvertTo-Json -Depth 4
"""
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = proc.stdout.strip()
            if not text:
                text = json.dumps(
                    {"error": proc.stderr.strip() or "No matching events found"},
                    indent=2,
                )
            output_file.write_text(text, encoding="utf-8", errors="replace")
        except Exception as exc:
            output_file.write_text(
                json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2),
                encoding="utf-8",
            )


class CrashMonitor:
    def __init__(self, interval: float, output_dir: Path, status_queue: "queue.Queue[Dict[str, Any]]") -> None:
        self.interval = max(0.25, float(interval))
        self.output_dir = output_dir
        self.status_queue = status_queue
        self.stop_event = threading.Event()

        self.nvidia = NvidiaSmi()
        self.lhm = LibreHardwareMonitorWMI()
        self.event_collector = WindowsEventCollector()

        self.session_id = now_local().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.output_dir / f"session_{self.session_id}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.csv_path = self.session_dir / "telemetry.csv"
        self.last_sample_path = self.session_dir / "last_sample.json"
        self.session_info_path = self.session_dir / "session_info.json"
        self.events_path = self.session_dir / "recent_windows_events.json"
        self.error_path = self.session_dir / "logger_errors.txt"

        self.dynamic_fields: List[str] = []
        self.fieldnames: List[str] = []
        self.csv_file = None
        self.writer = None
        self.sample_count = 0
        self.last_rows: deque = deque(maxlen=120)

    def _write_session_info(self, state: str) -> None:
        info = {
            "app": APP_NAME,
            "version": VERSION,
            "session_id": self.session_id,
            "state": state,
            "last_update": iso_now(),
            "interval_seconds": self.interval,
            "session_dir": str(self.session_dir),
            "platform": platform.platform(),
            "python": sys.version,
            "psutil_available": psutil is not None,
            "nvidia_smi_available": self.nvidia.available(),
            "librehardwaremonitor_available": self.lhm.available(),
            "librehardwaremonitor_error": self.lhm.error,
        }
        self.session_info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")

    def _log_error(self, message: str) -> None:
        with self.error_path.open("a", encoding="utf-8") as f:
            f.write(f"[{iso_now()}] {message}\n")

    def _base_sample(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "timestamp": iso_now(),
            "epoch": time.time(),
            "sample_number": self.sample_count + 1,
        }

        if psutil is None:
            row["system.psutil_error"] = "psutil not installed"
            return row

        try:
            cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
            row["cpu.total_usage_percent"] = psutil.cpu_percent(interval=None)
            for idx, value in enumerate(cpu_per_core):
                row[f"cpu.core_{idx}_usage_percent"] = value

            freq = psutil.cpu_freq()
            if freq:
                row["cpu.frequency_current_mhz"] = freq.current
                row["cpu.frequency_min_mhz"] = freq.min
                row["cpu.frequency_max_mhz"] = freq.max

            vm = psutil.virtual_memory()
            row["memory.total_mb"] = round(vm.total / 1024 / 1024, 2)
            row["memory.used_mb"] = round(vm.used / 1024 / 1024, 2)
            row["memory.available_mb"] = round(vm.available / 1024 / 1024, 2)
            row["memory.usage_percent"] = vm.percent

            swap = psutil.swap_memory()
            row["pagefile.total_mb"] = round(swap.total / 1024 / 1024, 2)
            row["pagefile.used_mb"] = round(swap.used / 1024 / 1024, 2)
            row["pagefile.usage_percent"] = swap.percent

            disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
            row["disk.system_usage_percent"] = disk.percent
            row["disk.system_free_gb"] = round(disk.free / 1024 / 1024 / 1024, 2)

            io = psutil.disk_io_counters()
            if io:
                row["disk.read_bytes_total"] = io.read_bytes
                row["disk.write_bytes_total"] = io.write_bytes

            net = psutil.net_io_counters()
            if net:
                row["network.bytes_sent_total"] = net.bytes_sent
                row["network.bytes_recv_total"] = net.bytes_recv

            row["system.process_count"] = len(psutil.pids())
            row["system.uptime_seconds"] = time.time() - psutil.boot_time()
        except Exception as exc:
            row["system.psutil_error"] = f"{type(exc).__name__}: {exc}"

        return row

    @staticmethod
    def _extract_high_value_lhm_metrics(lhm: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        patterns = {
            "cpu.package_temperature_c": [r"Temperature\..*CPU Package", r"Temperature\..*Tctl", r"Temperature\..*Tdie"],
            "cpu.package_power_w": [r"Power\..*CPU Package", r"Power\..*Package"],
            "cpu.core_voltage_v": [r"Voltage\..*CPU Core", r"Voltage\..*Vcore"],
            "gpu.core_temperature_c": [r"Temperature\..*GPU Core"],
            "gpu.hotspot_temperature_c": [r"Temperature\..*GPU Hot Spot", r"Temperature\..*Hot Spot"],
            "gpu.memory_temperature_c": [r"Temperature\..*GPU Memory"],
            "gpu.core_load_percent": [r"Load\..*GPU Core"],
            "gpu.memory_load_percent": [r"Load\..*GPU Memory"],
            "gpu.power_w": [r"Power\..*GPU Power", r"Power\..*GPU Package"],
            "gpu.fan_rpm": [r"Fan\..*GPU"],
            "psu.12v_sensor_v": [r"Voltage\..*\+?12V", r"Voltage\..*12V"],
            "psu.5v_sensor_v": [r"Voltage\..*\+?5V", r"Voltage\..*5V"],
            "psu.3_3v_sensor_v": [r"Voltage\..*\+?3\.3V", r"Voltage\..*3VCC"],
            "motherboard.temperature_c": [r"Temperature\..*Motherboard"],
            "aio.pump_rpm": [r"Fan\..*Pump", r"Fan\..*AIO"],
            "cpu.fan_rpm": [r"Fan\..*CPU"],
        }

        for target, regexes in patterns.items():
            for key, value in lhm.items():
                if key in {"available", "error"}:
                    continue
                if any(re.search(pattern, key, flags=re.IGNORECASE) for pattern in regexes):
                    out[target] = value
                    break
        return out

    def _collect_sample(self) -> Dict[str, Any]:
        row = self._base_sample()

        gpu = self.nvidia.sample()
        flatten_dict("nvidia", gpu, row)
        row["gpu.telemetry_status"] = "Connected" if gpu.get("available") and not gpu.get("error") else (gpu.get("error") or "Not connected")

        lhm = self.lhm.sample()
        flatten_dict("lhm", lhm, row)
        row.update(self._extract_high_value_lhm_metrics(lhm))

        # Prefer NVIDIA telemetry for these standardized columns when available.
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
        }
        for target, source in mappings.items():
            if row.get(source) is not None:
                row[target] = row[source]

        # Derived warnings. These are not proof of failure; they help spot the last abnormal sample.
        warnings: List[str] = []
        cpu_temp = safe_float(row.get("cpu.package_temperature_c"))
        gpu_temp = safe_float(row.get("gpu.core_temperature_c"))
        hotspot = safe_float(row.get("gpu.hotspot_temperature_c"))
        gpu_power = safe_float(row.get("gpu.power_w"))
        rail12 = safe_float(row.get("psu.12v_sensor_v"))
        rail5 = safe_float(row.get("psu.5v_sensor_v"))
        rail33 = safe_float(row.get("psu.3_3v_sensor_v"))

        if cpu_temp is not None and cpu_temp >= 90:
            warnings.append(f"CPU_TEMP_HIGH:{cpu_temp:.1f}C")
        if gpu_temp is not None and gpu_temp >= 83:
            warnings.append(f"GPU_TEMP_HIGH:{gpu_temp:.1f}C")
        if hotspot is not None and hotspot >= 100:
            warnings.append(f"GPU_HOTSPOT_HIGH:{hotspot:.1f}C")
        if rail12 is not None and not 11.4 <= rail12 <= 12.6:
            warnings.append(f"12V_OUT_OF_ATX_RANGE:{rail12:.3f}V")
        if rail5 is not None and not 4.75 <= rail5 <= 5.25:
            warnings.append(f"5V_OUT_OF_ATX_RANGE:{rail5:.3f}V")
        if rail33 is not None and not 3.135 <= rail33 <= 3.465:
            warnings.append(f"3V3_OUT_OF_ATX_RANGE:{rail33:.3f}V")
        if gpu_power is not None and gpu_power <= 1 and safe_float(row.get("gpu.core_load_percent") or 0) > 20:
            warnings.append("GPU_LOAD_WITH_NEAR_ZERO_POWER")

        row["warnings"] = " | ".join(warnings)
        return row

    def _ensure_writer(self, row: Dict[str, Any]) -> None:
        if self.writer is not None:
            return

        self.fieldnames = list(row.keys())
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8-sig", buffering=1)
        self.writer = csv.DictWriter(self.csv_file, fieldnames=self.fieldnames, extrasaction="ignore")
        self.writer.writeheader()
        self.csv_file.flush()
        os.fsync(self.csv_file.fileno())

    def _write_row(self, row: Dict[str, Any]) -> None:
        self._ensure_writer(row)

        # New dynamic sensors can appear after startup. Put them in JSON even if they are not
        # in the initial CSV header. The standardized, important fields are present at startup.
        assert self.writer is not None
        assert self.csv_file is not None
        self.writer.writerow(row)
        self.csv_file.flush()
        os.fsync(self.csv_file.fileno())

        self.last_sample_path.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
        self.last_rows.append(row)

    def run(self) -> None:
        self._write_session_info("running")
        self.event_collector.collect_recent(self.events_path, hours=24)

        # Prime psutil's CPU counters.
        if psutil is not None:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)

        next_tick = time.monotonic()
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    row = self._collect_sample()
                    self._write_row(row)
                    self.sample_count += 1
                    self.status_queue.put(
                        {
                            "type": "sample",
                            "sample": row,
                            "session_dir": str(self.session_dir),
                            "count": self.sample_count,
                        }
                    )
                except Exception:
                    self._log_error(traceback.format_exc())
                    self.status_queue.put({"type": "error", "message": "Sample failed; see logger_errors.txt"})

                next_tick += self.interval
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    self.stop_event.wait(sleep_for)
                else:
                    next_tick = time.monotonic()
        finally:
            if self.csv_file:
                try:
                    self.csv_file.flush()
                    os.fsync(self.csv_file.fileno())
                    self.csv_file.close()
                except Exception:
                    pass
            self._write_session_info("stopped")
            self.status_queue.put({"type": "stopped", "session_dir": str(self.session_dir)})

    def stop(self) -> None:
        self.stop_event.set()


class App:
    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("980x650")
        self.root.minsize(850, 560)

        LOG_ROOT.mkdir(parents=True, exist_ok=True)

        self.status_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.monitor: Optional[CrashMonitor] = None
        self.worker: Optional[threading.Thread] = None

        self.interval_var = tk.StringVar(value="1.0")
        self.output_var = tk.StringVar(value=str(LOG_ROOT))
        self.status_var = tk.StringVar(value="Ready")
        self.session_var = tk.StringVar(value="No active session")
        self.last_values: Dict[str, tk.StringVar] = {}

        self._build_ui()
        self._check_previous_unclean_session()
        self.root.after(250, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(outer, text="Logging Controls", padding=10)
        controls.pack(fill="x")

        ttk.Label(controls, text="Sample interval (seconds):").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.interval_var, width=8).grid(row=0, column=1, padx=(6, 20))

        ttk.Label(controls, text="Log folder:").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.output_var, width=55).grid(row=0, column=3, padx=6, sticky="ew")
        ttk.Button(controls, text="Browse", command=self._browse).grid(row=0, column=4, padx=4)

        self.start_btn = ttk.Button(controls, text="Start Logging", command=self._start)
        self.start_btn.grid(row=1, column=0, columnspan=2, pady=(10, 0), sticky="ew")

        self.stop_btn = ttk.Button(controls, text="Stop Logging", command=self._stop, state="disabled")
        self.stop_btn.grid(row=1, column=2, pady=(10, 0), sticky="ew")

        ttk.Button(controls, text="Open Log Folder", command=self._open_logs).grid(
            row=1, column=3, pady=(10, 0), sticky="ew"
        )
        ttk.Button(controls, text="Collect Windows Events Now", command=self._collect_events_now).grid(
            row=1, column=4, pady=(10, 0), sticky="ew"
        )
        controls.columnconfigure(3, weight=1)

        status = ttk.Frame(outer, padding=(0, 10))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(status, textvariable=self.session_var).pack(anchor="w")

        values_frame = ttk.LabelFrame(outer, text="Live Values", padding=10)
        values_frame.pack(fill="both", expand=True)

        fields = [
            ("CPU usage", "cpu.total_usage_percent", "%"),
            ("CPU temperature", "cpu.package_temperature_c", "°C"),
            ("CPU package power", "cpu.package_power_w", "W"),
            ("CPU frequency", "cpu.frequency_current_mhz", "MHz"),
            ("RAM usage", "memory.usage_percent", "%"),
            ("GPU usage", "gpu.core_load_percent", "%"),
            ("GPU temperature", "gpu.core_temperature_c", "°C"),
            ("GPU hotspot", "gpu.hotspot_temperature_c", "°C"),
            ("GPU power", "gpu.power_w", "W"),
            ("GPU voltage", "gpu.graphics_voltage_v", "V"),
            ("GPU fan", "gpu.fan_percent", "%"),
            ("GPU core clock", "gpu.core_clock_mhz", "MHz"),
            ("GPU VRAM used", "gpu.memory_used_mb", "MB"),
            ("12V motherboard sensor", "psu.12v_sensor_v", "V"),
            ("5V motherboard sensor", "psu.5v_sensor_v", "V"),
            ("3.3V motherboard sensor", "psu.3_3v_sensor_v", "V"),
            ("AIO pump", "aio.pump_rpm", "RPM"),
            ("Warnings", "warnings", ""),
        ]

        for idx, (label, key, suffix) in enumerate(fields):
            row = idx // 2
            col = (idx % 2) * 2
            ttk.Label(values_frame, text=f"{label}:").grid(row=row, column=col, sticky="w", padx=(0, 8), pady=5)
            var = tk.StringVar(value="—")
            self.last_values[key] = var
            ttk.Label(values_frame, textvariable=var, font=("Consolas", 10, "bold")).grid(
                row=row, column=col + 1, sticky="w", pady=5
            )
            var._suffix = suffix  # type: ignore[attr-defined]

        values_frame.columnconfigure(1, weight=1)
        values_frame.columnconfigure(3, weight=1)

        note = (
            "For the best sensor coverage, run LibreHardwareMonitor as Administrator before starting this logger. "
            "The logger writes and flushes telemetry.csv every sample, so a hard freeze should leave the last completed row intact. "
            "PSU rail readings only appear if the motherboard exposes them."
        )
        ttk.Label(outer, text=note, wraplength=930, justify="left").pack(fill="x", pady=(10, 0))

    def _check_previous_unclean_session(self) -> None:
        try:
            if not STATE_FILE.exists():
                return
            info = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if info.get("state") == "running":
                self.status_var.set(
                    "Previous session did not close normally. This may indicate a crash or power loss."
                )
                self.session_var.set(f"Previous log: {info.get('session_dir', 'unknown')}")
        except Exception:
            pass

    def _browse(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_var.get())
        if selected:
            self.output_var.set(selected)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            interval = float(self.interval_var.get())
            if not 0.25 <= interval <= 60:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid interval", "Enter a value from 0.25 to 60 seconds.")
            return

        output = Path(self.output_var.get()).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Cannot use folder", str(exc))
            return

        self.monitor = CrashMonitor(interval, output, self.status_queue)
        self.worker = threading.Thread(target=self.monitor.run, daemon=True)
        self.worker.start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Logging in progress — leave this running while gaming.")
        self.session_var.set(str(self.monitor.session_dir))

    def _stop(self) -> None:
        if self.monitor:
            self.monitor.stop()
        self.status_var.set("Stopping logger...")

    def _open_logs(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Open folder failed", str(exc))

    def _collect_events_now(self) -> None:
        base = Path(self.output_var.get()).expanduser()
        folder = base / f"event_snapshot_{now_local().strftime('%Y%m%d_%H%M%S')}"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / "recent_windows_events.json"

        self.status_var.set("Collecting recent Windows events...")

        def work() -> None:
            WindowsEventCollector().collect_recent(output, hours=24)
            self.status_queue.put({"type": "events", "path": str(output)})

        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.status_queue.get_nowait()
                kind = item.get("type")
                if kind == "sample":
                    sample = item["sample"]
                    count = item["count"]
                    self.status_var.set(f"Logging sample {count:,} — safe to minimize")
                    for key, var in self.last_values.items():
                        value = sample.get(key)
                        if value is None or value == "":
                            text = "—"
                        elif isinstance(value, float):
                            text = f"{value:.2f}"
                        else:
                            text = str(value)
                        suffix = getattr(var, "_suffix", "")
                        if suffix and text != "—":
                            text = f"{text} {suffix}"
                        var.set(text)
                elif kind == "error":
                    self.status_var.set(item.get("message", "Logging error"))
                elif kind == "stopped":
                    self.status_var.set("Logging stopped")
                    self.session_var.set(item.get("session_dir", ""))
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                elif kind == "events":
                    self.status_var.set("Windows event snapshot created")
                    self.session_var.set(item.get("path", ""))
        except queue.Empty:
            pass
        finally:
            self.root.after(250, self._poll_queue)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Logger is running",
                "Stop logging and close? For crash testing, minimize the app instead.",
            ):
                return
            self._stop()
            self.root.after(500, self.root.destroy)
        else:
            self.root.destroy()


def main() -> int:
    if tk is None:
        print("Tkinter is unavailable in this Python installation.")
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
