MEMORY OPTIMIZER — Windows 10/11 x64

Launch MemoryOptimizer.exe. No Python installation is needed for the executable.
Monitoring runs without administrator permissions. Use Run as administrator in
the app to allow optimization. Windows will show its normal elevation prompt.

DELIVERED FUNCTIONALITY
Live RAM totals, availability, system cache, commit, page-file use, top memory
consumers, private commit, a six-minute chart, and a transparent pressure heuristic.
Extended diagnostics sample approximately every 30 seconds: paging input/output,
startup inventory, physical disk health, low-memory events, Windows disk activity,
compression and page-file management. Unsupported counters show Unavailable.
Private-memory growth needs at least five minutes of observation; flags are
suspicions, not confirmed leaks. Some processes cannot be inspected.

ONE-CLICK ACTIONS
1. Read current Windows settings and require administrator permissions.
2. Create a new restore point and verify its new sequence number. If System
   Protection is off, throttled, or unavailable, STOP without setting changes.
3. Enable memory compression if disabled.
4. Enable Windows system-managed paging if currently manually managed.
5. Verify each setting, record results, compare live observations, save a report.
No restart is forced. A restart may be needed for page-file changes.

ROLLBACK
Roll back settings restores only settings changed by this app, in reverse order.
A durable write-ahead journal also covers interrupted or partially failed runs.
It backs up and restores pre-existing manual page-file configuration entries. Do not delete
the journal before rollback. Windows System Restore is an additional recovery
option, not a personal-file backup or a promise to reverse arbitrary activity.
Run as administrator for rollback. An active record blocks a second optimization
until rollback. Review custom page-file policies before using on a managed PC.

SAFETY AND SCOPE
This is a conservative first implementation, not the entire proposed repair suite.
It never kills processes, trims other applications, purges standby memory,
deletes files, disables startup entries, modifies security/telemetry services,
changes visual effects, or controls browser tabs. Those actions are recommendations
where applicable. It cannot guarantee to resolve 100% disk use or reduce RAM use.
High utilization alone is not proof of a defect. It does not diagnose system-wide
fragmentation, prove memory leaks, attribute startup RAM reliably, or infer SSD
health percentages. No AI service, remote upload, or cloud account is used.
No composite health score is fabricated where component evidence is incomplete.
RAM pressure is max(RAM %, commit %) plus up to 15 for paging input, capped at 100;
the displayed RAM health proxy is 100 minus this heuristic, not a hardware test.
System cache is the Windows GetPerformanceInfo SystemCache metric, not an exact
standby list count. Paging input includes file-backed I/O, not just page-file I/O.
Disk activity may exceed 100% across disks in the Windows aggregate counter.

DATA
Local journal and automatically generated optimization reports:
%LOCALAPPDATA%\MemoryOptimizer
Export report saves a JSON file at your chosen location. Reports include process
names, startup names/users, and system metrics; review before sharing.

SOURCE / BUILD
Source is memory_optimizer.py. Requires Python 3.11+ and requirements.txt.
Run: python memory_optimizer.py
Build with PyInstaller: python -m PyInstaller --noconfirm --clean --onefile
  --windowed --name MemoryOptimizer memory_optimizer.py

REFERENCES
https://learn.microsoft.com/en-us/windows/win32/memory/memory-performance-information
https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getperformanceinfo
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/checkpoint-computer
https://learn.microsoft.com/en-us/powershell/module/mmagent/enable-mmagent
