import csv
import os
import socket
import threading
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
import psutil
import requests

app = FastAPI(title="Hardware & Energy Monitor API")

CSV_FILE = "consumo_sistema.csv"
history_buffer = []

# Inicializa CSV se não existir
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", 
            "cpu_usage_percent", 
            "ram_usage_percent", 
            "ram_used_gb", 
            "cpu_power_watts",
            "gpu_power_watts"
        ])

def get_windows_host_ip():
    """Descobre o IP de Gateway do Windows a partir do container Linux/WSL."""
    try:
        return socket.gethostbyname("host.docker.internal")
    except Exception:
        with open("/proc/net/route") as fh:
            for line in fh:
                fields = line.strip().split()
                if fields[1] == '00000000':
                    return socket.inet_ntoa(int(fields[2], 16).to_bytes(4, 'little'))
    return "localhost"

def find_cpu_power_sensor(node):
    text = node.get("Text", "")
    value = node.get("Value", "")
    if ("CPU Package" in text or "CPU Total" in text) and "W" in value:
        val_str = value.replace("W", "").strip().replace(",", ".")
        try:
            return float(val_str)
        except ValueError:
            pass
    for child in node.get("Children", []):
        res = find_cpu_power_sensor(child)
        if res > 0.0:
            return res
    return 0.0

def find_gpu_power_sensor(node):
    text = node.get("Text", "")
    value = node.get("Value", "")
    if ("GPU" in text and ("Power" in text or "Total" in text or "Core" in text)) and "W" in value:
        val_str = value.replace("W", "").strip().replace(",", ".")
        try:
            return float(val_str)
        except ValueError:
            pass
    for child in node.get("Children", []):
        res = find_gpu_power_sensor(child)
        if res > 0.0:
            return res
    return 0.0

def get_power_from_host():
    host_ip = get_windows_host_ip()
    url = f"http://{host_ip}:8085/data.json"
    cpu_power, gpu_power = 0.0, 0.0
    try:
        response = requests.get(url, timeout=1.5)
        if response.status_code == 200:
            json_data = response.json()
            cpu_power = find_cpu_power_sensor(json_data)
            gpu_power = find_gpu_power_sensor(json_data)
    except Exception:
        pass
    return cpu_power, gpu_power

def read_metrics():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cpu_usage = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    ram_percent = ram.percent
    ram_used_gb = round(ram.used / (1024**3), 2)
    cpu_power, gpu_power = get_power_from_host()

    return {
        "timestamp": timestamp,
        "cpu_usage_percent": cpu_usage,
        "ram_usage_percent": ram_percent,
        "ram_used_gb": ram_used_gb,
        "cpu_power_watts": cpu_power,
        "gpu_power_watts": gpu_power
    }

def background_collector():
    while True:
        data = read_metrics()
        history_buffer.append(data)
        if len(history_buffer) > 100:
            history_buffer.pop(0)

        with open(CSV_FILE, mode='a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                data["timestamp"],
                data["cpu_usage_percent"],
                data["ram_usage_percent"],
                data["ram_used_gb"],
                data["cpu_power_watts"],
                data["gpu_power_watts"]
            ])

threading.Thread(target=background_collector, daemon=True).start()

@app.get("/metrics")
def get_metrics():
    return history_buffer[-1] if history_buffer else read_metrics()

@app.get("/history")
def get_history(limit: int = 20):
    return history_buffer[-limit:]

@app.get("/average")
def get_average(
    start_time: str = Query(..., description="Formato: YYYY-MM-DD HH:MM:SS"),
    end_time: str = Query(..., description="Formato: YYYY-MM-DD HH:MM:SS")
):
    """Calcula a média do consumo de energia e do sistema entre dois intervalos de tempo."""
    time_format = "%Y-%m-%d %H:%M:%S"
    
    try:
        dt_start = datetime.strptime(start_time, time_format)
        dt_end = datetime.strptime(end_time, time_format)
    except ValueError:
        raise HTTPException(
            status_code=400, 
            detail="Formato de data inválido. Use o formato: YYYY-MM-DD HH:MM:SS"
        )

    if dt_start >= dt_end:
        raise HTTPException(
            status_code=400, 
            detail="'start_time' deve ser menor que 'end_time'."
        )

    if not os.path.exists(CSV_FILE):
        raise HTTPException(status_code=404, detail="Arquivo de histórico não encontrado.")

    matching_rows = []

    with open(CSV_FILE, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row_dt = datetime.strptime(row["timestamp"], time_format)
                if dt_start <= row_dt <= dt_end:
                    matching_rows.append(row)
            except ValueError:
                continue

    if not matching_rows:
        return {
            "message": "Nenhum registro encontrado no intervalo especificado.",
            "start_time": start_time,
            "end_time": end_time,
            "total_records": 0
        }

    count = len(matching_rows)
    avg_cpu_usage = sum(float(r["cpu_usage_percent"]) for r in matching_rows) / count
    avg_ram_usage = sum(float(r["ram_usage_percent"]) for r in matching_rows) / count
    avg_ram_used_gb = sum(float(r["ram_used_gb"]) for r in matching_rows) / count
    avg_cpu_power = sum(float(r["cpu_power_watts"]) for r in matching_rows) / count
    avg_gpu_power = sum(float(r["gpu_power_watts"]) for r in matching_rows) / count

    return {
        "start_time": start_time,
        "end_time": end_time,
        "total_records": count,
        "averages": {
            "cpu_usage_percent": round(avg_cpu_usage, 2),
            "ram_usage_percent": round(avg_ram_usage, 2),
            "ram_used_gb": round(avg_ram_used_gb, 2),
            "cpu_power_watts": round(avg_cpu_power, 2),
            "gpu_power_watts": round(avg_gpu_power, 2),
            "total_power_watts": round(avg_cpu_power + avg_gpu_power, 2)
        }
    }