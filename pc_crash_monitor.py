"""
PC Crash Monitor v1.4
Windows crash logger that combines:
  1) Native NVIDIA NVML telemetry
  2) Windows/psutil telemetry
  3) LibreHardwareMonitor's own CSV logger

Why CSV instead of LibreHardwareMonitor WMI/DLL?
LibreHardwareMonitor already records the widest sensor set reliably. v1.4 watches its
latest LibreHardwareMonitorLog-*.csv, follows file rotation automatically, and merges the
latest hardware sample into our crash-safe telemetry.csv.
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
from typing import Any, Dict, List, Optional, Tuple

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
VERSION = "1.4.0"
DEFAULT_LOG_ROOT = Path.home() / "Documents" / "PC_Crash_Monitor"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="milliseconds")


def sf(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", text).strip("_")[:140]


class NvidiaReader:
    """NVML-first NVIDIA telemetry."""

    def __init__(self):
        self.ok = False
        self.error = None
        self.pynvml = None
        self.handle = None
        try:
            import pynvml
            self.pynvml = pynvml
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                raise RuntimeError("NVML reports no NVIDIA GPU")
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.ok = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def status(self) -> str:
        return "Connected (NVML)" if self.ok else f"Not connected ({self.error})"

    def sample(self) -> Dict[str, Any]:
        if not self.ok:
            return {"available": False, "error": self.error}

        n = self.pynvml
        h = self.handle
        out: Dict[str, Any] = {"available": True}

        def get(name, func, divisor=1.0):
            try:
                value = func()
                if isinstance(value, bytes):
                    value = value.decode(errors="replace")
                if isinstance(value, (int, float)):
                    value /= divisor
                out[name] = value
            except Exception:
                out[name] = None

        get("name", lambda: n.nvmlDeviceGetName(h))
        get("driver_version", lambda: n.nvmlSystemGetDriverVersion())
        get("temperature_gpu", lambda: n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
        get("power_draw_w", lambda: n.nvmlDeviceGetPowerUsage(h), 1000.0)
        get("power_limit_w", lambda: n.nvmlDeviceGetEnforcedPowerLimit(h), 1000.0)
        get("fan_percent", lambda: n.nvmlDeviceGetFanSpeed(h))
        get("graphics_clock_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS))
        get("memory_clock_mhz", lambda: n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_MEM))

        try:
            util = n.nvmlDeviceGetUtilizationRates(h)
            out["gpu_usage_percent"] = float(util.gpu)
            out["memory_controller_usage_percent"] = float(util.memory)
        except Exception:
            out["gpu_usage_percent"] = None
            out["memory_controller_usage_percent"] = None

        try:
            mem = n.nvmlDeviceGetMemoryInfo(h)
            out["vram_used_mb"] = mem.used / 1048576
            out["vram_total_mb"] = mem.total / 1048576
        except Exception:
            out["vram_used_mb"] = None
            out["vram_total_mb"] = None

        try:
            out["pcie_generation"] = float(n.nvmlDeviceGetCurrPcieLinkGeneration(h))
        except Exception:
            out["pcie_generation"] = None
        try:
            out["pcie_width"] = float(n.nvmlDeviceGetCurrPcieLinkWidth(h))
        except Exception:
            out["pcie_width"] = None

        return out


class LHMCSVReader:
    """
    Reads LibreHardwareMonitor's native two-row CSV header:
      row 1 = sensor identifier paths
      row 2 = display sensor names
      row 3+ = samples

    It automatically follows LibreHardwareMonitor's rotating log files.
    """

    GLOB = "LibreHardwareMonitorLog-*.csv"

    def __init__(self, folder: Optional[Path] = None):
        self.manual_folder = folder
        self.last_file: Optional[Path] = None
        self.last_error: Optional[str] = None
        self.last_timestamp: Optional[str] = None
        self.sensor_count = 0

    def set_folder(self, folder: Optional[Path]) -> None:
        self.manual_folder = folder
        self.last_file = None
        self.last_error = None

    def candidate_folders(self) -> List[Path]:
        folders: List[Path] = []
        if self.manual_folder:
            folders.append(self.manual_folder)

        # The running LibreHardwareMonitor executable's directory is the best guess.
        if psutil:
            try:
                for proc in psutil.process_iter(["name", "exe", "cwd"]):
                    name = (proc.info.get("name") or "").lower()
                    if "librehardwaremonitor" in name:
                        exe = proc.info.get("exe")
                        cwd = proc.info.get("cwd")
                        if exe:
                            folders.append(Path(exe).parent)
                        if cwd:
                            folders.append(Path(cwd))
            except Exception:
                pass

        home = Path.home()
        folders += [
            home / "Downloads",
            home / "Desktop",
            home / "Documents",
            home / "OneDrive" / "Desktop",
            home / "OneDrive" / "Documents",
        ]

        # A shallow search catches extracted folders without scanning the whole drive.
        expanded: List[Path] = []
        seen = set()
        for folder in folders:
            try:
                folder = folder.expanduser().resolve()
            except Exception:
                continue
            if folder in seen or not folder.exists():
                continue
            seen.add(folder)
            expanded.append(folder)
            try:
                for sub in folder.glob("*LibreHardwareMonitor*"):
                    if sub.is_dir() and sub not in seen:
                        seen.add(sub)
                        expanded.append(sub)
            except Exception:
                pass
        return expanded

    def latest_log_file(self) -> Optional[Path]:
        files: List[Path] = []
        for folder in self.candidate_folders():
            try:
                files.extend(p for p in folder.glob(self.GLOB) if p.is_file())
                # Also one level down for common extracted layouts.
                files.extend(p for p in folder.glob(f"*/{self.GLOB}") if p.is_file())
            except Exception:
                pass
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _read_complete_rows(path: Path) -> Tuple[List[str], List[str], List[str]]:
        # Retry once in case LibreHardwareMonitor is writing at the same instant.
        last_exc = None
        for _ in range(2):
            try:
                with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
                    rows = list(csv.reader(f))
                if len(rows) < 3:
                    raise ValueError("LibreHardwareMonitor CSV has fewer than 3 rows")
                ids = rows[0]
                labels = rows[1]
                # Last nonempty row with approximately the same width as the headers.
                data = None
                expected = min(len(ids), len(labels))
                for row in reversed(rows[2:]):
                    if row and len(row) >= expected:
                        data = row
                        break
                if data is None:
                    raise ValueError("No complete LibreHardwareMonitor data row found")
                return ids, labels, data
            except Exception as exc:
                last_exc = exc
                time.sleep(0.05)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _records(ids: List[str], labels: List[str], data: List[str]) -> List[Dict[str, Any]]:
        out = []
        n = min(len(ids), len(labels), len(data))
        for i in range(1, n):  # column 0 is Time
            out.append({
                "id": (ids[i] or "").strip(),
                "label": (labels[i] or "").strip(),
                "value": sf(data[i]),
                "index": i,
            })
        return out

    @staticmethod
    def _first(records, *, id_contains=None, label_exact=None, label_regex=None):
        for rec in records:
            rid = rec["id"].lower()
            label = rec["label"]
            if id_contains and not all(x.lower() in rid for x in id_contains):
                continue
            if label_exact is not None and label.lower() != label_exact.lower():
                continue
            if label_regex and not re.search(label_regex, label, re.I):
                continue
            if rec["value"] is not None:
                return rec["value"]
        return None

    @classmethod
    def standardize(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        o: Dict[str, Any] = {}

        # CPU: works with /intelcpu/, /amdcpu/, and future cpu path variants.
        cpu_temp = (
            cls._first(records, id_contains=["temperature"], label_exact="CPU Package")
            or cls._first(records, id_contains=["temperature"], label_regex=r"CPU.*(?:Tctl|Tdie)")
            or cls._first(records, id_contains=["temperature"], label_regex=r"CPU Core Max")
            or cls._first(records, id_contains=["temperature"], label_exact="CPU Core")
        )
        cpu_power = (
            cls._first(records, id_contains=["power"], label_exact="CPU Package")
            or cls._first(records, id_contains=["power"], label_regex=r"CPU.*Package")
        )
        cpu_voltage = (
            cls._first(records, id_contains=["voltage"], label_regex=r"CPU Core")
            or cls._first(records, id_contains=["voltage"], label_regex=r"Vcore")
        )

        if cpu_temp is not None: o["cpu.temperature_c"] = cpu_temp
        if cpu_power is not None: o["cpu.package_power_w"] = cpu_power
        if cpu_voltage is not None: o["cpu.core_voltage_v"] = cpu_voltage

        # NVIDIA/AMD GPU sensors in LHM.
        gpu_hot = cls._first(records, id_contains=["gpu", "temperature"], label_regex=r"Hot\s*Spot")
        gpu_voltage = cls._first(records, id_contains=["gpu", "voltage"], label_regex=r"GPU Core Voltage|Core Voltage|GPU Core")
        gpu_package = cls._first(records, id_contains=["gpu", "power"], label_regex=r"GPU Package|GPU Board Power")
        gpu_fan_rpm = cls._first(records, id_contains=["gpu", "fan"], label_regex=r"GPU Fan|Fan")

        if gpu_hot is not None: o["gpu.hotspot_temperature_c"] = gpu_hot
        if gpu_voltage is not None: o["gpu.voltage_v"] = gpu_voltage
        if gpu_package is not None: o["gpu.lhm_power_w"] = gpu_package
        if gpu_fan_rpm is not None: o["gpu.fan_rpm"] = gpu_fan_rpm

        # Board rails. Not every board exposes these.
        rail12 = cls._first(records, id_contains=["voltage"], label_regex=r"^\+?12V$")
        rail5 = cls._first(records, id_contains=["voltage"], label_regex=r"^\+?5V$")
        rail33 = (
            cls._first(records, id_contains=["voltage"], label_regex=r"^\+?3\.3V$")
            or cls._first(records, id_contains=["voltage"], label_exact="AVCC")
        )
        if rail12 is not None: o["board.12v_v"] = rail12
        if rail5 is not None: o["board.5v_v"] = rail5
        if rail33 is not None: o["board.3_3v_v"] = rail33

        # Named pump/fan sensors, where available.
        pump = cls._first(records, id_contains=["fan"], label_regex=r"AIO|Pump|Water")
        cpu_fan = cls._first(records, id_contains=["fan"], label_regex=r"CPU Fan")
        if pump is not None: o["cooling.aio_pump_rpm"] = pump
        if cpu_fan is not None: o["cooling.cpu_fan_rpm"] = cpu_fan

        # Preserve first several generic motherboard fan readings as clues even when the board
        # only calls them "Fan #1", "Fan #2", etc.
        generic_fans = [
            r for r in records
            if "/fan/" in r["id"].lower()
            and "/gpu" not in r["id"].lower()
            and r["value"] is not None
        ]
        for idx, rec in enumerate(generic_fans[:8], 1):
            o[f"board.fan_{idx}_rpm"] = rec["value"]

        return o

    def sample(self) -> Dict[str, Any]:
        path = self.latest_log_file()
        if not path:
            self.last_error = "No LibreHardwareMonitorLog-*.csv found"
            return {"connected": False, "error": self.last_error}

        try:
            ids, labels, data = self._read_complete_rows(path)
            records = self._records(ids, labels, data)
            self.last_file = path
            self.last_error = None
            self.last_timestamp = data[0] if data else None
            self.sensor_count = len(records)

            out: Dict[str, Any] = {
                "connected": True,
                "source_file": str(path),
                "sample_time": self.last_timestamp,
                "sensor_count": self.sensor_count,
            }
            out.update(self.standardize(records))

            # Keep the raw values too, uniquely keyed by identifier.
            for rec in records:
                if rec["value"] is not None and rec["id"]:
                    out["raw." + safe_name(rec["id"])] = rec["value"]

            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {
                "connected": False,
                "source_file": str(path),
                "error": self.last_error,
            }

    def status(self) -> str:
        if self.last_file and not self.last_error:
            return f"Connected ({self.last_file.name})"
        return self.last_error or "Not tested"


class WindowsEvents:
    def collect(self, output: Path, hours=24):
        if os.name != "nt":
            return
        script = f"""
$start=(Get-Date).AddHours(-{int(hours)})
Get-WinEvent -FilterHashtable @{{LogName=@('System','Application'); StartTime=$start}} -ErrorAction SilentlyContinue |
Where-Object {{
 $_.LevelDisplayName -in @('Critical','Error','Warning') -or
 $_.ProviderName -in @('Microsoft-Windows-Kernel-Power','Microsoft-Windows-WHEA-Logger','Display','nvlddmkm','Microsoft-Windows-WindowsErrorReporting') -or
 $_.Id -in @(41,18,19,20,4101,1000,1001,6008,14)
}} |
Select TimeCreated,Id,LevelDisplayName,ProviderName,LogName,Message |
ConvertTo-Json -Depth 4
"""
        try:
            cp = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output.write_text(cp.stdout.strip() or "[]", encoding="utf-8", errors="replace")
        except Exception as exc:
            output.write_text(json.dumps({"error": str(exc)}, indent=2), encoding="utf-8")


class CrashLogger:
    STANDARD_COLUMNS = [
        "timestamp", "epoch", "sample_number",
        "cpu.usage_percent", "cpu.frequency_mhz", "cpu.temperature_c",
        "cpu.package_power_w", "cpu.core_voltage_v",
        "ram.usage_percent", "ram.used_mb", "ram.total_mb",
        "gpu.usage_percent", "gpu.temperature_c", "gpu.hotspot_temperature_c",
        "gpu.power_w", "gpu.lhm_power_w", "gpu.voltage_v",
        "gpu.fan_percent", "gpu.fan_rpm", "gpu.core_clock_mhz",
        "gpu.memory_clock_mhz", "gpu.vram_used_mb", "gpu.vram_total_mb",
        "gpu.pcie_generation", "gpu.pcie_width",
        "board.12v_v", "board.5v_v", "board.3_3v_v",
        "cooling.aio_pump_rpm", "cooling.cpu_fan_rpm",
        "board.fan_1_rpm", "board.fan_2_rpm", "board.fan_3_rpm", "board.fan_4_rpm",
        "lhm.connected", "lhm.source_file", "lhm.sample_time", "lhm.sensor_count",
        "warnings",
    ]

    def __init__(self, interval: float, output_root: Path, lhm_folder: Optional[Path], q: queue.Queue):
        self.interval = max(0.25, float(interval))
        self.output_root = output_root
        self.q = q
        self.stop_evt = threading.Event()
        self.nvidia = NvidiaReader()
        self.lhm = LHMCSVReader(lhm_folder)
        self.events = WindowsEvents()

        sid = now_local().strftime("%Y%m%d_%H%M%S")
        self.session = output_root / f"session_{sid}"
        self.session.mkdir(parents=True, exist_ok=True)
        self.telemetry = self.session / "telemetry.csv"
        self.last_sample = self.session / "last_sample.json"
        self.diagnostics = self.session / "sensor_diagnostics.json"
        self.event_file = self.session / "recent_windows_events.json"
        self.error_file = self.session / "logger_errors.txt"

        self.file = None
        self.writer = None
        self.count = 0

    def collect(self) -> Dict[str, Any]:
        r: Dict[str, Any] = {
            "timestamp": iso_now(),
            "epoch": time.time(),
            "sample_number": self.count + 1,
        }

        if psutil:
            try:
                r["cpu.usage_percent"] = psutil.cpu_percent(None)
                freq = psutil.cpu_freq()
                if freq:
                    r["cpu.frequency_mhz"] = freq.current
                vm = psutil.virtual_memory()
                r["ram.usage_percent"] = vm.percent
                r["ram.used_mb"] = vm.used / 1048576
                r["ram.total_mb"] = vm.total / 1048576
            except Exception as exc:
                r["psutil.error"] = str(exc)

        nv = self.nvidia.sample()
        if nv.get("available"):
            mapping = {
                "gpu.usage_percent": "gpu_usage_percent",
                "gpu.temperature_c": "temperature_gpu",
                "gpu.power_w": "power_draw_w",
                "gpu.fan_percent": "fan_percent",
                "gpu.core_clock_mhz": "graphics_clock_mhz",
                "gpu.memory_clock_mhz": "memory_clock_mhz",
                "gpu.vram_used_mb": "vram_used_mb",
                "gpu.vram_total_mb": "vram_total_mb",
                "gpu.pcie_generation": "pcie_generation",
                "gpu.pcie_width": "pcie_width",
            }
            for dst, src in mapping.items():
                if nv.get(src) is not None:
                    r[dst] = nv[src]

        lhm = self.lhm.sample()
        r["lhm.connected"] = bool(lhm.get("connected"))
        r["lhm.source_file"] = lhm.get("source_file")
        r["lhm.sample_time"] = lhm.get("sample_time")
        r["lhm.sensor_count"] = lhm.get("sensor_count")
        for k, v in lhm.items():
            if k in ("connected", "source_file", "sample_time", "sensor_count", "error"):
                continue
            if k.startswith("raw."):
                r["lhm." + k] = v
            else:
                # LHM fills missing standardized metrics; NVML remains primary for overlapping GPU basics.
                if r.get(k) is None:
                    r[k] = v
                elif k in ("gpu.hotspot_temperature_c", "gpu.voltage_v", "gpu.fan_rpm",
                           "gpu.lhm_power_w", "cpu.temperature_c", "cpu.package_power_w",
                           "cpu.core_voltage_v", "board.12v_v", "board.5v_v",
                           "board.3_3v_v", "cooling.aio_pump_rpm", "cooling.cpu_fan_rpm") or k.startswith("board.fan_"):
                    r[k] = v

        warnings = []
        cpu_t = sf(r.get("cpu.temperature_c"))
        gpu_t = sf(r.get("gpu.temperature_c"))
        hot = sf(r.get("gpu.hotspot_temperature_c"))
        if cpu_t is not None and cpu_t >= 90:
            warnings.append(f"CPU_TEMP_HIGH:{cpu_t:.1f}C")
        if gpu_t is not None and gpu_t >= 83:
            warnings.append(f"GPU_TEMP_HIGH:{gpu_t:.1f}C")
        if hot is not None and hot >= 100:
            warnings.append(f"GPU_HOTSPOT_HIGH:{hot:.1f}C")

        for key, lo, hi, label in [
            ("board.12v_v", 11.4, 12.6, "12V"),
            ("board.5v_v", 4.75, 5.25, "5V"),
            ("board.3_3v_v", 3.135, 3.465, "3.3V"),
        ]:
            value = sf(r.get(key))
            if value is not None and not lo <= value <= hi:
                warnings.append(f"{label}_OUT_OF_RANGE:{value:.3f}V")

        if not lhm.get("connected"):
            warnings.append("LHM_CSV_NOT_CONNECTED")

        r["warnings"] = " | ".join(warnings)
        return r

    def write(self, row: Dict[str, Any]):
        if self.writer is None:
            # Put standardized fields first, then whatever raw LHM fields existed at startup.
            fields = list(self.STANDARD_COLUMNS)
            fields += [k for k in row.keys() if k not in fields]
            self.file = self.telemetry.open("w", newline="", encoding="utf-8-sig", buffering=1)
            self.writer = csv.DictWriter(self.file, fieldnames=fields, extrasaction="ignore")
            self.writer.writeheader()

        self.writer.writerow(row)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.last_sample.write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")

    def initial_diagnostics(self):
        lhm = self.lhm.sample()
        diag = {
            "timestamp": iso_now(),
            "app_version": VERSION,
            "platform": platform.platform(),
            "nvidia_status": self.nvidia.status(),
            "lhm_status": self.lhm.status(),
            "lhm_selected_folder": str(self.lhm.manual_folder) if self.lhm.manual_folder else None,
            "lhm_source_file": lhm.get("source_file"),
            "lhm_sensor_count": lhm.get("sensor_count"),
            "lhm_error": lhm.get("error"),
            "lhm_candidate_folders": [str(x) for x in self.lhm.candidate_folders()],
        }
        self.diagnostics.write_text(json.dumps(diag, indent=2), encoding="utf-8")

    def run(self):
        self.initial_diagnostics()
        self.events.collect(self.event_file)
        if psutil:
            psutil.cpu_percent(None)

        try:
            while not self.stop_evt.is_set():
                started = time.monotonic()
                try:
                    row = self.collect()
                    self.write(row)
                    self.count += 1
                    self.q.put({
                        "type": "sample",
                        "row": row,
                        "count": self.count,
                        "session": str(self.session),
                        "nvidia": self.nvidia.status(),
                        "lhm": self.lhm.status(),
                    })
                except Exception:
                    with self.error_file.open("a", encoding="utf-8") as f:
                        f.write(f"[{iso_now()}]\n{traceback.format_exc()}\n")
                    self.q.put({"type": "error", "message": "Sampling error; see logger_errors.txt"})

                elapsed = time.monotonic() - started
                self.stop_evt.wait(max(0, self.interval - elapsed))
        finally:
            if self.file:
                try:
                    self.file.flush()
                    os.fsync(self.file.fileno())
                    self.file.close()
                except Exception:
                    pass
            self.q.put({"type": "stopped"})

    def stop(self):
        self.stop_evt.set()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("1080x800")
        root.minsize(980, 700)

        self.q = queue.Queue()
        self.logger = None
        self.worker = None

        self.interval = tk.StringVar(value="1.0")
        self.output = tk.StringVar(value=str(DEFAULT_LOG_ROOT))
        self.lhm_folder = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready")
        self.nvidia_status = tk.StringVar(value="NVIDIA GPU: Not tested")
        self.lhm_status = tk.StringVar(value="LibreHardwareMonitor CSV: Not tested")
        self.values = {}

        self.build()
        root.after(250, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(outer, text="Logging Controls", padding=10)
        controls.pack(fill="x")
        ttk.Label(controls, text="Sample interval:").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.interval, width=8).grid(row=0, column=1, padx=(5, 20))
        ttk.Label(controls, text="Crash log folder:").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.output, width=57).grid(row=0, column=3, padx=5, sticky="ew")
        ttk.Button(controls, text="Browse", command=self.browse_output).grid(row=0, column=4)
        self.start_btn = ttk.Button(controls, text="Start Logging", command=self.start_logging)
        self.start_btn.grid(row=1, column=0, columnspan=2, pady=8, sticky="ew")
        self.stop_btn = ttk.Button(controls, text="Stop Logging", command=self.stop_logging, state="disabled")
        self.stop_btn.grid(row=1, column=2, pady=8, sticky="ew")
        ttk.Button(controls, text="Test Sensors", command=self.test_sensors).grid(row=1, column=3, pady=8, sticky="ew")
        ttk.Button(controls, text="Open Crash Logs", command=self.open_logs).grid(row=1, column=4, pady=8, sticky="ew")
        controls.columnconfigure(3, weight=1)

        sensor_box = ttk.LabelFrame(outer, text="Sensor Sources", padding=10)
        sensor_box.pack(fill="x", pady=(10, 0))
        ttk.Label(sensor_box, textvariable=self.nvidia_status, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(sensor_box, textvariable=self.lhm_status, font=("Segoe UI", 10, "bold")).pack(anchor="w")

        row = ttk.Frame(sensor_box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Locate LHM Log Folder", command=self.locate_lhm).pack(side="left")
        ttk.Label(row, textvariable=self.lhm_folder).pack(side="left", padx=8)

        ttk.Label(
            sensor_box,
            text="LibreHardwareMonitor logging must be ON. v1.4 follows its newest LibreHardwareMonitorLog-*.csv automatically, including when LHM creates a new file.",
            wraplength=1020,
        ).pack(anchor="w", pady=(5, 0))

        ttk.Label(outer, textvariable=self.status, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=10)

        live = ttk.LabelFrame(outer, text="Live Values", padding=10)
        live.pack(fill="both", expand=True)

        fields = [
            ("CPU Usage", "cpu.usage_percent", "%"),
            ("CPU Temperature", "cpu.temperature_c", "°C"),
            ("CPU Package Power", "cpu.package_power_w", "W"),
            ("CPU Core Voltage", "cpu.core_voltage_v", "V"),
            ("CPU Frequency", "cpu.frequency_mhz", "MHz"),
            ("RAM Usage", "ram.usage_percent", "%"),
            ("GPU Usage", "gpu.usage_percent", "%"),
            ("GPU Temperature", "gpu.temperature_c", "°C"),
            ("GPU Hotspot", "gpu.hotspot_temperature_c", "°C"),
            ("GPU Power (NVML)", "gpu.power_w", "W"),
            ("GPU Power (LHM)", "gpu.lhm_power_w", "W"),
            ("GPU Voltage", "gpu.voltage_v", "V"),
            ("GPU Fan", "gpu.fan_percent", "%"),
            ("GPU Fan RPM", "gpu.fan_rpm", "RPM"),
            ("GPU Core Clock", "gpu.core_clock_mhz", "MHz"),
            ("GPU VRAM Used", "gpu.vram_used_mb", "MB"),
            ("PCIe Generation", "gpu.pcie_generation", ""),
            ("PCIe Width", "gpu.pcie_width", ""),
            ("12V Sensor", "board.12v_v", "V"),
            ("5V Sensor", "board.5v_v", "V"),
            ("3.3V Sensor", "board.3_3v_v", "V"),
            ("AIO Pump", "cooling.aio_pump_rpm", "RPM"),
            ("CPU Fan", "cooling.cpu_fan_rpm", "RPM"),
            ("Board Fan #1", "board.fan_1_rpm", "RPM"),
            ("Board Fan #2", "board.fan_2_rpm", "RPM"),
            ("LHM Sample Time", "lhm.sample_time", ""),
            ("Warnings", "warnings", ""),
        ]

        for i, (label, key, suffix) in enumerate(fields):
            rr = i // 2
            cc = (i % 2) * 2
            ttk.Label(live, text=label + ":").grid(row=rr, column=cc, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value="—")
            self.values[key] = (var, suffix)
            ttk.Label(live, textvariable=var, font=("Consolas", 10, "bold")).grid(row=rr, column=cc + 1, sticky="w")

        live.columnconfigure(1, weight=1)
        live.columnconfigure(3, weight=1)

        ttk.Label(
            outer,
            text="After a hard freeze: restart Windows, then send the newest Documents\\PC_Crash_Monitor\\session_* folder. The final completed sample is flushed to disk every second.",
            wraplength=1020,
        ).pack(fill="x", pady=(8, 0))

    def browse_output(self):
        x = filedialog.askdirectory(initialdir=self.output.get())
        if x:
            self.output.set(x)

    def locate_lhm(self):
        x = filedialog.askdirectory(title="Select the folder where LibreHardwareMonitorLog-*.csv files are appearing")
        if x:
            self.lhm_folder.set(x)
            self.test_sensors()

    def make_reader(self):
        selected = Path(self.lhm_folder.get()) if self.lhm_folder.get() else None
        return LHMCSVReader(selected)

    def test_sensors(self):
        self.status.set("Testing NVIDIA and LibreHardwareMonitor CSV...")

        def work():
            nv = NvidiaReader()
            lhm = self.make_reader()
            lhm_data = lhm.sample()

            preview = {
                "cpu.temperature_c": lhm_data.get("cpu.temperature_c"),
                "cpu.package_power_w": lhm_data.get("cpu.package_power_w"),
                "cpu.core_voltage_v": lhm_data.get("cpu.core_voltage_v"),
                "gpu.hotspot_temperature_c": lhm_data.get("gpu.hotspot_temperature_c"),
                "gpu.voltage_v": lhm_data.get("gpu.voltage_v"),
                "gpu.lhm_power_w": lhm_data.get("gpu.lhm_power_w"),
                "gpu.fan_rpm": lhm_data.get("gpu.fan_rpm"),
                "board.12v_v": lhm_data.get("board.12v_v"),
                "board.5v_v": lhm_data.get("board.5v_v"),
                "board.3_3v_v": lhm_data.get("board.3_3v_v"),
                "cooling.aio_pump_rpm": lhm_data.get("cooling.aio_pump_rpm"),
                "cooling.cpu_fan_rpm": lhm_data.get("cooling.cpu_fan_rpm"),
                "board.fan_1_rpm": lhm_data.get("board.fan_1_rpm"),
                "board.fan_2_rpm": lhm_data.get("board.fan_2_rpm"),
                "lhm.sample_time": lhm_data.get("sample_time"),
            }

            self.q.put({
                "type": "test",
                "nvidia": nv.status(),
                "lhm": lhm.status(),
                "preview": preview,
                "count": lhm_data.get("sensor_count", 0),
                "error": lhm_data.get("error"),
            })

        threading.Thread(target=work, daemon=True).start()

    def start_logging(self):
        try:
            interval = float(self.interval.get())
        except Exception:
            messagebox.showerror("Invalid interval", "Enter a numeric sample interval.")
            return

        selected = Path(self.lhm_folder.get()) if self.lhm_folder.get() else None
        output = Path(self.output.get()).expanduser()
        output.mkdir(parents=True, exist_ok=True)

        self.logger = CrashLogger(interval, output, selected, self.q)
        self.worker = threading.Thread(target=self.logger.run, daemon=True)
        self.worker.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set("Logging — minimize this window and test the game.")

    def stop_logging(self):
        if self.logger:
            self.logger.stop()

    def open_logs(self):
        p = Path(self.output.get()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    def update_values(self, row):
        for key, (var, suffix) in self.values.items():
            value = row.get(key)
            if value is None or value == "":
                text = "—"
            elif isinstance(value, float):
                text = f"{value:.2f}"
            else:
                text = str(value)
            if suffix and text != "—":
                text += " " + suffix
            var.set(text)

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item.get("type")

                if kind == "sample":
                    self.nvidia_status.set("NVIDIA GPU: " + item["nvidia"])
                    self.lhm_status.set("LibreHardwareMonitor CSV: " + item["lhm"])
                    self.status.set(f"Logging sample {item['count']:,} — {item['session']}")
                    self.update_values(item["row"])

                elif kind == "test":
                    self.nvidia_status.set("NVIDIA GPU: " + item["nvidia"])
                    self.lhm_status.set("LibreHardwareMonitor CSV: " + item["lhm"])
                    self.update_values(item["preview"])
                    if item.get("error"):
                        self.status.set("Sensor test failed: " + str(item["error"]))
                    else:
                        self.status.set(f"Sensor test passed — {item['count']} LHM sensor columns detected.")

                elif kind == "error":
                    self.status.set(item.get("message", "Logger error"))

                elif kind == "stopped":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.set("Logging stopped.")

        except queue.Empty:
            pass
        self.root.after(250, self.poll)

    def close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Logger running", "Stop logging and close? Minimize instead during crash testing."):
                return
            self.logger.stop()
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
