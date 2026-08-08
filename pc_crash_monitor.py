"""
PC Crash Monitor v1.3
Windows gaming crash telemetry logger.

Key v1.3 change:
LibreHardwareMonitor sensors are now read DIRECTLY from LibreHardwareMonitorLib.dll
using pythonnet. The app automatically looks beside a running
LibreHardwareMonitor.exe, in the app folder, Downloads, Desktop, and common
LibreHardwareMonitor folders.

WMI is retained only as a fallback.
"""

from __future__ import annotations
import csv, datetime as dt, json, os, platform, queue, re, shutil, subprocess, sys, threading, time, traceback
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
VERSION = "1.3.0"
LOG_ROOT = Path.home() / "Documents" / "PC_Crash_Monitor"
STATE_FILE = LOG_ROOT / "last_session.json"

def now_local(): return dt.datetime.now().astimezone()
def iso_now(): return now_local().isoformat(timespec="milliseconds")
def sf(v):
    try:
        return None if v is None or v == "" else float(v)
    except Exception:
        return None
def flatten(prefix, obj, out):
    for k,v in obj.items():
        key=f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v,dict): flatten(key,v,out)
        else: out[key]=v

class NvidiaNVML:
    def __init__(self):
        self.ok=False; self.error=None; self.pynvml=None; self.handle=None
        try:
            import pynvml
            self.pynvml=pynvml; pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount()<1: raise RuntimeError("No NVIDIA GPU detected")
            self.handle=pynvml.nvmlDeviceGetHandleByIndex(0); self.ok=True
        except Exception as e: self.error=f"{type(e).__name__}: {e}"
    def available(self): return self.ok
    def sample(self):
        if not self.ok: return {"available":False,"source":"NVML","error":self.error}
        n,h=self.pynvml,self.handle; out={"available":True,"source":"NVML"}
        def g(name,func,div=1.0):
            try:
                v=func()
                if isinstance(v,bytes): v=v.decode(errors="replace")
                out[name]=v/div if isinstance(v,(int,float)) else v
            except Exception: out[name]=None
        g("name",lambda:n.nvmlDeviceGetName(h))
        g("driver_version",lambda:n.nvmlSystemGetDriverVersion())
        g("temperature_gpu",lambda:n.nvmlDeviceGetTemperature(h,n.NVML_TEMPERATURE_GPU))
        g("power_draw",lambda:n.nvmlDeviceGetPowerUsage(h),1000.0)
        g("power_limit",lambda:n.nvmlDeviceGetEnforcedPowerLimit(h),1000.0)
        g("fan_speed",lambda:n.nvmlDeviceGetFanSpeed(h))
        g("clocks_current_graphics",lambda:n.nvmlDeviceGetClockInfo(h,n.NVML_CLOCK_GRAPHICS))
        g("clocks_current_memory",lambda:n.nvmlDeviceGetClockInfo(h,n.NVML_CLOCK_MEM))
        try:
            u=n.nvmlDeviceGetUtilizationRates(h); out["utilization_gpu"]=float(u.gpu); out["utilization_memory"]=float(u.memory)
        except Exception: out["utilization_gpu"]=out["utilization_memory"]=None
        try:
            m=n.nvmlDeviceGetMemoryInfo(h); out["memory_used"]=m.used/1048576; out["memory_total"]=m.total/1048576
        except Exception: out["memory_used"]=out["memory_total"]=None
        try: out["pcie_link_gen_current"]=float(n.nvmlDeviceGetCurrPcieLinkGeneration(h))
        except Exception: out["pcie_link_gen_current"]=None
        try: out["pcie_link_width_current"]=float(n.nvmlDeviceGetCurrPcieLinkWidth(h))
        except Exception: out["pcie_link_width_current"]=None
        out["voltage_graphics"]=None
        return out

class NvidiaSMI:
    FIELDS=["name","driver_version","utilization.gpu","utilization.memory","temperature.gpu","power.draw","power.limit","clocks.current.graphics","clocks.current.memory","fan.speed","memory.used","memory.total","pcie.link.gen.current","pcie.link.width.current"]
    def __init__(self):
        self.exe=None
        for c in [shutil.which("nvidia-smi"),r"C:\Windows\System32\nvidia-smi.exe",r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]:
            if c and Path(c).exists(): self.exe=str(c); break
    def available(self): return bool(self.exe)
    def sample(self):
        if not self.exe:return {"available":False,"source":"nvidia-smi","error":"not found"}
        try:
            cp=subprocess.run([self.exe,"--query-gpu="+",".join(self.FIELDS),"--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW)
            if cp.returncode:return {"available":True,"source":"nvidia-smi","error":cp.stderr.strip()}
            vals=next(csv.reader([cp.stdout.strip().splitlines()[0]],skipinitialspace=True)); out={"available":True,"source":"nvidia-smi"}
            for f,v in zip(self.FIELDS,vals):
                v=v.strip(); out[f.replace(".","_")]=None if v in ("","N/A","[Not Supported]") else (v if f in ("name","driver_version") else sf(v))
            return out
        except Exception as e:return {"available":True,"source":"nvidia-smi","error":str(e)}

class NvidiaReader:
    def __init__(self): self.nvml=NvidiaNVML(); self.smi=NvidiaSMI()
    def status(self):
        if self.nvml.available():return "Connected (NVML)"
        if self.smi.available():return "Connected (nvidia-smi fallback)"
        return "Not connected"
    def sample(self):
        if self.nvml.available(): return self.nvml.sample()
        return self.smi.sample()

class DirectLHM:
    def __init__(self, manual_folder=None):
        self.computer=None; self.error=None; self.dll_path=None; self.manual_folder=manual_folder
        self._connect()
    def _candidate_dlls(self):
        out=[]
        if self.manual_folder: out.append(Path(self.manual_folder)/"LibreHardwareMonitorLib.dll")
        # Best source: folder of a running LibreHardwareMonitor process.
        if psutil:
            try:
                for p in psutil.process_iter(["name","exe"]):
                    if (p.info.get("name") or "").lower() in ("librehardwaremonitor.exe","librehardwaremonitor.net10.exe"):
                        exe=p.info.get("exe")
                        if exe: out.append(Path(exe).parent/"LibreHardwareMonitorLib.dll")
            except Exception: pass
        here=Path(sys.executable).parent
        out += [
            here/"LibreHardwareMonitorLib.dll",
            Path.cwd()/"LibreHardwareMonitorLib.dll",
            Path.home()/"Downloads"/"LibreHardwareMonitor"/"LibreHardwareMonitorLib.dll",
            Path.home()/"Desktop"/"LibreHardwareMonitor"/"LibreHardwareMonitorLib.dll",
            Path.home()/"Documents"/"LibreHardwareMonitor"/"LibreHardwareMonitorLib.dll",
        ]
        # shallow Downloads search
        try:
            out += list((Path.home()/"Downloads").glob("LibreHardwareMonitor*/LibreHardwareMonitorLib.dll"))
        except Exception: pass
        seen=[]
        for x in out:
            try:x=x.resolve()
            except Exception:pass
            if x not in seen:seen.append(x)
        return seen
    def _connect(self):
        self.computer=None; self.error=None; self.dll_path=None
        try:
            dll=next((p for p in self._candidate_dlls() if p.exists()),None)
            if not dll:
                raise FileNotFoundError("LibreHardwareMonitorLib.dll not found. Run LibreHardwareMonitor, or use 'Locate LHM Folder'.")
            self.dll_path=str(dll)
            import clr
            clr.AddReference(str(dll))
            from LibreHardwareMonitor.Hardware import Computer
            c=Computer()
            c.IsCpuEnabled=True; c.IsGpuEnabled=True; c.IsMemoryEnabled=True
            c.IsMotherboardEnabled=True; c.IsControllerEnabled=True; c.IsStorageEnabled=True
            try:c.IsPowerMonitorEnabled=True
            except Exception:pass
            c.Open()
            self.computer=c
        except Exception as e:self.error=f"{type(e).__name__}: {e}"
    def retry(self):
        if not self.computer:self._connect()
        return self.available()
    def available(self):return self.computer is not None
    def set_folder(self,folder):
        self.manual_folder=folder
        try:
            if self.computer:self.computer.Close()
        except Exception:pass
        self._connect()
    def _walk(self,hw,results):
        try: hw.Update()
        except Exception: pass
        for s in list(hw.Sensors):
            try:
                if s.Value is not None:
                    results.append({"hardware":str(hw.Name),"hardware_type":str(hw.HardwareType),"name":str(s.Name),"sensor_type":str(s.SensorType),"value":float(s.Value),"identifier":str(s.Identifier)})
            except Exception: pass
        for sub in list(hw.SubHardware):
            self._walk(sub,results)
    def all_sensors(self):
        if not self.computer:return []
        out=[]
        try:
            for hw in list(self.computer.Hardware): self._walk(hw,out)
        except Exception as e:self.error=f"{type(e).__name__}: {e}"
        return out
    def sample(self):
        sensors=self.all_sensors()
        if not self.available():return {"available":False,"error":self.error}
        out={"available":True,"dll_path":self.dll_path}
        for s in sensors:
            key=f"{s['hardware']}|{s['sensor_type']}|{s['name']}"
            out[key]=s["value"]
        return out

class WmiLHM:
    def __init__(self):
        self.conn=None;self.error=None
        try:
            import wmi
            self.conn=wmi.WMI(namespace=r"root\LibreHardwareMonitor")
        except Exception as e:self.error=f"{type(e).__name__}: {e}"
    def available(self):return self.conn is not None
    def all_sensors(self):
        if not self.conn:return []
        out=[]
        try:
            for s in self.conn.Sensor():
                v=sf(getattr(s,"Value",None))
                if v is not None:out.append({"hardware":"WMI","hardware_type":"","name":str(s.Name),"sensor_type":str(s.SensorType),"value":v,"identifier":str(getattr(s,"Identifier",""))})
        except Exception as e:self.error=str(e)
        return out

class HardwareReader:
    def __init__(self,folder=None):
        self.direct=DirectLHM(folder); self.wmi=WmiLHM()
    def set_folder(self,folder):self.direct.set_folder(folder)
    def available(self):return self.direct.available() or self.wmi.available()
    def source(self):
        if self.direct.available():return f"Direct DLL ({self.direct.dll_path})"
        if self.wmi.available():return "WMI fallback"
        return "Not connected"
    def error(self):return self.direct.error or self.wmi.error
    def all_sensors(self):
        x=self.direct.all_sensors()
        return x if x else self.wmi.all_sensors()
    def sample(self):
        sensors=self.all_sensors()
        out={"available":self.available(),"source":self.source(),"error":self.error()}
        for s in sensors:
            out[f"{s['hardware']}|{s['sensor_type']}|{s['name']}"]=s["value"]
        return out

def standardize_hw(sensors):
    out={}
    def first(stype,patterns,hwpatterns=None):
        for s in sensors:
            if s["sensor_type"].lower()!=stype.lower():continue
            text=f"{s['hardware']} {s['name']}"
            if hwpatterns and not any(re.search(p,text,re.I) for p in hwpatterns):continue
            if any(re.search(p,text,re.I) for p in patterns):return s["value"]
        return None
    out["cpu.package_temperature_c"]=first("Temperature",[r"Tctl/Tdie",r"CPU Package",r"CPU Die",r"CPU Core"],[r"Ryzen",r"CPU"])
    out["cpu.package_power_w"]=first("Power",[r"Package",r"CPU Package"],[r"Ryzen",r"CPU"])
    out["cpu.core_voltage_v"]=first("Voltage",[r"Core \(SVI",r"Vcore",r"CPU Core"])
    out["gpu.hotspot_temperature_c"]=first("Temperature",[r"Hot Spot",r"Hotspot"],[r"GPU",r"NVIDIA",r"GeForce"])
    out["gpu.memory_temperature_c"]=first("Temperature",[r"Memory"],[r"GPU",r"NVIDIA",r"GeForce"])
    out["gpu.graphics_voltage_v"]=first("Voltage",[r"GPU Core",r"Core Voltage"],[r"GPU",r"NVIDIA",r"GeForce"])
    out["psu.12v_sensor_v"]=first("Voltage",[r"^\+?12V$",r"\b12V\b"])
    out["psu.5v_sensor_v"]=first("Voltage",[r"^\+?5V$",r"\b5V\b"])
    out["psu.3_3v_sensor_v"]=first("Voltage",[r"^\+?3\.3V$",r"\b3\.3V\b",r"\bAVCC\b"])
    out["aio.pump_rpm"]=first("Fan",[r"AIO",r"Pump",r"Water"])
    out["cpu.fan_rpm"]=first("Fan",[r"CPU Fan",r"Fan #1"])
    return {k:v for k,v in out.items() if v is not None}

class EventCollector:
    def collect(self,path,hours=24):
        if os.name!="nt":return
        ps=f"""$s=(Get-Date).AddHours(-{hours}); Get-WinEvent -FilterHashtable @{{LogName=@('System','Application');StartTime=$s}} -ErrorAction SilentlyContinue | Where-Object {{$_.LevelDisplayName -in @('Critical','Error','Warning') -or $_.Id -in @(41,18,19,20,4101,1000,1001,6008,14)}} | Select TimeCreated,Id,LevelDisplayName,ProviderName,LogName,Message | ConvertTo-Json -Depth 4"""
        try:
            cp=subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command",ps],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            path.write_text(cp.stdout.strip() or "[]",encoding="utf-8")
        except Exception as e:path.write_text(json.dumps({"error":str(e)}),encoding="utf-8")

class Monitor:
    def __init__(self,interval,base,q,lhm_folder=None):
        self.interval=max(.25,float(interval));self.base=base;self.q=q;self.stop_evt=threading.Event()
        self.nv=NvidiaReader();self.hw=HardwareReader(lhm_folder);self.events=EventCollector()
        sid=now_local().strftime("%Y%m%d_%H%M%S");self.dir=base/f"session_{sid}";self.dir.mkdir(parents=True,exist_ok=True)
        self.csvp=self.dir/"telemetry.csv";self.lastp=self.dir/"last_sample.json";self.diagp=self.dir/"sensor_diagnostics.json";self.eventp=self.dir/"recent_windows_events.json"
        self.f=None;self.writer=None;self.fields=None;self.count=0
    def diagnostics(self):
        sensors=self.hw.all_sensors()
        d={"timestamp":iso_now(),"version":VERSION,"nvidia_status":self.nv.status(),"hardware_status":self.hw.source(),"hardware_error":self.hw.error(),"sensor_count":len(sensors),"hardware_sensors":sensors,"nvidia_sample":self.nv.sample()}
        self.diagp.write_text(json.dumps(d,indent=2,default=str),encoding="utf-8");return d
    def sample(self):
        r={"timestamp":iso_now(),"epoch":time.time(),"sample_number":self.count+1}
        if psutil:
            r["cpu.total_usage_percent"]=psutil.cpu_percent(None)
            fr=psutil.cpu_freq()
            if fr:r["cpu.frequency_current_mhz"]=fr.current
            vm=psutil.virtual_memory();r["memory.usage_percent"]=vm.percent;r["memory.used_mb"]=vm.used/1048576;r["memory.total_mb"]=vm.total/1048576
        g=self.nv.sample();flatten("nvidia",g,r)
        mp={"gpu.core_temperature_c":"nvidia.temperature_gpu","gpu.core_load_percent":"nvidia.utilization_gpu","gpu.memory_load_percent":"nvidia.utilization_memory","gpu.power_w":"nvidia.power_draw","gpu.power_limit_w":"nvidia.power_limit","gpu.fan_percent":"nvidia.fan_speed","gpu.core_clock_mhz":"nvidia.clocks_current_graphics","gpu.memory_clock_mhz":"nvidia.clocks_current_memory","gpu.memory_used_mb":"nvidia.memory_used","gpu.memory_total_mb":"nvidia.memory_total","gpu.pcie_generation":"nvidia.pcie_link_gen_current","gpu.pcie_width":"nvidia.pcie_link_width_current"}
        for a,b in mp.items():
            if r.get(b) is not None:r[a]=r[b]
        sensors=self.hw.all_sensors(); r.update(standardize_hw(sensors))
        # keep all raw hardware values in CSV using stable sensor IDs where possible
        for s in sensors:
            key="hw."+re.sub(r"[^A-Za-z0-9_.-]+","_",f"{s['hardware']}.{s['sensor_type']}.{s['name']}")[:120]
            r[key]=s["value"]
        warns=[]
        if sf(r.get("cpu.package_temperature_c")) is not None and r["cpu.package_temperature_c"]>=90:warns.append(f"CPU_TEMP_HIGH:{r['cpu.package_temperature_c']:.1f}C")
        if sf(r.get("gpu.core_temperature_c")) is not None and r["gpu.core_temperature_c"]>=83:warns.append(f"GPU_TEMP_HIGH:{r['gpu.core_temperature_c']:.1f}C")
        for k,lo,hi,label in [("psu.12v_sensor_v",11.4,12.6,"12V"),("psu.5v_sensor_v",4.75,5.25,"5V"),("psu.3_3v_sensor_v",3.135,3.465,"3.3V")]:
            v=sf(r.get(k))
            if v is not None and not lo<=v<=hi:warns.append(f"{label}_OUT_OF_RANGE:{v:.3f}V")
        r["warnings"]=" | ".join(warns);return r
    def write(self,r):
        if self.writer is None:
            self.fields=list(r.keys());self.f=self.csvp.open("w",newline="",encoding="utf-8-sig",buffering=1);self.writer=csv.DictWriter(self.f,fieldnames=self.fields,extrasaction="ignore");self.writer.writeheader()
        self.writer.writerow(r);self.f.flush();os.fsync(self.f.fileno());self.lastp.write_text(json.dumps(r,indent=2,default=str),encoding="utf-8")
    def run(self):
        self.diagnostics();self.events.collect(self.eventp)
        if psutil:psutil.cpu_percent(None)
        try:
            while not self.stop_evt.is_set():
                t=time.monotonic()
                try:
                    r=self.sample();self.write(r);self.count+=1;self.q.put({"type":"sample","row":r,"count":self.count,"gpu":self.nv.status(),"hw":self.hw.source(),"dir":str(self.dir)})
                except Exception as e:self.q.put({"type":"error","message":str(e)})
                self.stop_evt.wait(max(0,self.interval-(time.monotonic()-t)))
        finally:
            if self.f:self.f.flush();os.fsync(self.f.fileno());self.f.close()
            self.q.put({"type":"stopped"})
    def stop(self):self.stop_evt.set()

class App:
    def __init__(self,root):
        self.root=root;root.title(f"{APP_NAME} {VERSION}");root.geometry("1030x770")
        self.q=queue.Queue();self.mon=None;self.worker=None
        self.interval=tk.StringVar(value="1.0");self.folder=tk.StringVar(value=str(LOG_ROOT));self.lhm_folder=tk.StringVar(value="")
        self.status=tk.StringVar(value="Ready");self.gpust=tk.StringVar(value="NVIDIA GPU: Not tested");self.hwst=tk.StringVar(value="Hardware Sensors: Not tested")
        self.vals={}
        f=ttk.Frame(root,padding=12);f.pack(fill="both",expand=True)
        c=ttk.LabelFrame(f,text="Logging Controls",padding=10);c.pack(fill="x")
        ttk.Label(c,text="Sample interval:").grid(row=0,column=0);ttk.Entry(c,textvariable=self.interval,width=7).grid(row=0,column=1,padx=5)
        ttk.Label(c,text="Log folder:").grid(row=0,column=2);ttk.Entry(c,textvariable=self.folder,width=52).grid(row=0,column=3,padx=5,sticky="ew");ttk.Button(c,text="Browse",command=self.browse).grid(row=0,column=4)
        self.start=ttk.Button(c,text="Start Logging",command=self.startlog);self.start.grid(row=1,column=0,columnspan=2,pady=7,sticky="ew")
        self.stop=ttk.Button(c,text="Stop Logging",command=self.stoplog,state="disabled");self.stop.grid(row=1,column=2,pady=7,sticky="ew")
        ttk.Button(c,text="Test Sensors",command=self.test).grid(row=1,column=3,pady=7,sticky="ew");ttk.Button(c,text="Open Logs",command=self.openlogs).grid(row=1,column=4,pady=7,sticky="ew");c.columnconfigure(3,weight=1)
        s=ttk.LabelFrame(f,text="Sensor Status",padding=10);s.pack(fill="x",pady=8)
        ttk.Label(s,textvariable=self.gpust,font=("Segoe UI",10,"bold")).pack(anchor="w");ttk.Label(s,textvariable=self.hwst,font=("Segoe UI",10,"bold")).pack(anchor="w")
        row=ttk.Frame(s);row.pack(fill="x",pady=(5,0));ttk.Button(row,text="Locate LibreHardwareMonitor Folder",command=self.locatelhm).pack(side="left")
        ttk.Label(row,textvariable=self.lhm_folder).pack(side="left",padx=8)
        ttk.Label(f,textvariable=self.status,font=("Segoe UI",11,"bold")).pack(anchor="w",pady=5)
        live=ttk.LabelFrame(f,text="Live Values",padding=10);live.pack(fill="both",expand=True)
        fields=[("CPU Usage","cpu.total_usage_percent","%"),("CPU Temperature","cpu.package_temperature_c","°C"),("CPU Package Power","cpu.package_power_w","W"),("CPU Frequency","cpu.frequency_current_mhz","MHz"),("RAM Usage","memory.usage_percent","%"),("GPU Usage","gpu.core_load_percent","%"),("GPU Temperature","gpu.core_temperature_c","°C"),("GPU Hotspot","gpu.hotspot_temperature_c","°C"),("GPU Power","gpu.power_w","W"),("GPU Voltage","gpu.graphics_voltage_v","V"),("GPU Fan","gpu.fan_percent","%"),("GPU Core Clock","gpu.core_clock_mhz","MHz"),("GPU VRAM Used","gpu.memory_used_mb","MB"),("PCIe Generation","gpu.pcie_generation",""),("PCIe Width","gpu.pcie_width",""),("12V Sensor","psu.12v_sensor_v","V"),("5V Sensor","psu.5v_sensor_v","V"),("3.3V Sensor","psu.3_3v_sensor_v","V"),("AIO Pump","aio.pump_rpm","RPM"),("CPU Fan","cpu.fan_rpm","RPM"),("Warnings","warnings","")]
        for i,(lab,key,suf) in enumerate(fields):
            rr=i//2;cc=(i%2)*2;ttk.Label(live,text=lab+":").grid(row=rr,column=cc,sticky="w",padx=(0,8),pady=4);v=tk.StringVar(value="—");self.vals[key]=(v,suf);ttk.Label(live,textvariable=v,font=("Consolas",10,"bold")).grid(row=rr,column=cc+1,sticky="w")
        live.columnconfigure(1,weight=1);live.columnconfigure(3,weight=1)
        ttk.Label(f,text="v1.3 reads LibreHardwareMonitorLib.dll directly. Keep LibreHardwareMonitor open as Administrator, then click Test Sensors. If auto-detection fails, use Locate LibreHardwareMonitor Folder.",wraplength=980).pack(fill="x",pady=8)
        root.after(250,self.poll);root.protocol("WM_DELETE_WINDOW",self.close)
    def browse(self):
        x=filedialog.askdirectory(initialdir=self.folder.get())
        if x:self.folder.set(x)
    def locatelhm(self):
        x=filedialog.askdirectory(title="Select the folder containing LibreHardwareMonitor.exe and LibreHardwareMonitorLib.dll")
        if x:self.lhm_folder.set(x);self.test()
    def mkmon(self):return Monitor(float(self.interval.get()),Path(self.folder.get()).expanduser(),self.q,self.lhm_folder.get() or None)
    def test(self):
        self.status.set("Testing sensors...")
        def w():
            m=self.mkmon();d=m.diagnostics();r=m.sample();self.q.put({"type":"test","row":r,"gpu":m.nv.status(),"hw":m.hw.source(),"diag":str(m.diagp),"count":d.get("sensor_count",0),"err":m.hw.error()})
        threading.Thread(target=w,daemon=True).start()
    def startlog(self):
        self.mon=self.mkmon();self.worker=threading.Thread(target=self.mon.run,daemon=True);self.worker.start();self.start.config(state="disabled");self.stop.config(state="normal")
    def stoplog(self):
        if self.mon:self.mon.stop()
    def openlogs(self):
        p=Path(self.folder.get());p.mkdir(parents=True,exist_ok=True);os.startfile(p)
    def updatevals(self,r):
        for k,(v,suf) in self.vals.items():
            x=r.get(k);txt="—" if x is None or x=="" else (f"{x:.2f}" if isinstance(x,float) else str(x));v.set(txt+((" "+suf) if suf and txt!="—" else ""))
    def poll(self):
        try:
            while True:
                x=self.q.get_nowait();typ=x["type"]
                if typ in ("sample","test"):
                    self.gpust.set("NVIDIA GPU: "+x["gpu"]);self.hwst.set("Hardware Sensors: "+x["hw"]);self.updatevals(x["row"])
                    if typ=="test":self.status.set(f"Sensor test: {x['count']} hardware sensors detected. {x['diag']}" + (f" | {x['err']}" if x.get("err") else ""))
                    else:self.status.set(f"Logging sample {x['count']:,} — {x['dir']}")
                elif typ=="stopped":self.start.config(state="normal");self.stop.config(state="disabled");self.status.set("Logging stopped")
                elif typ=="error":self.status.set("Logger error: "+x["message"])
        except queue.Empty:pass
        self.root.after(250,self.poll)
    def close(self):
        if self.worker and self.worker.is_alive() and not messagebox.askyesno("Logger running","Stop logging and close?"):return
        if self.mon:self.mon.stop()
        self.root.destroy()

def main():
    if tk is None:return 1
    root=tk.Tk();App(root);root.mainloop();return 0
if __name__=="__main__":raise SystemExit(main())
