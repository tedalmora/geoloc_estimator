"""Low-overhead Linux resource sampling without opening campaign hardware."""

import argparse
import csv
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = (
    "timestamp_utc", "monotonic_s", "mode", "point", "session", "state",
    "cpu_total_pct", "cpu_process_pct", "rss_kb", "hwm_kb", "memory_available_kb",
    "load_1m", "load_5m", "load_15m", "temperature_c", "disk_free_bytes", "database_bytes",
)


def _cpu_counters(proc_root):
    with (Path(proc_root) / "stat").open(encoding="ascii") as stream:
        line = next((line for line in stream if line.startswith("cpu ")), None)
    if line is None:
        raise ValueError("linha cpu ausente em /proc/stat")
    values = [int(value) for value in line.split()[1:]]
    if len(values) < 4:
        raise ValueError("contadores de CPU incompletos em /proc/stat")
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def total_cpu_percent(previous, current):
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))


def process_cpu_percent(previous_ticks, current_ticks, elapsed_s, clock_ticks):
    if (previous_ticks is None or current_ticks is None or not math.isfinite(elapsed_s) or
            elapsed_s <= 0 or clock_ticks <= 0):
        return None
    delta = current_ticks - previous_ticks
    if delta < 0:
        return None
    return 100.0 * delta / (clock_ticks * elapsed_s)


def _process_stats(pid, proc_root):
    path = Path(proc_root) / str(pid)
    try:
        stat = (path / "stat").read_text(encoding="ascii")
        status = (path / "status").read_text(encoding="ascii")
    except OSError:
        return None, None, None
    close = stat.rfind(")")
    if close < 0:
        return None, None, None
    fields = stat[close + 1:].split()
    try:
        process_ticks = int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        process_ticks = None
    values = {}
    for line in status.splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    values[parts[0][:-1]] = int(parts[1])
                except ValueError:
                    pass
    return process_ticks, values.get("VmRSS"), values.get("VmHWM")


def _database_size(db_path):
    total = 0
    found = False
    for path in (Path(db_path), Path(f"{db_path}-wal")):
        try:
            total += path.stat().st_size
            found = True
        except FileNotFoundError:
            continue
        except OSError:
            return ""
    return total if found else ""


def _memory_available(proc_root):
    try:
        with (Path(proc_root) / "meminfo").open(encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return ""


def sample_resources(db_path, data_dir, pid=None, state="", mode="", point="", session="",
                     proc_root="/proc", sys_root="/sys"):
    try:
        cpu = _cpu_counters(proc_root)
    except (OSError, ValueError):
        cpu = None
    process_ticks, rss, hwm = _process_stats(pid, proc_root) if pid is not None else (None, None, None)
    load = ("", "", "")
    try:
        load_values = (Path(proc_root) / "loadavg").read_text(encoding="ascii").split()
        if len(load_values) >= 3:
            load = tuple(load_values[:3])
    except OSError:
        pass
    temperature = ""
    try:
        raw_temperature = int((Path(sys_root) / "class" / "thermal" / "thermal_zone0" / "temp")
                              .read_text(encoding="ascii").strip())
        temperature = raw_temperature / 1000.0
    except (OSError, ValueError):
        pass
    try:
        disk_free = shutil.disk_usage(data_dir).free
    except OSError:
        disk_free = ""
    database_bytes = _database_size(db_path)
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "monotonic_s": time.monotonic(),
        "mode": mode, "point": point, "session": session, "state": state,
        "cpu": cpu, "process_ticks": process_ticks, "rss_kb": rss if rss is not None else "",
        "hwm_kb": hwm if hwm is not None else "", "load": load,
        "memory_available_kb": _memory_available(proc_root),
        "temperature_c": temperature, "disk_free_bytes": disk_free,
        "database_bytes": database_bytes,
    }


def _metric_row(sample, previous_cpu, previous_ticks, previous_time, clock_ticks):
    now = sample["monotonic_s"]
    cpu_pct = total_cpu_percent(previous_cpu, sample["cpu"])
    process_pct = process_cpu_percent(
        previous_ticks, sample["process_ticks"],
        now - previous_time if previous_time is not None else 0, clock_ticks,
    )
    load = sample["load"]
    row = {
        "timestamp_utc": sample["timestamp_utc"], "monotonic_s": f"{now:.3f}",
        "mode": sample["mode"],
        "point": sample["point"], "session": sample["session"], "state": sample["state"],
        "cpu_total_pct": "" if cpu_pct is None else f"{cpu_pct:.2f}",
        "cpu_process_pct": "" if process_pct is None else f"{process_pct:.2f}",
        "rss_kb": sample["rss_kb"], "hwm_kb": sample["hwm_kb"],
        "memory_available_kb": sample["memory_available_kb"],
        "load_1m": load[0], "load_5m": load[1], "load_15m": load[2],
        "temperature_c": sample["temperature_c"], "disk_free_bytes": sample["disk_free_bytes"],
        "database_bytes": sample["database_bytes"],
    }
    return row, sample["cpu"], sample["process_ticks"], now


def _open_metrics(path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size:
        with target.open(newline="", encoding="utf-8") as existing:
            header = next(csv.reader(existing), None)
        if header != list(FIELDS):
            raise ValueError(f"{target}: cabeçalho de métricas incompatível")
    stream = target.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    if target.stat().st_size == 0:
        writer.writeheader()
    return stream, writer


def run_idle(db_path, data_dir, output, duration_s=60, interval_s=10, sleep=time.sleep,
             proc_root="/proc", sys_root="/sys"):
    if (not math.isfinite(duration_s) or not math.isfinite(interval_s) or
            duration_s <= 0 or interval_s <= 0):
        raise ValueError("duração e intervalo precisam ser positivos")
    stream, writer = _open_metrics(output)
    previous_cpu = previous_ticks = previous_time = None
    clock_ticks = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    try:
        while True:
            sample = sample_resources(db_path, data_dir, mode="repouso",
                                      proc_root=proc_root, sys_root=sys_root)
            row, previous_cpu, previous_ticks, previous_time = _metric_row(
                sample, previous_cpu, previous_ticks, previous_time, clock_ticks)
            writer.writerow(row)
            stream.flush()
            remaining = duration_s - (time.monotonic() - started)
            if remaining <= 0:
                break
            sleep(min(interval_s, remaining))
    finally:
        stream.close()


def run_collection(db_path, data_dir, output, log_path, point, session, command,
                   interval_s=10, sleep=time.sleep, proc_root="/proc", sys_root="/sys"):
    if not command:
        raise ValueError("comando do coletor não informado")
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("intervalo precisa ser positivo")
    metrics_stream, writer = _open_metrics(output)
    log_target = Path(log_path)
    log_target.parent.mkdir(parents=True, exist_ok=True)
    previous_cpu = previous_ticks = previous_time = None
    clock_ticks = os.sysconf("SC_CLK_TCK")
    process = None
    try:
        with log_target.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            while True:
                return_code = process.poll()
                state = "running" if return_code is None else "exited"
                sample = sample_resources(
                    db_path, data_dir, pid=process.pid, state=state, mode="coleta",
                    point=point, session=session, proc_root=proc_root, sys_root=sys_root,
                )
                row, previous_cpu, previous_ticks, previous_time = _metric_row(
                    sample, previous_cpu, previous_ticks, previous_time, clock_ticks)
                writer.writerow(row)
                metrics_stream.flush()
                if return_code is not None:
                    return return_code
                sleep(interval_s)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            return process.wait()
        raise
    finally:
        metrics_stream.close()


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Mede recursos do sistema sem consultar o modem.")
    sub = parser.add_subparsers(dest="mode", required=True)
    idle = sub.add_parser("repouso", help="amostra recursos por 60 s antes da coleta")
    idle.add_argument("--db", required=True)
    idle.add_argument("--data-dir", required=True)
    idle.add_argument("--out", required=True)
    idle.add_argument("--duration-s", type=float, default=60)
    idle.add_argument("--interval-s", type=float, default=10)
    collect = sub.add_parser("coleta", help="monitora um comando e grava sua saída em log")
    collect.add_argument("--db", required=True)
    collect.add_argument("--data-dir", required=True)
    collect.add_argument("--out", required=True)
    collect.add_argument("--log", required=True)
    collect.add_argument("--point", required=True)
    collect.add_argument("--session", required=True)
    collect.add_argument("--interval-s", type=float, default=10)
    collect.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "repouso":
        run_idle(args.db, args.data_dir, args.out, args.duration_s, args.interval_s)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run_collection(args.db, args.data_dir, args.out, args.log, args.point,
                          args.session, command, args.interval_s)


if __name__ == "__main__":
    raise SystemExit(_main())
