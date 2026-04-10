#!/usr/bin/env python3
"""
GPU Fan Control - VM Client (Windows/Linux)

Connects to host server via WebSocket, sends telemetry, receives fan profiles.

Platform-specific temperature reading:
- Windows: LibreHardwareMonitorLib via pythonnet
- Linux: pyngjvt package for junction/VRAM temps

NVML is used for:
- GPU UUID identification
- Fan control (speed setting, manual/auto mode)
- Fan speed reading
- Power consumption reading
- PCI address in VM

Usage:
    python fanc_client.py

Windows requirements:
    - pynvml, websockets, pythonnet
    - LibreHardwareMonitorLib.dll + dependencies in windows/ folder

Linux requirements:
    - pynvml, websockets, pyngjvt
    - Root privileges (for /dev/mem access)
    - iomem=relaxed kernel parameter OR disabled Secure Boot
"""

# ============================================================================
# CONFIG - Edit these values
# ============================================================================

SERVER_URL = "ws://192.168.10.1:17006/ws/vm"    # WebSocket URL
POLL_INTERVAL = 2.0                             # Seconds between readings
EMERGENCY_TIMEOUT = 5.0                         # No server -> fans 100%
RECONNECT_DELAY = 3.0                           # Seconds between reconnects
PING_INTERVAL = 10.0                            # Seconds between pings
PONG_TIMEOUT = 15.0                             # Seconds to wait for pong

# ============================================================================

import asyncio
import json
import logging
import platform
import re
import sys
import time
from pathlib import Path
from typing import Optional, Dict

import websockets.exceptions

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Changed from WARNING to INFO for debugging
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Temperature Smoother (EMA - Exponential Moving Average)
# ============================================================================

class TemperatureSmoother:
    """
    Smooths temperature readings using Exponential Moving Average.
    This prevents rapid fluctuations and provides stable fan control.
    """
    
    def __init__(self, alpha: float = 0.8):
        """
        Initialize smoother.
        
        Args:
            alpha: Smoothing factor (0-1). Lower = more smoothing.
                   0.3 means 30% new value, 70% previous smoothed value.
        """
        self.alpha = alpha
        self._smoothed: Dict[str, float] = {}
    
    def smooth(self, key: str, value: Optional[float]) -> Optional[float]:
        """
        Apply EMA smoothing to a value.
        
        Args:
            key: Unique identifier (e.g., "gpu0_core", "gpu1_memory")
            value: New temperature reading (or None if unavailable)
        
        Returns:
            Smoothed temperature value
        """
        if value is None:
            return self._smoothed.get(key)  # Return last known smoothed value
        
        if key not in self._smoothed:
            self._smoothed[key] = value
            return value
        
        # EMA formula: new_smoothed = alpha * new_value + (1 - alpha) * old_smoothed
        self._smoothed[key] = self.alpha * value + (1 - self.alpha) * self._smoothed[key]
        return self._smoothed[key]
    
    def reset(self):
        """Reset all smoothed values"""
        self._smoothed.clear()


# ============================================================================
# NVML Manager - Fan Control & Power (Platform-independent)
# ============================================================================

class NvmlManager:
    """
    NVML manager for fan control, power readings, UUID and PCI address.
    Works on both Windows and Linux.
    """

    def __init__(self):
        self.gpus = []
        self._manual_gpus = set()
        self._pynvml = None
        self._smoother = TemperatureSmoother()

    def initialize(self):
        """Initialize NVML and detect all GPUs"""
        try:
            import pynvml
            self._pynvml = pynvml
        except ImportError:
            logger.error("pynvml not installed. Run: pip install nvidia-ml-py")
            sys.exit(1)

        pynvml.nvmlInit()

        count = pynvml.nvmlDeviceGetCount()
        print(f"NVML: Found {count} GPU(s)")

        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            uuid = pynvml.nvmlDeviceGetUUID(handle)
            name = pynvml.nvmlDeviceGetName(handle)

            if isinstance(uuid, bytes):
                uuid = uuid.decode()
            if isinstance(name, bytes):
                name = name.decode()

            # Get PCI address in VM
            pci_in_vm = self._get_pci_address(handle, pynvml)

            # Get fan count
            try:
                num_fans = pynvml.nvmlDeviceGetNumFans(handle)
            except pynvml.NVMLError:
                num_fans = 1

            self.gpus.append({
                'handle': handle,
                'uuid': uuid,
                'name': name,
                'index': i,
                'num_fans': num_fans,
                'pci_in_vm': pci_in_vm,
                'profile': []
            })

            print(f"  [{i}] {name}: {uuid}")
            if pci_in_vm:
                print(f"      PCI in VM: {pci_in_vm}")

    def _get_pci_address(self, handle, pynvml) -> Optional[str]:
        """Get PCI address as seen in the VM"""
        try:
            pci_info = pynvml.nvmlDeviceGetPciInfo(handle)
            bus_id = pci_info.busId
            if isinstance(bus_id, bytes):
                bus_id = bus_id.decode('utf-8')

            # Parse bus ID (format: domain:bus:device.function)
            match = re.match(r'([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)\.([0-9a-fA-F]+)', bus_id)
            if match:
                domain = int(match.group(1), 16)
                bus = int(match.group(2), 16)
                device = int(match.group(3), 16)
                function = int(match.group(4), 16)
                return f"{domain:04x}:{bus:02x}:{device:02x}.{function}"
        except Exception as e:
            logger.debug(f"Failed to get PCI address: {e}")
        return None

    def get_fan_speed(self, index: int) -> Optional[int]:
        """Get current fan speed percentage for GPU by index"""
        for gpu in self.gpus:
            if gpu['index'] == index:
                try:
                    speeds = []
                    for fan_idx in range(gpu['num_fans']):
                        speed = self._pynvml.nvmlDeviceGetFanSpeed_v2(gpu['handle'], fan_idx)
                        speeds.append(speed)
                    return int(sum(speeds) / len(speeds)) if speeds else None
                except self._pynvml.NVMLError:
                    return None
        return None

    def get_power(self, index: int) -> Optional[float]:
        """Get current power consumption in watts"""
        for gpu in self.gpus:
            if gpu['index'] == index:
                try:
                    power_mw = self._pynvml.nvmlDeviceGetPowerUsage(gpu['handle'])
                    return power_mw / 1000
                except self._pynvml.NVMLError:
                    return None
        return None

    def set_fan_speed(self, index: int, speed: int):
        """Set fan speed (0-100) for GPU by index"""
        for gpu in self.gpus:
            if gpu['index'] == index:
                if index not in self._manual_gpus:
                    if not self._enable_manual_mode(gpu):
                        print(f"  [GPU{index}] WARNING: Failed to enable manual mode, trying to set speed anyway")
                    self._manual_gpus.add(index)

                for fan_idx in range(gpu['num_fans']):
                    try:
                        self._pynvml.nvmlDeviceSetFanSpeed_v2(gpu['handle'], fan_idx, speed)
                    except self._pynvml.NVMLError as e:
                        print(f"  [GPU{index}] Fan {fan_idx}: FAILED to set speed {speed}%: {e}")
                return

    def _enable_manual_mode(self, gpu: dict) -> bool:
        """Enable manual fan control policy. Returns True on success."""
        success = True
        for fan_idx in range(gpu['num_fans']):
            try:
                self._pynvml.nvmlDeviceSetFanControlPolicy(
                    gpu['handle'], fan_idx, self._pynvml.NVML_FEATURE_ENABLED
                )
                print(f"  [GPU{gpu['index']}] Fan {fan_idx}: Manual mode ENABLED")
            except self._pynvml.NVMLError as e:
                print(f"  [GPU{gpu['index']}] Fan {fan_idx}: FAILED to enable manual mode: {e}")
                success = False
        return success

    def set_fan_auto(self, index: int):
        """Set fan to auto mode"""
        for gpu in self.gpus:
            if gpu['index'] == index:
                for fan_idx in range(gpu['num_fans']):
                    try:
                        self._pynvml.nvmlDeviceSetFanControlPolicy(
                            gpu['handle'], fan_idx, self._pynvml.NVML_FEATURE_DISABLED
                        )
                        print(f"  [GPU{index}] Fan {fan_idx}: AUTO mode enabled")
                    except self._pynvml.NVMLError as e:
                        print(f"  [GPU{index}] Fan {fan_idx}: FAILED to set auto mode: {e}")
                self._manual_gpus.discard(index)
                return

    def update_profiles(self, profiles: dict):
        """Update fan profiles from server response.
        
        Only updates GPUs that are present in the received profiles dict.
        GPUs not in the dict keep their existing profiles unchanged.
        """
        for uuid, new_profile in profiles.items():
            # Find GPU by UUID
            for gpu in self.gpus:
                if gpu['uuid'] == uuid:
                    old_profile = gpu['profile']
                    
                    if new_profile != old_profile:
                        gpu['profile'] = new_profile
                        index = gpu['index']

                        if new_profile:
                            # Force manual mode when profile changes from empty to non-empty
                            if index in self._manual_gpus:
                                print(f"[GPU{index}] Profile updated (already in manual mode): {new_profile}")
                            else:
                                print(f"[GPU{index}] Profile received, switching to manual mode: {new_profile}")
                                self._enable_manual_mode(gpu)
                                self._manual_gpus.add(index)
                        else:
                            self.set_fan_auto(index)
                            print(f"[GPU{index}] Profile empty, switched to AUTO mode")
                    break

    def calculate_fan_speed(self, temp: int, profile: list) -> int:
        """
        Calculate fan speed from temperature using profile.
        
        Edge cases:
        - Temperature left of first point -> 0% (fans off or minimal)
        - Temperature right of last point -> 100% (max cooling)
        
        Returns -1 for auto mode (empty profile).
        """
        if not profile:
            return -1

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

    def update_fans(self, temps: dict[int, dict], emergency: bool = False) -> dict[int, int]:
        """
        Update fan speeds based on smoothed temperatures.
        
        Args:
            temps: {gpu_index: {"temp_core": X, "temp_hotspot": Y, "temp_memory": Z}}
            emergency: If True, set all fans to 100%
        
        Returns:
            {gpu_index: target_pwm} - calculated target PWM for each GPU
        """
        target_pwms = {}
        
        for gpu in self.gpus:
            index = gpu['index']
            profile = gpu['profile']
            target_pwm = -1  # -1 means auto

            if emergency:
                self.set_fan_speed(index, 100)
                target_pwm = 100
            elif not profile:
                # Auto mode - only switch if not already in auto
                if index in self._manual_gpus:
                    self.set_fan_auto(index)
            else:
                gpu_temps = temps.get(index, {})
                
                # Smooth each temperature separately
                smoothed_core = self._smoother.smooth(f"gpu{index}_core", gpu_temps.get("temp_core"))
                smoothed_hotspot = self._smoother.smooth(f"gpu{index}_hotspot", gpu_temps.get("temp_hotspot"))
                smoothed_memory = self._smoother.smooth(f"gpu{index}_memory", gpu_temps.get("temp_memory"))
                
                # Use max of smoothed temperatures for fan control
                max_smoothed = max(
                    smoothed_core or 0,
                    smoothed_hotspot or 0,
                    smoothed_memory or 0
                )
                
                if max_smoothed > 0:
                    speed = self.calculate_fan_speed(int(max_smoothed), profile)
                    if speed >= 0:
                        self.set_fan_speed(index, speed)
                        target_pwm = speed
            
            target_pwms[index] = target_pwm
        
        return target_pwms

    def shutdown(self):
        """Shutdown NVML - return all fans to auto mode"""
        for gpu in self.gpus:
            self.set_fan_auto(gpu['index'])
        if self._pynvml:
            try:
                self._pynvml.nvmlShutdown()
            except:
                pass

    def get_gpus_info(self) -> list[dict]:
        """Get list of all GPUs with UUID, name and PCI address"""
        return [
            {
                'uuid': gpu['uuid'],
                'name': gpu['name'],
                'pci_in_vm': gpu['pci_in_vm']
            }
            for gpu in self.gpus
        ]


# ============================================================================
# Windows Temperature Reader (LibreHardwareMonitor)
# ============================================================================

class WindowsTempReader:
    """Temperature reader using LibreHardwareMonitorLib"""

    def __init__(self):
        self.computer = None

    def initialize(self):
        """Initialize LibreHardwareMonitor"""
        try:
            import clr
        except ImportError:
            logger.error("pythonnet not installed. Run: pip install pythonnet")
            sys.exit(1)

        # Find DLL in the same directory as this script
        script_dir = Path(__file__).parent
        dll_path = script_dir / "LibreHardwareMonitorLib.dll"

        if not dll_path.exists():
            logger.error(
                "LibreHardwareMonitorLib.dll not found!\n"
                f"Place DLLs in the same folder as fanc_client.py:\n"
                f"  {script_dir}\n"
                "Required files:\n"
                "  - LibreHardwareMonitorLib.dll\n"
                "  - HidSharp.dll\n"
                "  - System.Memory.dll\n"
                "  - System.Numerics.Vectors.dll\n"
                "  - System.Runtime.CompilerServices.Unsafe.dll\n"
                "Download from: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases"
            )
            sys.exit(1)

        logger.info(f"Loading: {dll_path}")
        clr.AddReference(str(dll_path))

        from LibreHardwareMonitor.Hardware import Computer

        self.computer = Computer()
        self.computer.IsCpuEnabled = False
        self.computer.IsGpuEnabled = True
        self.computer.IsMemoryEnabled = False
        self.computer.IsMotherboardEnabled = False
        self.computer.IsControllerEnabled = False
        self.computer.IsNetworkEnabled = False
        self.computer.IsStorageEnabled = False

        self.computer.Open()
        logger.info("LibreHardwareMonitor initialized")

    def get_temperatures(self, nvml_gpus: list) -> Dict[int, dict]:
        """
        Get temperatures for all GPUs.
        Matches GPU by name with NVML devices.

        Returns:
            {gpu_index: {"temp_core": X, "temp_hotspot": Y, "temp_memory": Z}}
        """
        results = {}

        try:
            for hardware in self.computer.Hardware:
                hardware.Update()

                gpu_name = hardware.Name
                temps = {}

                for sensor in hardware.Sensors:
                    sensor_type = str(sensor.SensorType).lower()
                    sensor_name = str(sensor.Name).lower()

                    if 'temperature' in sensor_type and sensor.Value is not None:
                        if 'core' in sensor_name:
                            temps['temp_core'] = round(sensor.Value)
                        elif 'hot' in sensor_name or ('junction' in sensor_name and 'memory' not in sensor_name):
                            temps['temp_hotspot'] = round(sensor.Value)
                        elif 'memory' in sensor_name:
                            temps['temp_memory'] = round(sensor.Value)

                if temps:
                    # Match with NVML GPU by name
                    for gpu in nvml_gpus:
                        if gpu['name'] in gpu_name or gpu_name in gpu['name']:
                            results[gpu['index']] = temps
                            break

        except Exception as e:
            logger.error(f"Error reading GPU temps: {e}")

        return results

    def close(self):
        """Close LibreHardwareMonitor"""
        if self.computer:
            self.computer.Close()
            self.computer = None


# ============================================================================
# Linux Temperature Reader (pyngjvt)
# ============================================================================

class LinuxTempReader:
    """
    Temperature reader using pyngjvt package.

    Uses pynvml for core temp and BAR address.
    Uses pyngjvt for junction and VRAM temps.
    """

    def __init__(self):
        self.lib = None
        self._pynvml = None
        self._initialized = False

    def initialize(self):
        """Initialize pyngjvt library"""
        try:
            import pyngjvt
            self.lib = pyngjvt
        except ImportError:
            logger.error(
                "pyngjvt not installed!\n"
                "Install: pip install pyngjvt\n"
                "https://github.com/AlexanderChad/ngjvt"
            )
            sys.exit(1)

        # Initialize library
        result = self.lib.ngjvt_init()
        if result != 0:
            logger.warning(
                "pyngjvt init failed. Junction/VRAM temps unavailable.\n"
                "Run with sudo and ensure:\n"
                "  - iomem=relaxed kernel parameter\n"
                "  - OR disabled Secure Boot"
            )
        else:
            self._initialized = True
            version = self.lib.ngjvt_version()
            print(f"pyngjvt v{version} initialized")

    def get_temperatures(self, nvml_gpus: list) -> Dict[int, dict]:
        """
        Get temperatures for all GPUs.
        Uses NVML for core temp and BAR address.
        Uses pyngjvt for junction and VRAM temps.
        """
        import re

        results = {}

        for gpu in nvml_gpus:
            idx = gpu['index']
            handle = gpu['handle']

            temps = {}

            # Core temperature from NVML
            try:
                temps['temp_core'] = self._pynvml.nvmlDeviceGetTemperature(
                    handle, self._pynvml.NVML_TEMPERATURE_GPU
                )
            except:
                temps['temp_core'] = None

            # Junction and VRAM from pyngjvt
            if self._initialized:
                try:
                    # Get BAR address from sysfs via NVML PCI info
                    pci_info = self._pynvml.nvmlDeviceGetPciInfo(handle)
                    bus_id = pci_info.busId.decode('utf-8') if isinstance(pci_info.busId, bytes) else pci_info.busId

                    match = re.match(r'([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)\.([0-9a-fA-F]+)', bus_id)
                    if match:
                        domain = int(match.group(1), 16)
                        bus = int(match.group(2), 16)
                        device = int(match.group(3), 16)
                        function = int(match.group(4), 16)

                        # Read BAR0 from sysfs
                        resource_path = f"/sys/bus/pci/devices/{domain:04x}:{bus:02x}:{device:02x}.{function}/resource"
                        with open(resource_path, 'r') as f:
                            bar_addr = int(f.readline().split()[0], 16)

                        if bar_addr:
                            junction = self.lib.ngjvt_get_junction_temp(bar_addr)
                            vram = self.lib.ngjvt_get_vram_temp(bar_addr)

                            temps['temp_hotspot'] = junction if junction >= 0 else None
                            temps['temp_memory'] = vram if vram >= 0 else None

                except Exception as e:
                    logger.debug(f"Failed to read junction/VRAM temps for GPU {idx}: {e}")

            results[idx] = temps

        return results

    def set_pynvml(self, pynvml_module):
        """Set pynvml reference for core temp reading"""
        self._pynvml = pynvml_module

    def close(self):
        """Shutdown pyngjvt library"""
        if self._initialized and self.lib:
            self.lib.ngjvt_shutdown()
            self._initialized = False


# ============================================================================
# WebSocket Client
# ============================================================================

class FanControlClient:
    """Main client connecting to host server"""

    def __init__(self):
        self.nvml = NvmlManager()
        self.temp_reader = None
        self.ws = None
        self._last_server_contact = time.time()
        self._last_ping_sent = time.time()
        self._last_pong_received = time.time()
        self._running = False
        self._connected = False
        self._connection_lock = asyncio.Lock()

    def _init_temp_reader(self):
        """Initialize platform-specific temperature reader"""
        system = platform.system()

        if system == "Windows":
            self.temp_reader = WindowsTempReader()
            self.temp_reader.initialize()
        elif system == "Linux":
            self.temp_reader = LinuxTempReader()
            self.temp_reader.initialize()
            self.temp_reader.set_pynvml(self.nvml._pynvml)
        else:
            logger.error(f"Unsupported platform: {system}")
            sys.exit(1)

    async def _disconnect(self):
        """Safely close WebSocket connection"""
        async with self._connection_lock:
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
                self.ws = None
            self._connected = False

    async def connect(self):
        """Connect to WebSocket server"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed. Run: pip install websockets")
            sys.exit(1)

        logger.info(f"Connecting to {SERVER_URL}...")

        try:
            # Use ping/pong for connection health
            self.ws = await websockets.connect(
                SERVER_URL,
                ping_interval=PING_INTERVAL,
                ping_timeout=PONG_TIMEOUT,
                close_timeout=1.0
            )
            logger.info("Connected to server")

            # Send init message with GPU info (including PCI addresses)
            gpus_info = self.nvml.get_gpus_info()

            await self.ws.send(json.dumps({
                "type": "init",
                "gpus": gpus_info
            }))
            logger.info(f"Sent init with {len(gpus_info)} GPU(s)")

            self._last_server_contact = time.time()
            self._last_pong_received = time.time()
            async with self._connection_lock:
                self._connected = True
            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            await self._disconnect()
            return False

    async def send_telemetry(self, gpus: list):
        """Send telemetry to server. Returns True on success, False on failure."""
        if not self.ws or not self._connected:
            return False

        try:
            await self.ws.send(json.dumps({
                "type": "telemetry",
                "gpus": gpus
            }))
            self._last_server_contact = time.time()
            return True
        except Exception as e:
            logger.error(f"Failed to send telemetry: {e}")
            await self._disconnect()
            return False

    async def receive_messages(self):
        """Receive messages from server (profiles). Returns when connection is lost."""
        if not self.ws:
            return

        try:
            async for message in self.ws:
                # Check if connection is still valid
                if not self._connected:
                    break
                    
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "profiles":
                    profiles = data.get("profiles", {})
                    self.nvml.update_profiles(profiles)
                    logger.info(f"Received profiles for {len(profiles)} GPU(s)")
                elif msg_type == "pong":
                    self._last_pong_received = time.time()

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        
        # Connection lost
        await self._disconnect()

    async def _check_connection_health(self) -> bool:
        """Check if connection is healthy. Returns False if need to reconnect."""
        if not self.ws or not self._connected:
            return False
            
        # In websockets 11+, we can't check .closed directly
        # Connection health is detected via send failures or receive exceptions
        return True

    async def run(self):
        """Main client loop"""
        print("=" * 60)
        print("GPU Fan Control Client")
        print("=" * 60)
        print(f"Server: {SERVER_URL}")
        print(f"Poll interval: {POLL_INTERVAL}s")
        print(f"Emergency timeout: {EMERGENCY_TIMEOUT}s")
        print("")

        # Initialize components
        self.nvml.initialize()
        self._init_temp_reader()

        self._running = True
        receive_task = None

        while self._running:
            # Connect to server if not connected
            if not self._connected or not self.ws:
                # Cancel old receive task if exists
                if receive_task and not receive_task.done():
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                
                if not await self.connect():
                    # Enter emergency mode during reconnection
                    self.nvml.update_fans({}, emergency=True)
                    logger.info(f"Retrying in {RECONNECT_DELAY}s...")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
                
                # Start receive task
                receive_task = asyncio.create_task(self.receive_messages())

            try:
                # Check connection health before sending
                if not await self._check_connection_health():
                    logger.warning("Connection lost, reconnecting...")
                    await self._disconnect()
                    continue

                # Read temperatures
                temps_raw = self.temp_reader.get_temperatures(self.nvml.gpus)

                if temps_raw:
                    telemetry_gpus = []
                    temps_for_fan = {}  # Pass full temp data to update_fans

                    for gpu in self.nvml.gpus:
                        idx = gpu['index']
                        if idx in temps_raw:
                            temps = temps_raw[idx]
                            
                            # Store full temp data for fan control
                            temps_for_fan[idx] = temps

                    # Update fans FIRST to get target PWMs
                    server_timeout = (time.time() - self._last_server_contact) > EMERGENCY_TIMEOUT
                    target_pwms = self.nvml.update_fans(temps_for_fan, emergency=(server_timeout or False))
                    
                    if server_timeout:
                        logger.warning("Server timeout - emergency fan mode!")

                    # Now build telemetry with both actual and target
                    for gpu in self.nvml.gpus:
                        idx = gpu['index']
                        if idx in temps_raw:
                            temps = temps_raw[idx]
                            gpu_data = {"uuid": gpu['uuid']}
                            gpu_data.update(temps)

                            # Actual fan speed from sensor
                            fan = self.nvml.get_fan_speed(idx)
                            if fan is not None:
                                gpu_data["fan_percent"] = fan

                            # Target fan speed (calculated from profile)
                            target = target_pwms.get(idx, -1)
                            if target >= 0:
                                gpu_data["fan_target"] = target

                            power = self.nvml.get_power(idx)
                            if power is not None:
                                gpu_data["power_watts"] = power

                            telemetry_gpus.append(gpu_data)

                    # Send telemetry (this will disconnect on failure)
                    success = await self.send_telemetry(telemetry_gpus)
                    
                    # If send failed, enter emergency mode
                    if not success:
                        self.nvml.update_fans(temps_for_fan, emergency=True)

                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await self._disconnect()

        # Cleanup
        if receive_task and not receive_task.done():
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    def stop(self):
        """Stop the client"""
        self._running = False
        self.nvml.shutdown()
        if self.temp_reader:
            self.temp_reader.close()


# ============================================================================
# Main
# ============================================================================

def check_single_instance():
    """Ensure only one instance is running (Windows only, uses mutex)"""
    if platform.system() != "Windows":
        return True
    
    try:
        import ctypes
        from ctypes import wintypes
        
        kernel32 = ctypes.windll.kernel32
        
        # Try to create a named mutex
        mutex_name = "Global\\fanc_vm_single_instance"
        mutex = kernel32.CreateMutexW(None, False, mutex_name)
        
        last_error = kernel32.GetLastError()
        
        # ERROR_ALREADY_EXISTS = 183
        if last_error == 183:
            print("Another instance is already running. Exiting.")
            return False
        
        return True
    except Exception as e:
        logger.warning(f"Single instance check failed: {e}")
        return True  # Allow to run if check fails


async def main():
    client = FanControlClient()

    try:
        await client.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        client.stop()


if __name__ == "__main__":
    if not check_single_instance():
        sys.exit(0)
    asyncio.run(main())
