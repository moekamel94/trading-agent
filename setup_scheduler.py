"""Run once to register all trading agent tasks in Windows Task Scheduler."""
import subprocess
import sys
import os

PYTHON = sys.executable
DIR    = os.path.dirname(os.path.abspath(__file__))
MAIN   = os.path.join(DIR, "main.py")

TASKS = [
    # (task_name, script_args, schedule_type, days_or_none, time_et)
    ("TradingAgent_Basket",    ["--basket-refresh"], "weekly",  "MON",                   "08:00"),
    ("TradingAgent_Premarket", ["--premarket"],      "weekly",  "MON,TUE,WED,THU,FRI",   "09:00"),
    ("TradingAgent_Cycle",     [],                  "weekly",  "MON,TUE,WED,THU,FRI",   "09:35"),
    ("TradingAgent_Close",     ["--close-summary"], "weekly",  "MON,TUE,WED,THU,FRI",   "16:05"),
]

def create_task(name, args, schedule, days, time_et):
    # Build the command string
    script_args = " ".join(args)
    cmd_str = f'"{PYTHON}" "{MAIN}" {script_args}'.strip()

    # Delete existing task silently
    subprocess.run(
        ["schtasks", "/delete", "/tn", name, "/f"],
        capture_output=True
    )

    # Build schtasks create command
    base = ["schtasks", "/create", "/tn", name, "/tr", cmd_str,
            "/sc", schedule, "/st", time_et, "/f"]
    if days:
        base += ["/d", days]

    result = subprocess.run(base, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  OK  {name:35s} {time_et}  {schedule} {days or 'daily'}")
    else:
        print(f"  ERR {name}: {result.stderr.strip()}")

print("Registering Windows Task Scheduler jobs...\n")
for task in TASKS:
    create_task(*task)

print("\nDone. Verify with: schtasks /query /fo list /tn TradingAgent_Cycle")
