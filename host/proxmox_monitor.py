"""
Proxmox VM monitor - reads VM configs and statuses.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from config import PROXMOX_VM_CONFIG_DIR, PROXMOX_QM_CMD

logger = logging.getLogger(__name__)


class ProxmoxMonitor:
    """
    Monitors Proxmox VM configurations and statuses.

    - Reads VM configs from /etc/pve/qemu-server/*.conf
    - Parses hostpci entries for GPU passthrough (PCI addresses on HOST)
    - Checks VM running status via `qm list`
    - Maps GPU UUID to VM ID via config (uuid -> host_pci -> vm_id)
    """

    def __init__(self):
        # vm_id -> {"name": str, "pci_devices": [str], "status": str}
        self.vms: dict[str, dict] = {}
        # pci_address (on host) -> vm_id
        self.pci_to_vm: dict[str, str] = {}
        # gpu_uuid -> vm_id (built from config: uuid -> host_pci -> vm_id)
        self.uuid_to_vm: dict[str, str] = {}
        # gpu_uuid -> pci_address on host (from config)
        self.uuid_to_host_pci: dict[str, str] = {}
        # Set of running VM IDs
        self.running_vms: set[str] = set()

    def set_gpu_config(self, gpus: dict):
        """
        Set GPU configuration from config file.
        gpus: {uuid: {"name": str, "host_pci": str, "fan_profile": list}}
        """
        self.uuid_to_host_pci = {}
        for uuid, gpu_data in (gpus or {}).items():
            host_pci = gpu_data.get("host_pci")
            if host_pci:
                normalized = self._normalize_pci_address(host_pci)
                self.uuid_to_host_pci[uuid] = normalized
        self._rebuild_uuid_to_vm_map()

    def refresh(self):
        """Refresh VM configs and statuses from Proxmox"""
        self._load_vm_configs()
        self._update_vm_statuses()
        self._rebuild_pci_to_vm_map()
        self._rebuild_uuid_to_vm_map()

    def _load_vm_configs(self):
        """Load VM configurations from /etc/pve/qemu-server/"""
        config_dir = Path(PROXMOX_VM_CONFIG_DIR)

        if not config_dir.exists():
            logger.warning(f"Proxmox config dir not found: {config_dir}")
            return

        for config_file in config_dir.glob("*.conf"):
            vm_id = config_file.stem
            try:
                self._parse_vm_config(vm_id, config_file)
            except Exception as e:
                logger.warning(f"Failed to parse VM {vm_id} config: {e}")

    def _parse_vm_config(self, vm_id: str, config_path: Path):
        """Parse a single VM config file"""
        with open(config_path) as f:
            content = f.read()

        # Extract VM name
        name = f"VM-{vm_id}"
        name_match = re.search(r'^name:\s*(.+)$', content, re.MULTILINE)
        if name_match:
            name = name_match.group(1).strip()

        # Extract hostpci entries
        pci_devices = []
        for match in re.finditer(r'^hostpci\d+:\s*([^,\s]+)', content, re.MULTILINE):
            pci_addr = match.group(1).strip()
            pci_addr = self._normalize_pci_address(pci_addr)
            pci_devices.append(pci_addr)

        self.vms[vm_id] = {
            "name": name,
            "pci_devices": pci_devices,
            "status": self.vms.get(vm_id, {}).get("status", "stopped")
        }

    def _normalize_pci_address(self, addr: str) -> str:
        """Normalize PCI address to format: 0000:00:00.0"""
        addr = addr.strip()

        if ':' not in addr:
            return addr

        parts = addr.split(':')
        if len(parts) == 3:
            domain, bus, dev_func = parts
        elif len(parts) == 2:
            domain = "0000"
            bus, dev_func = parts
        else:
            return addr

        domain = domain.zfill(4)

        if '.' not in dev_func:
            dev_func = dev_func + ".0"

        return f"{domain}:{bus}:{dev_func}"

    def _update_vm_statuses(self):
        """Update VM running statuses via `qm list`"""
        try:
            result = subprocess.run(
                [PROXMOX_QM_CMD, "list"],
                capture_output=True, text=True, timeout=10
            )

            if result.returncode != 0:
                logger.warning(f"qm list failed: {result.stderr}")
                return

            self.running_vms.clear()

            for line in result.stdout.strip().split('\n')[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    vm_id = parts[0]
                    status = parts[2].lower()

                    if vm_id in self.vms:
                        self.vms[vm_id]["status"] = status

                    if status == "running":
                        self.running_vms.add(vm_id)

        except FileNotFoundError:
            logger.warning("qm command not found - VM status detection disabled")
        except subprocess.TimeoutExpired:
            logger.warning("qm list timeout")
        except Exception as e:
            logger.warning(f"Failed to get VM statuses: {e}")

    def _rebuild_pci_to_vm_map(self):
        """
        Rebuild PCI address -> VM ID mapping from Proxmox configs.
        Running VMs have priority over stopped VMs.
        """
        self.pci_to_vm.clear()

        # First, add stopped VMs
        for vm_id, vm_data in self.vms.items():
            if vm_id in self.running_vms:
                continue  # Skip running, add later with priority
            for pci_addr in vm_data.get("pci_devices", []):
                normalized = self._normalize_pci_address(pci_addr)
                if normalized not in self.pci_to_vm:  # Don't overwrite
                    self.pci_to_vm[normalized] = vm_id

        # Then, add running VMs (they overwrite stopped)
        for vm_id in self.running_vms:
            vm_data = self.vms.get(vm_id)
            if vm_data:
                for pci_addr in vm_data.get("pci_devices", []):
                    normalized = self._normalize_pci_address(pci_addr)
                    self.pci_to_vm[normalized] = vm_id  # Running overwrites

    def _rebuild_uuid_to_vm_map(self):
        """
        Rebuild UUID -> VM ID mapping.
        Uses: uuid -> host_pci (from config) -> vm_id (from Proxmox)
        """
        self.uuid_to_vm.clear()

        for uuid, host_pci in self.uuid_to_host_pci.items():
            if host_pci in self.pci_to_vm:
                vm_id = self.pci_to_vm[host_pci]
                self.uuid_to_vm[uuid] = vm_id
                print(f"GPU {uuid[:8]}... -> PCI {host_pci} -> VM {vm_id}")
            else:
                print(f"GPU {uuid[:8]}... -> PCI {host_pci} -> FREE")

    def get_gpu_status(self, uuid: str) -> dict:
        """
        Get GPU assignment status.
        Returns: {"status": "free"} or {"status": "vm", "vm_id": "102", "vm_name": "...", "vm_running": True}
        """
        vm_id = self.uuid_to_vm.get(uuid)
        if not vm_id:
            return {"status": "free"}

        vm_info = self.vms.get(vm_id, {})
        return {
            "status": "vm",
            "vm_id": vm_id,
            "vm_name": vm_info.get("name", f"VM-{vm_id}"),
            "vm_running": vm_id in self.running_vms
        }

    def get_gpus_for_running_vms(self) -> list[str]:
        """Get all GPU UUIDs assigned to running VMs"""
        return [uuid for uuid, vm_id in self.uuid_to_vm.items()
                if vm_id in self.running_vms]

    def get_running_vms_with_pci(self) -> dict[str, list[str]]:
        """Get running VMs with their PCI devices: {vm_id: [pci_addr, ...]}"""
        result = {}
        for vm_id in self.running_vms:
            vm_data = self.vms.get(vm_id)
            if vm_data and vm_data.get("pci_devices"):
                result[vm_id] = vm_data["pci_devices"]
        return result

    def find_vm_by_host_pci(self, host_pci: str) -> Optional[str]:
        """Find VM ID by host PCI address"""
        normalized = self._normalize_pci_address(host_pci)
        return self.pci_to_vm.get(normalized)
