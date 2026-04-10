"""
PWM controller for case fans via sysfs.
"""

import logging
from pathlib import Path

from config import EMERGENCY_PWM

logger = logging.getLogger(__name__)


class PwmController:
    """Controls case fans via PWM through sysfs."""

    def __init__(self, statsd):
        self.statsd = statsd
        self.pwm_paths: list[str] = []
        self.current_pwm_percent: int = 0

    def initialize(self):
        """Find PWM paths in sysfs"""
        platform_path = Path("/sys/devices/platform")
        if platform_path.exists():
            for device in platform_path.iterdir():
                if device.name.startswith("nct6775"):
                    hwmon_path = device / "hwmon"
                    if hwmon_path.exists():
                        for hwmon in hwmon_path.iterdir():
                            for i in range(1, 10):
                                pwm_path = hwmon / f"pwm{i}"
                                if pwm_path.exists():
                                    self.pwm_paths.append(str(pwm_path))
                        if self.pwm_paths:
                            print(f"PWM found via nct6775: {self.pwm_paths}")
                            return

        # Fallback: search in /sys/class/hwmon
        hwmon_path = Path("/sys/class/hwmon")
        if hwmon_path.exists():
            for hwmon in hwmon_path.iterdir():
                for i in range(1, 10):
                    pwm_path = hwmon / f"pwm{i}"
                    if pwm_path.exists():
                        self.pwm_paths.append(str(pwm_path))

        if self.pwm_paths:
            print(f"PWM paths: {self.pwm_paths}")
        else:
            print("WARNING: No PWM paths found")

    @staticmethod
    def calculate_pwm_from_profile(temp: int, profile: list) -> int:
        """
        Calculate PWM percentage from temperature using profile curve.
        
        Edge cases:
        - Temperature left of first point -> 0% (fans off or minimal)
        - Temperature right of last point -> 100% (max cooling)
        
        Args:
            temp: Current temperature
            profile: List of [temp, pwm%] points
        
        Returns:
            PWM percentage (0-100)
        """
        if not profile:
            return 0

        curve = sorted(profile, key=lambda x: x[0])

        # Left of first point -> 0%
        if temp < curve[0][0]:
            return 0
        
        # Right of last point -> 100%
        if temp > curve[-1][0]:
            return 100
        
        # Exact match on edges
        if temp == curve[0][0]:
            return curve[0][1]
        if temp == curve[-1][0]:
            return curve[-1][1]

        # Linear interpolation between points
        for i in range(len(curve) - 1):
            t1, s1 = curve[i]
            t2, s2 = curve[i + 1]
            if t1 <= temp <= t2:
                ratio = (temp - t1) / (t2 - t1)
                return int(s1 + (s2 - s1) * ratio)

        return curve[-1][1]

    def set_fan_speed(self, percent: int):
        """Set case fan speed percentage"""
        if not self.pwm_paths:
            return

        percent = max(0, min(100, percent))
        self.current_pwm_percent = percent
        pwm_value = int(percent * 255 / 100)

        for path in self.pwm_paths:
            try:
                enable_path = path + "_enable"
                Path(enable_path).write_text("1")
                Path(path).write_text(str(pwm_value))
            except Exception as e:
                logger.error(f"Failed to set PWM {path}: {e}")

        self.statsd.send_box_fan_metric(percent)

    def set_emergency(self):
        """Set fans to maximum speed (emergency mode)"""
        self.set_fan_speed(EMERGENCY_PWM)

    def set_auto_mode(self):
        """Set fans to auto mode"""
        for path in self.pwm_paths:
            try:
                enable_path = path + "_enable"
                Path(enable_path).write_text("2")
            except Exception as e:
                logger.error(f"Failed to set auto mode: {e}")

    def has_pwm(self) -> bool:
        """Check if PWM paths are available"""
        return len(self.pwm_paths) > 0
