"""
State manager for GPU data, fan profiles, and WebSocket connections.
Includes GPU registration logic for dynamic identification.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATA_TIMEOUT_SECONDS
from proxmox_monitor import ProxmoxMonitor
from statsd_exporter import StatsDExporter

logger = logging.getLogger(__name__)


class StateManager:
    """Manages GPU states, fan profiles, and WebSocket connections."""

    def __init__(self, proxmox: ProxmoxMonitor, statsd: StatsDExporter):
        self.proxmox = proxmox
        self.statsd = statsd

        # GPU config: {uuid: {"name": str, "host_pci": str, "fan_profile": list}}
        self.gpu_config: dict[str, dict] = {}

        # Runtime GPU data: {uuid: {temp_core, temp_hotspot, temp_memory, fan_percent, power_watts, last_seen}}
        self.gpu_runtime: dict[str, dict] = {}

        # Case fan profile (CPU only now, GPU fans are individual)
        self.cpu_fan_profile: list = []

        # Current temps
        self.cpu_temps: dict[int, int] = {}
        self.cpu_temp: int = 0

        # PWM values
        self.cpu_pwm_percent: int = 0
        self.case_fan_pwm_percent: int = 0

        # Emergency mode
        self.emergency_mode: bool = False
        self.emergency_reason: str = ""

        # GPU registration: uuid -> vm_pci (PCI address inside VM)
        self.uuid_to_vm_pci: dict[str, str] = {}

        # WebSocket connections
        self.vm_connections: list = []
        self.ui_connections: list = []
        self.ws_uuids: dict = {}  # ws -> list[uuid]
        self._lock = asyncio.Lock()

        # Config path - use working directory
        self._config_path = Path.cwd() / "config.json"

    def load_config(self):
        """Load config from config.json (always in CWD)"""
        if self._config_path.exists():
            try:
                with open(self._config_path) as f:
                    data = json.load(f)

                self.gpu_config = data.get("gpus", {})
                self.cpu_fan_profile = data.get("cpu_fan_profile", [])
                # Note: gpu_group_fan_profile is deprecated

                # Update ProxmoxMonitor with GPU PCI mappings
                self.proxmox.set_gpu_config(self.gpu_config)

                logger.info(f"Config loaded from {self._config_path}: {len(self.gpu_config)} GPUs")
            except Exception as e:
                logger.error(f"Failed to load config: {e}")

    def save_config(self):
        """Save config to config.json"""
        try:
            data = {
                "gpus": self.gpu_config,
                "cpu_fan_profile": self.cpu_fan_profile,
            }
            with open(self._config_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Config saved to {self._config_path}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def is_known_gpu(self, uuid: str) -> bool:
        """Check if GPU UUID is in config"""
        return uuid in self.gpu_config

    def register_gpu(self, uuid: str, vm_pci: str, gpu_name: str) -> bool:
        """
        Register a GPU from VM client connection.

        Logic:
        1. Get all running VMs with their PCI devices from Proxmox
        2. For each running VM, check if this GPU's vm_pci matches expected position
        3. Match host_pci -> vm_pci -> uuid

        The key insight: Proxmox config has hostpci0, hostpci1, etc.
        Inside VM, these appear as 01:00.0, 02:00.0, etc. (in order)

        Returns True if this is a new GPU that was auto-registered.
        """
        # Normalize vm_pci (e.g., "0000:01:00.0" or "01:00.0")
        vm_pci_normalized = self._normalize_pci(vm_pci)

        # Store mapping
        self.uuid_to_vm_pci[uuid] = vm_pci_normalized

        # Extract bus number from vm_pci (e.g., "01:00.0" -> bus=1)
        # This tells us which hostpci index this corresponds to
        try:
            parts = vm_pci_normalized.split(':')
            if len(parts) >= 2:
                bus_num = int(parts[1], 16)  # Bus number in hex
                hostpci_index = bus_num - 1  # hostpci0 -> bus 1, hostpci1 -> bus 2
            else:
                hostpci_index = -1
        except:
            hostpci_index = -1

        logger.debug(f"Registering GPU {uuid[:8]}... vm_pci={vm_pci_normalized}, hostpci_index={hostpci_index}")

        # Find running VM and match host_pci
        matched_host_pci = None
        matched_vm_id = None

        running_vms = self.proxmox.get_running_vms_with_pci()
        logger.debug(f"Running VMs with PCI: {running_vms}")

        for vm_id, pci_list in running_vms.items():
            # pci_list is ordered by hostpci index
            if hostpci_index >= 0 and hostpci_index < len(pci_list):
                matched_host_pci = pci_list[hostpci_index]
                matched_vm_id = vm_id
                logger.debug(f"  VM {vm_id}: hostpci{hostpci_index} = {matched_host_pci}")
                break

        # Check if this is a new GPU (not in config)
        is_new = uuid not in self.gpu_config

        if is_new:
            # Auto-register with empty profile
            self.gpu_config[uuid] = {
                "name": gpu_name or f"GPU {uuid[:8]}",
                "host_pci": matched_host_pci or "",
                "fan_profile": []
            }

            if matched_host_pci:
                self.proxmox.uuid_to_host_pci[uuid] = matched_host_pci

            logger.info(f"NEW GPU registered: {uuid[:8]}... -> VM PCI {vm_pci} -> Host PCI {matched_host_pci or 'unknown'}")
            self.save_config()

        elif matched_host_pci:
            # Update host_pci if changed
            current_host_pci = self.gpu_config[uuid].get("host_pci", "")
            if current_host_pci != matched_host_pci:
                self.gpu_config[uuid]["host_pci"] = matched_host_pci
                self.proxmox.uuid_to_host_pci[uuid] = matched_host_pci
                logger.info(f"Updated GPU {uuid[:8]}... host_pci: {current_host_pci} -> {matched_host_pci}")
                self.save_config()

        # Rebuild UUID to VM mapping
        self._rebuild_uuid_to_vm_map()

        return is_new

    def _normalize_pci(self, pci: str) -> str:
        """Normalize PCI address to 0000:00:00.0 format"""
        if not pci:
            return ""
        pci = pci.strip()
        if ':' not in pci:
            return pci

        parts = pci.split(':')
        if len(parts) == 3:
            domain, bus, dev_func = parts
        elif len(parts) == 2:
            domain = "0000"
            bus, dev_func = parts
        else:
            return pci

        domain = domain.zfill(4)
        if '.' not in dev_func:
            dev_func = dev_func + ".0"

        return f"{domain}:{bus}:{dev_func}"

    def _rebuild_uuid_to_vm_map(self):
        """Rebuild UUID -> VM ID mapping based on current data"""
        # Clear and rebuild from proxmox data
        self.proxmox.uuid_to_vm.clear()

        for uuid, host_pci in self.proxmox.uuid_to_host_pci.items():
            if host_pci in self.proxmox.pci_to_vm:
                vm_id = self.proxmox.pci_to_vm[host_pci]
                self.proxmox.uuid_to_vm[uuid] = vm_id

    def get_profiles_for_uuids(self, uuids: list[str]) -> dict:
        """Get fan profiles for given UUIDs"""
        return {
            uuid: self.gpu_config.get(uuid, {}).get("fan_profile", [])
            for uuid in uuids if uuid in self.gpu_config
        }

    def is_gpu_online(self, uuid: str) -> bool:
        """Check if GPU has recent telemetry"""
        gpu = self.gpu_runtime.get(uuid)
        if not gpu or not gpu.get("last_seen"):
            return False
        age = (datetime.now() - gpu["last_seen"]).total_seconds()
        return age < DATA_TIMEOUT_SECONDS

    def check_emergency_mode(self) -> tuple[bool, str]:
        """
        Check emergency condition:
        - VM is running (from Proxmox qm list)
        - GPU is passed through to that VM (from Proxmox config)
        - No telemetry from that GPU

        Returns (is_emergency, reason)
        """
        gpus_in_running_vms = self.proxmox.get_gpus_for_running_vms()

        for uuid in gpus_in_running_vms:
            if not self.is_gpu_online(uuid):
                gpu_status = self.proxmox.get_gpu_status(uuid)
                vm_name = gpu_status.get("vm_name", "Unknown")
                gpu_name = self.gpu_config.get(uuid, {}).get("name", uuid[:8])
                return True, f"VM '{vm_name}' running, GPU '{gpu_name}' offline"

        return False, ""

    async def update_telemetry(self, data: dict):
        """Update GPU telemetry from client"""
        now = datetime.now()

        async with self._lock:
            for gpu_data in data.get("gpus", []):
                uuid = gpu_data.get("uuid")

                # Only process known GPUs (from config or just registered)
                if not self.is_known_gpu(uuid):
                    continue

                if uuid not in self.gpu_runtime:
                    self.gpu_runtime[uuid] = {"uuid": uuid}

                gpu = self.gpu_runtime[uuid]
                gpu["last_seen"] = now
                gpu["temp_core"] = gpu_data.get("temp_core")
                gpu["temp_hotspot"] = gpu_data.get("temp_hotspot")
                gpu["temp_memory"] = gpu_data.get("temp_memory")
                gpu["fan_percent"] = gpu_data.get("fan_percent")  # Actual from sensor
                gpu["fan_target"] = gpu_data.get("fan_target")     # Calculated from profile
                gpu["power_watts"] = gpu_data.get("power_watts")

                self.statsd.send_gpu_metrics(uuid, gpu)

    async def get_state(self) -> dict:
        """Get current state for UI"""
        gpus_with_status = []

        for uuid, config in self.gpu_config.items():
            runtime = self.gpu_runtime.get(uuid, {})
            gpu_status = self.proxmox.get_gpu_status(uuid)

            gpu_data = {
                "uuid": uuid,
                "name": config.get("name", ""),
                "host_pci": config.get("host_pci", ""),
                "is_online": self.is_gpu_online(uuid),
                "gpu_status": gpu_status,
                "vm_id": gpu_status.get("vm_id"),
                "vm_name": gpu_status.get("vm_name"),
                "vm_running": gpu_status.get("vm_running", False),
                "temp_core": runtime.get("temp_core"),
                "temp_hotspot": runtime.get("temp_hotspot"),
                "temp_memory": runtime.get("temp_memory"),
                "fan_percent": runtime.get("fan_percent"),
                "fan_target": runtime.get("fan_target"),
                "power_watts": runtime.get("power_watts"),
                "fan_profile": config.get("fan_profile", [])
            }

            if runtime.get("last_seen"):
                gpu_data["last_seen"] = runtime["last_seen"].isoformat()

            gpus_with_status.append(gpu_data)

        return {
            "timestamp": datetime.now().isoformat(),
            "gpus": gpus_with_status,
            "cpu": {
                "temp": self.cpu_temp,
                "temps_by_socket": self.cpu_temps,
                "pwm_percent": self.cpu_pwm_percent,
                "profile": self.cpu_fan_profile
            },
            "case_fans": {
                "pwm_percent": self.case_fan_pwm_percent
            },
            "emergency": {
                "active": self.emergency_mode,
                "reason": self.emergency_reason
            }
        }

    def get_max_gpu_temp(self) -> int:
        """Get max GPU Core temperature from all online GPUs"""
        max_temp = 0
        for uuid in self.gpu_config:
            if self.is_gpu_online(uuid):
                runtime = self.gpu_runtime.get(uuid, {})
                core_temp = runtime.get("temp_core") or 0
                if core_temp > max_temp:
                    max_temp = core_temp
        return max_temp
    
    def get_max_gpu_pwm(self) -> int:
        """
        Get max fan_target from all online GPUs.
        This is used for case fans - we want case fans to match GPU cooling demand.
        Returns 0 if no GPUs are online or have targets.
        
        Note: fan_target is calculated by VM clients from profile.
        We use target instead of actual because some GPUs (RTX 2080 Ti, etc.)
        have vBIOS limits that prevent them from reaching 100%.
        """
        max_pwm = 0
        for uuid in self.gpu_config:
            if self.is_gpu_online(uuid):
                runtime = self.gpu_runtime.get(uuid, {})
                fan_target = runtime.get("fan_target")
                if fan_target is not None and fan_target >= 0:
                    if fan_target > max_pwm:
                        max_pwm = fan_target
        return max_pwm

    async def broadcast_to_ui(self):
        """Broadcast state to all UI connections"""
        if not self.ui_connections:
            return

        state = await self.get_state()
        message = json.dumps({"type": "state", "data": state})

        disconnected = []
        for ws in self.ui_connections:
            try:
                await ws.send_text(message)
            except:
                disconnected.append(ws)

        for ws in disconnected:
            if ws in self.ui_connections:
                self.ui_connections.remove(ws)

    async def send_profiles_to_vm(self, ws, uuids: list[str]):
        """Send fan profiles to VM client"""
        profiles = self.get_profiles_for_uuids(uuids)
        message = json.dumps({"type": "profiles", "profiles": profiles})
        try:
            await ws.send_text(message)
        except Exception as e:
            logger.error(f"Failed to send profiles: {e}")

    async def broadcast_profiles_to_vms(self, uuids: list[str] = None):
        """Broadcast updated profiles to VM clients"""
        for ws, vm_uuids in list(self.ws_uuids.items()):
            if uuids:
                target_uuids = [u for u in vm_uuids if u in uuids]
            else:
                target_uuids = vm_uuids

            if not target_uuids:
                continue

            profiles = self.get_profiles_for_uuids(target_uuids)
            message = json.dumps({"type": "profiles", "profiles": profiles})
            try:
                await ws.send_text(message)
            except Exception as e:
                logger.error(f"Failed to broadcast profiles: {e}")
