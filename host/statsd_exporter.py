"""
StatsD exporter for Netdata metrics.
"""

import logging
import socket

from config import STATSD_HOST, STATSD_PORT

logger = logging.getLogger(__name__)


class StatsDExporter:
    """Sends metrics to Netdata via StatsD protocol (UDP)"""

    def __init__(self, host: str = STATSD_HOST, port: int = STATSD_PORT):
        self.host = host
        self.port = port
        self.socket = None
        self._enabled = False

    def connect(self):
        """Initialize UDP socket for StatsD"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._enabled = True
            logger.info(f"StatsD enabled: {self.host}:{self.port}")
        except Exception as e:
            logger.warning(f"StatsD not available: {e}")
            self._enabled = False

    def send_gauge(self, name: str, value: float):
        if not self._enabled or self.socket is None:
            return
        try:
            message = f"{name}:{value}|g".encode()
            self.socket.sendto(message, (self.host, self.port))
        except Exception:
            pass

    def send_gpu_metrics(self, uuid: str, data: dict):
        uuid_short = uuid.split('-')[-1] if '-' in uuid else uuid[-8:]
        if data.get('temp_core') is not None:
            self.send_gauge(f"gpu_{uuid_short}_temp_core", data['temp_core'])
        if data.get('temp_hotspot') is not None:
            self.send_gauge(f"gpu_{uuid_short}_temp_hotspot", data['temp_hotspot'])
        if data.get('temp_memory') is not None:
            self.send_gauge(f"gpu_{uuid_short}_temp_memory", data['temp_memory'])
        if data.get('fan_percent') is not None:
            self.send_gauge(f"gpu_{uuid_short}_fan_percent", data['fan_percent'])
        if data.get('fan_target') is not None:
            self.send_gauge(f"gpu_{uuid_short}_fan_target", data['fan_target'])
        if data.get('power_watts') is not None:
            self.send_gauge(f"gpu_{uuid_short}_power_watts", data['power_watts'])

    def send_box_fan_metric(self, pwm_percent: int):
        self.send_gauge("box_fan_pwm_percent", pwm_percent)

    def send_cpu_metric(self, temp: int):
        self.send_gauge("cpu_max_temp", temp)

    def send_gpu_group_metric(self, temp: int):
        self.send_gauge("gpu_group_max_temp", temp)

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None
