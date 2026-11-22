"""Memory Optimizer for Windows. Local diagnostics; no remote services."""
import ctypes
import base64
from ctypes import wintypes
import datetime as dt
import gc
import functools
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import psutil
import msvcrt

ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MemoryOptimizer'
ROOT.mkdir(parents=True, exist_ok=True)
JOURNAL = ROOT / 'rollback.json'
BG, PANEL, TEXT, MUTED, GREEN = '#0c1220', '#141e30', '#e7eefb', '#99aac4', '#5ce1bb'

def powershell(script, timeout=45):
    r = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        "$ErrorActionPreference='Stop';" + script], capture_output=True,
                       text=True, timeout=timeout, creationflags=0x08000000)
    if r.returncode:
        raise RuntimeError((r.stderr.strip() or r.stdout.strip() or 'Windows command failed')[-1800:])
    return json.loads(r.stdout.strip()) if r.stdout.strip() else None

def save_json(path, value):
    tmp = path.with_suffix('.new')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def exclusive_operation(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with JOURNAL.with_suffix('.lock').open('a+b') as lock:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Another optimization or rollback is running. Wait for it to finish.')
            try: return fn(*args, **kwargs)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return run

def gb(n): return f'{n / 1024**3:.2f} GB'
def mb(n): return f'{n / 1024**2:,.0f} MB'

class Performance(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in
        ('CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal', 'PhysicalAvailable',
         'SystemCache', 'KernelTotal', 'KernelPaged', 'KernelNonpaged', 'PageSize')] + [
        ('HandleCount', wintypes.DWORD), ('ProcessCount', wintypes.DWORD), ('ThreadCount', wintypes.DWORD)]

def performance():
    p = Performance()
    p.cb = ctypes.sizeof(p)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(p), p.cb):
        raise ctypes.WinError()
    return {k: getattr(p, k) * p.PageSize for k in ('CommitTotal', 'CommitLimit', 'SystemCache')}

def settings():
    return powershell("$c=Get-CimInstance Win32_ComputerSystem; $m=Get-MMAgent; "
                      "if($null -eq $c.AutomaticManagedPagefile -or $null -eq $m.MemoryCompression){throw 'Windows memory settings are unavailable'}; "
                      "$p=Get-ItemProperty -LiteralPath 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management' -Name PagingFiles; "
                      "@{pageAuto=[bool]$c.AutomaticManagedPagefile;compression=[bool]$m.MemoryCompression;pagingFiles=@($p.PagingFiles)}|ConvertTo-Json")

def pressure(ram, commit, pages):
    # Transparent heuristic, not a Windows or hardware health diagnosis.
    score = max(ram, commit)
    if pages is not None:
        score += min(15, pages / 20)
    return min(100, round(score))

def suspected_growth(points):
    if len(points) < 10 or points[-1][0] - points[0][0] < 300:
        return False
    growth = points[-1][1] - points[0][1]
    rises = sum(b[1] > a[1] for a, b in zip(points, points[1:]))
    return growth > 256 * 1024**2 and growth > points[0][1] * .5 and rises >= len(points) * .7

def restore_point():
    # Checkpoint-Computer can be throttled: require a NEW sequence number.
    return powershell("$old=@(Get-ComputerRestorePoint); "
        "$max=($old|Measure-Object SequenceNumber -Maximum).Maximum; "
        "Checkpoint-Computer -Description 'Memory Optimizer before changes' -RestorePointType MODIFY_SETTINGS -WarningAction Stop; "
        "$new=Get-ComputerRestorePoint|Where-Object {$_.SequenceNumber -gt $max}|Sort-Object SequenceNumber -Descending|Select-Object -First 1; "
        "if(!$new){throw 'A new restore point could not be verified. No settings were changed.'}; "
        "@{sequence=$new.SequenceNumber;description=$new.Description}|ConvertTo-Json", 180)

@exclusive_operation
def optimize(before):
    if JOURNAL.exists():
        old = json.loads(JOURNAL.read_text(encoding='utf-8'))
        if old.get('status') != 'rolled_back' and old.get('changes'):
            raise RuntimeError('A rollback record is still active. Roll it back before another optimization.')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Run this app as administrator to create a restore point and apply settings. Monitoring works without elevation.')
    state = settings()
    checkpoint = restore_point()
    state = settings()  # Capture the rollback baseline after restore-point creation.
    journal = {'version': 1, 'created': dt.datetime.now().isoformat(), 'before': state,
               'restorePoint': checkpoint, 'changes': [], 'status': 'in_progress'}
    save_json(JOURNAL, journal)
    actions = []
    for key, script, label in [
        ('compression', 'Enable-MMAgent -MemoryCompression', 'Enabled Windows memory compression'),
        ('pageAuto', 'Get-CimInstance Win32_ComputerSystem | Set-CimInstance -Property @{AutomaticManagedPagefile=$true}',
         'Enabled system-managed page file (restart may be required)')]:
        if not state[key]:
            # Write-ahead record makes an interrupted operation recoverable.
            journal['changes'].append({'key': key, 'before': False, 'applied': True, 'status': 'pending'})
            save_json(JOURNAL, journal)
            try:
                powershell(script)
                if settings()[key] is not True:
                    raise RuntimeError('Windows did not confirm the setting: ' + key)
                journal['changes'][-1]['status'] = 'applied'
                actions.append(label)
            except Exception as exc:
                journal['status'] = 'partial_failure'
                journal['error'] = str(exc)
                save_json(JOURNAL, journal)
                raise RuntimeError(str(exc) + '\nUse Roll back settings to recover any changes already applied.')
            save_json(JOURNAL, journal)
    journal['status'] = 'complete'
    save_json(JOURNAL, journal)
    return actions, checkpoint

@exclusive_operation
def rollback():
    if not JOURNAL.exists():
        raise RuntimeError('No rollback record exists.')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Run as administrator to roll back system settings.')
    j = json.loads(JOURNAL.read_text(encoding='utf-8'))
    if j.get('status') == 'rolled_back': return ['Settings already rolled back.']
    for change in j['changes']:
        if (change.get('key') not in ('compression', 'pageAuto') or
            change.get('before') is not False or change.get('applied') is not True):
            raise RuntimeError('Rollback journal is invalid. No settings were changed.')
    actions = []
    for change in reversed(j['changes']):
        if change.get('status') == 'rolled_back': continue
        key = change['key']
        current_state = settings()
        current = current_state[key]
        entries_match = current_state.get('pagingFiles') == j.get('before', {}).get('pagingFiles')
        if current == change['before'] and (key != 'pageAuto' or entries_match):
            change['status'] = 'rolled_back'
            save_json(JOURNAL, j)
            continue
        if current != change['applied'] and current != change['before']:
            raise RuntimeError('Setting changed externally; rollback stopped to preserve it: ' + key)
        if key == 'compression':
            powershell('Disable-MMAgent -MemoryCompression')
        elif key == 'pageAuto':
            original = j.get('before', {}).get('pagingFiles')
            if not isinstance(original, list) or not all(isinstance(p, str) for p in original):
                raise RuntimeError('Original page-file configuration is unavailable; rollback stopped before changing paging settings.')
            payload = base64.b64encode(json.dumps(original).encode('utf-8')).decode('ascii')
            powershell('Get-CimInstance Win32_ComputerSystem | Set-CimInstance -Property @{AutomaticManagedPagefile=$false}')
            powershell("$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "'))|ConvertFrom-Json; "
                       "Set-ItemProperty -LiteralPath 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management' -Name PagingFiles -Value ([string[]]@($p)) -Type MultiString")
            if settings().get('pagingFiles') != original:
                raise RuntimeError('Original page-file entries were not restored. Keep the rollback journal and retry.')
        else:
            raise RuntimeError('Unknown rollback entry; no action taken.')
        if settings()[key] != change['before']:
            raise RuntimeError('Rollback verification failed for ' + key)
        change['status'] = 'rolled_back'
        save_json(JOURNAL, j)
        actions.append('Restored ' + key)
    j['status'] = 'rolled_back'
    save_json(JOURNAL, j)
    return actions or ['No setting changes required rollback.']

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Memory Optimizer | System care')
        self.root.geometry('1180x850')
        self.root.minsize(960, 720)
        self.root.configure(bg=BG)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.latest = None
        self.history = []
        self.growth = {}
        self.extra = {}
        self.report = None
        self.build()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.monitor, daemon=True).start()
        threading.Thread(target=self.diagnostics, daemon=True).start()
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('Operation in progress', 'Wait for settings changes or rollback to finish before closing.')
            return
        self.stop.set()
        self.root.destroy()

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Treeview', background=PANEL, foreground=TEXT, fieldbackground=PANEL, rowheight=28, borderwidth=0)
        style.configure('Treeview.Heading', background='#22314a', foreground=TEXT)
        style.map('Treeview', background=[('selected', '#2e5068')])
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background=PANEL, foreground=TEXT, padding=(20, 10))
        style.map('TNotebook.Tab', background=[('selected', '#2e5068')])
        shell = tk.Frame(self.root, bg=BG, padx=26, pady=20)
        shell.pack(fill='both', expand=True)
        tk.Label(shell, text='SYSTEM CARE  /  MEMORY', bg=BG, fg=GREEN, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        tk.Label(shell, text='A clearer view. A healthier PC.', bg=BG, fg=TEXT, font=('Segoe UI', 25, 'bold')).pack(anchor='w', pady=(6, 2))
        tk.Label(shell, text='Live Windows diagnostics • Local processing • Protected applications', bg=BG, fg=MUTED, font=('Segoe UI', 11)).pack(anchor='w')
        cards = tk.Frame(shell, bg=BG)
        cards.pack(fill='x', pady=20)
        self.cards = {}
        for i, name in enumerate(['TOTAL RAM', 'USED RAM', 'AVAILABLE', 'PRESSURE']):
            frame = tk.Frame(cards, bg=PANEL, padx=18, pady=13)
            frame.grid(row=0, column=i, sticky='nsew', padx=(0, 10 if i < 3 else 0))
            cards.columnconfigure(i, weight=1)
            tk.Label(frame, text=name, bg=PANEL, fg=MUTED, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
            value = tk.Label(frame, text='Measuring…', bg=PANEL, fg=TEXT, font=('Segoe UI', 21, 'bold'))
            value.pack(anchor='w')
            self.cards[name] = value
        controls = tk.Frame(shell, bg=BG)
        controls.pack(fill='x', pady=(0, 12))
        self.fix = tk.Button(controls, text='FIX 100% DISK & HIGH MEMORY USAGE', command=self.run_optimization,
                             bg=GREEN, fg=BG, activebackground='#89edce', relief='flat', padx=20, pady=12, font=('Segoe UI', 10, 'bold'))
        self.fix.pack(side='left')
        self.undo = tk.Button(controls, text='Roll back settings', command=self.run_rollback, bg=PANEL, fg=TEXT, relief='flat', padx=15, pady=12)
        self.undo.pack(side='left', padx=10)
        if not ctypes.windll.shell32.IsUserAnAdmin():
            tk.Button(controls, text='Run as administrator', command=self.elevate, bg=PANEL, fg=TEXT,
                      relief='flat', padx=10, pady=12).pack(side='left')
        tk.Button(controls, text='Export report', command=self.export, bg=PANEL, fg=TEXT, relief='flat', padx=15, pady=12).pack(side='right')
        self.status = tk.Label(shell, text='Monitoring. The button name describes symptoms; it does not guarantee a fix.', bg=BG, fg=MUTED, anchor='w', wraplength=1050)
        self.status.pack(fill='x', pady=(0, 10))
        tabs = ttk.Notebook(shell)
        tabs.pack(fill='both', expand=True)
        dash, advice, report = [tk.Frame(tabs, bg=PANEL, padx=15, pady=12) for _ in range(3)]
        tabs.add(dash, text='Live dashboard')
        tabs.add(advice, text='Diagnostics & next actions')
        tabs.add(report, text='Optimization report')
        self.chart = tk.Canvas(dash, bg=PANEL, height=120, highlightthickness=0)
        self.chart.pack(fill='x')
        self.metrics = tk.Label(dash, text='', bg=PANEL, fg=MUTED, justify='left', anchor='w', font=('Segoe UI', 10))
        self.metrics.pack(fill='x', pady=8)
        self.table = ttk.Treeview(dash, columns=('name', 'pid', 'ram', 'private', 'growth'), show='headings', height=7)
        for col, title, width in [('name', 'Top memory consumers', 260), ('pid', 'PID', 70), ('ram', 'Working set', 120), ('private', 'Private commit', 120), ('growth', 'Growth observation', 240)]:
            self.table.heading(col, text=title)
            self.table.column(col, width=width, stretch=True)
        self.table.pack(fill='both', expand=True)
        self.advice = self.textbox(advice)
        self.reportbox = self.textbox(report)
        self.set_text(self.reportbox, 'No optimization has run.\n\nMeasurements are real; no example or simulated results are shown.\n\nOne click checks Windows memory compression and system-managed paging. A newly verified restore point is required before any setting changes. The app never terminates another process, purges system caches, disables startup apps, or deletes files.\n\nRollback restores only settings changed by this app. It cannot reverse elapsed activity or a reboot. Full system recovery is available through Windows System Restore.')
        tk.Label(shell, text='Safety first: no process termination • no security changes • no file deletion • journaled settings', bg=BG, fg=MUTED, font=('Segoe UI', 9)).pack(anchor='w', pady=(10, 0))

    def textbox(self, frame):
        box = tk.Text(frame, bg=PANEL, fg=TEXT, relief='flat', wrap='word', font=('Segoe UI', 11), padx=8, pady=8)
        scroll = ttk.Scrollbar(frame, command=box.yview)
        scroll.pack(side='right', fill='y')
        box.configure(yscrollcommand=scroll.set)
        box.pack(fill='both', expand=True)
        return box

    def elevate(self, auto=False):
        if self.busy: return
        parameters = '' if getattr(sys, 'frozen', False) else subprocess.list2cmdline([str(Path(__file__).resolve())])
        if auto: parameters += ' --optimize'
        result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, parameters, None, 1)
        if result > 32: self.close()
        else: self.status.configure(text='Administrator launch was cancelled or unavailable. Monitoring continues.')

    def set_text(self, box, text):
        box.configure(state='normal')
        box.delete('1.0', 'end')
        box.insert('end', text)
        box.configure(state='disabled')

    def monitor(self):
        previous_disk, previous_time = None, None
        while not self.stop.is_set():
            try:
                v, swap, perf = psutil.virtual_memory(), psutil.swap_memory(), performance()
                processes = []
                now = time.monotonic()
                live_keys = set()
                for p in psutil.process_iter(['pid', 'name', 'memory_info', 'create_time']):
                    try:
                        info = p.info
                        mem = info['memory_info']
                        key = (p.pid, info['create_time'])
                        live_keys.add(key)
                        points = self.growth.setdefault(key, [])
                        private = getattr(mem, 'private', mem.rss)
                        points.append((now, private))
                        points[:] = [x for x in points if now - x[0] <= 900]
                        processes.append({'pid': p.pid, 'name': info['name'], 'rss': mem.rss,
                                          'private': private, 'growth': suspected_growth(points)})
                    except (psutil.Error, AttributeError): pass
                self.growth = {k: v for k, v in self.growth.items() if k in live_keys}
                disk = psutil.disk_io_counters()
                busy = None
                if disk and previous_disk and previous_time and hasattr(disk, 'busy_time'):
                    busy = min(100, max(0, (disk.busy_time - previous_disk.busy_time) / ((now - previous_time) * 10)))
                previous_disk, previous_time = disk, now
                commit_pct = perf['CommitTotal'] / max(1, perf['CommitLimit']) * 100
                sample = {'time': dt.datetime.now().isoformat(), 'total': v.total, 'used': v.total - v.available,
                    'available': v.available, 'percent': v.percent, 'cache': perf['SystemCache'],
                    'commit': perf['CommitTotal'], 'commitLimit': perf['CommitLimit'], 'commitPercent': commit_pct,
                    'pageUsed': swap.used, 'pageTotal': swap.total, 'diskBusy': busy,
                    'pressure': pressure(v.percent, commit_pct, self.extra.get('pagesInput')),
                    'processCount': len(processes), 'top': sorted(processes, key=lambda p: p['rss'], reverse=True)[:12],
                    'growthSuspects': [p for p in processes if p['growth']]}
                self.events.put(('sample', sample))
            except Exception as exc: self.events.put(('status', 'Monitoring error: ' + str(exc)))
            self.stop.wait(3)

    def diagnostics(self):
        script = r"""
$result=@{}
try {$m=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; $result.pagesInput=[double]$m.PagesInputPersec; $result.pagesOutput=[double]$m.PagesOutputPersec; $result.compressed=$m.CompressedPageSize} catch {}
try {$result.startup=@(Get-CimInstance Win32_StartupCommand|Select-Object Name,Location,User)} catch {}
try {$result.lowEvents=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Resource-Exhaustion-Detector';Id=2004;StartTime=(Get-Date).AddDays(-7)} -MaxEvents 10 -ErrorAction Stop|Select-Object TimeCreated,Id)} catch {$result.lowEvents=@();$result.eventNote='No matching events returned, or log unavailable'}
try {$result.disks=@(Get-PhysicalDisk|Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus)} catch {}
try {$c=Get-CimInstance Win32_ComputerSystem; $m=Get-MMAgent; $result.pageAuto=[bool]$c.AutomaticManagedPagefile;$result.compression=[bool]$m.MemoryCompression} catch {}
try {$result.diskActive=[double](Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk|Where-Object Name -eq '_Total').PercentDiskTime} catch {}
$result|ConvertTo-Json -Depth 5 -Compress
"""
        while not self.stop.is_set():
            try: self.events.put(('extra', powershell(script, 60)))
            except Exception as exc: self.events.put(('diagnostic_error', str(exc)))
            self.stop.wait(30)

    def recommendations(self, s):
        x = self.extra
        lines = ['DIAGNOSTICS & RECOMMENDATIONS', 'Local rules based on observed metrics; no AI service is connected.', '']
        lines += [f"RAM health proxy: {100 - s['pressure']}/100 (memory pressure heuristic)",
                  'Composite system health: unavailable. Disk activity, physical disk status, startup load and stability are distinct; no invented overall score.',
                  f"Background/process load: {s['processCount']} accessible processes. Counts alone do not establish excess.", '']
        if s['percent'] >= 85:
            lines += ['• Sustained high RAM use: save work and close unused applications or browser tabs. Review browser Task Manager for per-tab memory.',
                      '• If this persists during normal work, consider a RAM upgrade after checking hardware compatibility.']
        if s['commitPercent'] > 85:
            lines += ['• Commit headroom is low. Review system-managed paging and free space on the Windows drive.']
        if x.get('pagesInput', 0) > 100:
            lines += ['• Paging input is elevated. Observe several samples; page input also includes file-backed reads.']
        if not x.get('pageAuto', True): lines += ['• Manual page-file configuration detected. One-click can enable Windows management after a restore point.']
        if not x.get('compression', True): lines += ['• Memory compression is disabled. One-click can enable it after a restore point.']
        if not any([s['percent'] >= 85, s['commitPercent'] > 85, x.get('pagesInput', 0) > 100]):
            lines += ['• No acute memory pressure detected in the current sample. Leave reclaimable Windows caches intact.']
        lines += ['', 'ABNORMAL MEMORY GROWTH (5–15 minute observation)']
        lines += [f"• {p['name']} (PID {p['pid']}): sustained private-memory growth; investigate, not a confirmed leak." for p in s['growthSuspects']] or ['No sustained growth suspects yet. Continue monitoring.']
        lines += ['', 'STARTUP INVENTORY (not disabled automatically)']
        startup = x.get('startup', [])
        if isinstance(startup, dict): startup = [startup]
        for p in startup[:30]: lines.append(f"• {p.get('Name', '?')} — {p.get('Location', '')}")
        if not startup: lines.append('No startup entries returned, or inventory unavailable.')
        lines += ['Startup-to-running-process RAM attribution is not available; names alone are insufficient for a safe match.', '', 'PHYSICAL DISK STATUS']
        disks = x.get('disks', [])
        if isinstance(disks, dict): disks = [disks]
        for d in disks: lines.append(f"• {d.get('FriendlyName')}: {d.get('HealthStatus')} | {d.get('MediaType')}")
        if not disks: lines.append('Unavailable. No disk health score inferred.')
        try:
            d = psutil.disk_usage(os.environ.get('SystemDrive', 'C:') + '\\')
            lines.append(f'Windows drive: {gb(d.free)} free / {gb(d.total)} total. Use Windows Storage settings to review cleanup.')
        except OSError: pass
        lines += ['', f"Low-memory events, past 7 days: {len(x.get('lowEvents', []))} returned. {x.get('eventNote', '')}", '',
                  'LIMITS & SAFETY',
                  'System-wide fragmentation and confirmed application leaks cannot be diagnosed from RAM totals. Deep tracing is required.',
                  'Standby/cache purges, other-process working-set trimming, visual-effect changes, startup removal, browser termination and telemetry changes are intentionally excluded from automatic actions.',
                  'Memory compression and paging are managed by Windows. Page-file changes may require a restart. No reboot is triggered.',
                  'Rollback restores the two settings this app can change. Existing manual page-file entries are left intact. Restore points are an additional safeguard, not a backup of personal files.']
        return '\n'.join(lines)

    def poll(self):
        if self.stop.is_set(): return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'sample':
                    self.latest = value
                    self.history.append(value['percent'])
                    self.history = self.history[-120:]
                    self.render(value)
                elif kind == 'extra': self.extra = value
                elif kind == 'status': self.status.configure(text=value)
                elif kind == 'diagnostic_error':
                    self.extra['diagnosticError'] = value
                elif kind == 'done':
                    self.busy = False
                    self.fix.configure(state='normal')
                    self.undo.configure(state='normal')
                    if 'error' in value:
                        self.status.configure(text='Stopped safely. ' + value['error'].split('\n')[0][:200])
                        self.set_text(self.reportbox, value['error'])
                        # Keep the error in the report; avoid a modal dialog blocking live updates.
                    else:
                        self.report = value
                        self.status.configure(text=value['summary'])
                        self.set_text(self.reportbox, value['text'])
        except queue.Empty: pass
        if not self.stop.is_set(): self.root.after(200, self.poll)

    def render(self, s):
        for key, value in [('TOTAL RAM', gb(s['total'])), ('USED RAM', gb(s['used'])), ('AVAILABLE', gb(s['available'])), ('PRESSURE', f"{s['pressure']} / 100")]:
            self.cards[key].configure(text=value)
        cache = gb(s['cache'])
        compressed = self.extra.get('compressed')
        compressed_label = gb(float(compressed)) if compressed is not None else 'Unavailable'
        disk = self.extra.get('diskActive')
        disk_label = f'{disk:.0f}% (Windows counter)' if disk is not None else 'Unavailable'
        self.metrics.configure(text=f"RAM {s['percent']:.1f}%  •  System cache {cache}  •  Compressed {compressed_label}\n"
            f"Committed {gb(s['commit'])} / {gb(s['commitLimit'])}  •  Page file {gb(s['pageUsed'])} / {gb(s['pageTotal'])}\n"
            f"Pages input/sec: {self.extra.get('pagesInput', 'Unavailable')}  •  Disk activity: {disk_label}  •  Updated {s['time'][11:19]}")
        self.table.delete(*self.table.get_children())
        for p in s['top']:
            self.table.insert('', 'end', values=(p['name'], p['pid'], mb(p['rss']), mb(p['private']), 'Suspected growth' if p['growth'] else 'Observing'))
        c = self.chart
        c.delete('all')
        w, h = max(300, c.winfo_width()), 120
        for level in (0, 50, 100):
            y = 25 + (100 - level) * .8
            c.create_line(36, y, w-10, y, fill='#28364d')
            c.create_text(0, y, text=str(level), fill=MUTED, anchor='w', font=('Segoe UI', 8))
        c.create_text(36, 10, text='RAM USAGE %  /  last 6 minutes', anchor='w', fill=MUTED, font=('Segoe UI', 9))
        pts = []
        for i, value in enumerate(self.history): pts.extend((36 + i / 119 * (w - 50), 25 + (100-value)*.8))
        if len(pts) >= 4: c.create_line(*pts, fill=GREEN, width=2)
        text = self.recommendations(s)
        if self.extra.get('diagnosticError'): text += '\n\nExtended diagnostics unavailable: ' + self.extra['diagnosticError']
        self.set_text(self.advice, text)

    def task(self, fn):
        if self.busy: return
        self.busy = True
        self.fix.configure(state='disabled')
        self.undo.configure(state='disabled')
        def work():
            try: self.events.put(('done', fn()))
            except Exception as exc: self.events.put(('done', {'error': str(exc)}))
        threading.Thread(target=work, daemon=True).start()

    def run_optimization(self):
        if not self.latest: return
        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.elevate(auto=True)
            return
        before = dict(self.latest)
        disk_before = self.extra.get('diskActive')
        self.status.configure(text='Checking permissions and settings; creating and verifying a new restore point…')
        def work():
            actions, checkpoint = optimize(before)
            time.sleep(4)
            after = dict(self.latest or before)
            disk_after = self.extra.get('diskActive')
            disk_text = f'{disk_before:.0f}% → {disk_after:.0f}%' if disk_before is not None and disk_after is not None else 'Unavailable'
            text = '\n'.join(['OPTIMIZATION REPORT', dt.datetime.now().isoformat(timespec='seconds'), '',
                f"Memory: {gb(before['used'])} ({before['percent']:.1f}%) → {gb(after['used'])} ({after['percent']:.1f}%)",
                'These are observed samples; changes are not proof of optimization benefit.',
                f'Disk activity: {disk_text} (30-second counter; may be the same sample)',
                'Processes terminated / trimmed: 0', 'Files cleaned: 0 — automatic deletion is disabled',
                f"Memory pressure: {before['pressure']} → {after['pressure']} / 100",
                'System health improvement / optimization score: not established by a brief observation.', '',
                'VERIFIED ACTIONS'] + (actions or ['Windows compression and automatic paging were already enabled. No setting changes needed.']) + [
                '', f"Restore point verified: #{checkpoint['sequence']}", f'Rollback journal: {JOURNAL}', '',
                'NEXT ACTIONS', 'Observe normal workload for several minutes. Restart later if paging settings changed.',
                'Review startup apps and browser Task Manager. Review Windows Storage cleanup manually.',
                'If pressure persists, investigate the top consumers and suspected growth before considering compatible RAM upgrades.'])
            report = {'summary': 'Optimization checks completed. Review measured results and recommended next actions.',
                      'text': text, 'before': before, 'after': after, 'actions': actions, 'restorePoint': checkpoint}
            save_json(ROOT / ('report-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.json'), report)
            return report
        self.task(work)

    def run_rollback(self):
        self.status.configure(text='Restoring journaled settings and verifying Windows state…')
        def work():
            actions = rollback()
            return {'summary': 'Rollback completed. A restart may be required.', 'text': 'ROLLBACK\n\n' + '\n'.join(actions)}
        self.task(work)

    def export(self):
        if not self.latest:
            messagebox.showinfo('Waiting for data', 'Wait for the first live sample.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.json', initialfile='Memory-health-report.json', filetypes=[('JSON report', '*.json')])
        if path:
            try:
                save_json(Path(path), {'snapshot': self.latest, 'diagnostics': self.extra,
                                     'recommendations': self.recommendations(self.latest), 'optimization': self.report})
                self.status.configure(text='Report exported to ' + path)
            except Exception as exc: messagebox.showerror('Export failed', str(exc))

if __name__ == '__main__':
    if sys.platform != 'win32': raise SystemExit('Windows 10/11 is required.')
    app = App()
    if '--optimize' in sys.argv:
        def start_when_ready():
            if app.latest: app.run_optimization()
            elif not app.stop.is_set(): app.root.after(500, start_when_ready)
        app.root.after(1000, start_when_ready)
    app.root.mainloop()
