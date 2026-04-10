"""
Configuration constants and settings.
"""

# Data timeout (seconds without data before emergency mode)
DATA_TIMEOUT_SECONDS = 5

# StatsD settings (Netdata)
STATSD_HOST = "127.0.0.1"
STATSD_PORT = 8125

# Server settings
SERVER_PORT = 17006
SERVER_HOST = "0.0.0.0"

# Temperature validation
ANOMAL_MIN_TEMP = 5
ANOMAL_MAX_TEMP = 90

# Emergency mode
EMERGENCY_PWM = 100

# Proxmox paths
PROXMOX_VM_CONFIG_DIR = "/etc/pve/qemu-server"
PROXMOX_QM_CMD = "/usr/sbin/qm"
