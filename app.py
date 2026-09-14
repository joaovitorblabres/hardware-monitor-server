import csv
import os
import socket
import threading
import time
from datetime import datetime
from fastapi import FastAPI
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
    """Percorre recursivamente a árvore do JSON buscando 'CPU Package' ou 'CPU Total' em Watts."""
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
    """Percorre recursivamente a árvore do JSON buscando sensores de energia da GPU em Watts."""
    text = node.get("Text", "")
    value = node.get("Value", "")

    # Procura por 'GPU Power', 'GPU Total', 'GPU Core' ou 'GPU' com unidade W
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
    """Consulta o endpoint HTTP do Open Hardware Monitor e extrai CPU e GPU Power."""
    host_ip = get_windows_host_ip()
    url = f"http://{host_ip}:8085/data.json"

    cpu_power = 0.0
    gpu_power = 0.0

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
