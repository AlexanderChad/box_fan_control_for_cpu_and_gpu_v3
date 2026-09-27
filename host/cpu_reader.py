"""
CPU temperature reader using lm-sensors.
"""

import logging
import re
import subprocess

from config import ANOMAL_MIN_TEMP, ANOMAL_MAX_TEMP

logger = logging.getLogger(__name__)


class CpuTempReader:
    """Reads CPU temperatures using lm-sensors"""

    def __init__(self):
        self.cpu_count = 0
        self.socket_temps: dict[int, int] = {}
        self._initialized = False

    def initialize(self):
        try:
            # Count unique physical IDs (actual CPU sockets)
            result = subprocess.run(
                ["sh", "-c", "grep 'physical id' /proc/cpuinfo | sort -u | wc -l"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                self.cpu_count = int(result.stdout.strip())
            else:
                self.cpu_count = 1

            if self.cpu_count == 0:
                self.cpu_count = 1

            self._initialized = True
            logger.info(f"CPU sockets detected: {self.cpu_count}")
        except Exception as e:
            logger.warning(f"Failed to detect CPU count: {e}")
            self.cpu_count = 1
            self._initialized = True

    def get_all_temperatures(self) -> dict[int, int]:
        if not self._initialized:
            return {0: 0}

        result = {}

        for i in range(self.cpu_count):
            try:
                proc = subprocess.run(
                    ["sensors", f"coretemp-isa-000{i}"],
                    capture_output=True, text=True, timeout=2
                )

                if proc.returncode != 0:
                    proc = subprocess.run(
                        ["sensors"],
                        capture_output=True, text=True, timeout=2
                    )

                match = re.search(
                    rf"Package id {i}:\s+\+?(\d+\.?\d*)°?",
                    proc.stdout
                )

                if match:
                    temp = int(float(match.group(1)))
                    if ANOMAL_MIN_TEMP <= temp <= ANOMAL_MAX_TEMP:
                        result[i] = temp
                    else:
                        logger.warning(f"CPU {i} temp {temp}°C out of valid range")
                        result[i] = 0
                else:
                    result[i] = 0

            except Exception as e:
                logger.debug(f"Failed to read CPU {i} temp: {e}")
                result[i] = 0

        self.socket_temps = result
        return result

    def get_max_temperature(self) -> int:
        temps = self.get_all_temperatures()
        valid_temps = [t for t in temps.values() if t > 0]
        return max(valid_temps) if valid_temps else 0
