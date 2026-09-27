"""
FastAPI server with WebSocket endpoints.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from config import SERVER_HOST, SERVER_PORT
from proxmox_monitor import ProxmoxMonitor
from statsd_exporter import StatsDExporter
from cpu_reader import CpuTempReader
from state import StateManager
from pwm_controller import PwmController

logger = logging.getLogger(__name__)

# Global components
proxmox: Optional[ProxmoxMonitor] = None
statsd: Optional[StatsDExporter] = None
cpu_reader: Optional[CpuTempReader] = None
state: Optional[StateManager] = None
pwm: Optional[PwmController] = None

# Background tasks
pwm_task: Optional[asyncio.Task] = None
proxmox_task: Optional[asyncio.Task] = None


async def pwm_update_loop():
    """Background task for PWM updates"""
    while True:
        try:
            # Check emergency mode
            emergency, reason = state.check_emergency_mode()
            
            state.emergency_mode = emergency
            state.emergency_reason = reason

            if emergency:
                pwm.set_emergency()
                state.case_fan_pwm_percent = 100  # EMERGENCY_PWM
                logger.warning(f"EMERGENCY: {reason}")
            else:
                # CPU temps -> CPU PWM
                cpu_temps = cpu_reader.get_all_temperatures()
                cpu_temp = max(cpu_temps.values()) if cpu_temps else 0
                cpu_pwm = pwm.calculate_pwm_from_profile(cpu_temp, state.cpu_fan_profile)

                state.cpu_temps = cpu_temps
                state.cpu_temp = cpu_temp
                state.cpu_pwm_percent = cpu_pwm
                statsd.send_cpu_metric(cpu_temp)

                # GPU fans: PWM is calculated and set by VM clients, we use their actual fan_percent
                # Case fans: max of CPU PWM and all GPU fan_percents
                gpu_pwm = state.get_max_gpu_pwm()
                statsd.send_gpu_group_metric(state.get_max_gpu_temp())

                # Final PWM = max(cpu_pwm, all_gpu_pwms)
                # This ensures case fans spin up for any hot component
                final_pwm = max(cpu_pwm, gpu_pwm)
                pwm.set_fan_speed(final_pwm)
                state.case_fan_pwm_percent = final_pwm

            # Broadcast to UI on every cycle (for real-time updates)
            await state.broadcast_to_ui()

        except Exception as e:
            logger.error(f"PWM update error: {e}")

        await asyncio.sleep(2)


async def proxmox_update_loop():
    """Background task for Proxmox VM status updates"""
    while True:
        try:
            proxmox.refresh()
        except Exception as e:
            logger.error(f"Proxmox update error: {e}")
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler (replaces deprecated on_event)"""
    global proxmox, statsd, cpu_reader, state, pwm, pwm_task, proxmox_task

    # Startup
    logger.info("Fan Control - Host Server starting")

    proxmox = ProxmoxMonitor()
    proxmox.refresh()
    logger.info(f"Proxmox VMs: {len(proxmox.vms)}, Running: {len(proxmox.running_vms)}")

    statsd = StatsDExporter()
    state = StateManager(proxmox, statsd)
    state.load_config()

    statsd.connect()

    cpu_reader = CpuTempReader()
    cpu_reader.initialize()

    pwm = PwmController(statsd)
    pwm.initialize()

    # Start background tasks
    pwm_task = asyncio.create_task(pwm_update_loop())
    proxmox_task = asyncio.create_task(proxmox_update_loop())
    logger.info(f"Background tasks started. Web UI: http://{SERVER_HOST}:{SERVER_PORT}/, VM WebSocket: ws://{SERVER_HOST}:{SERVER_PORT}/ws/vm")

    yield

    # Shutdown
    if pwm_task:
        pwm_task.cancel()
    if proxmox_task:
        proxmox_task.cancel()

    for ws in state.ui_connections:
        try:
            await ws.send_text(json.dumps({"type": "shutdown"}))
        except:
            pass

    pwm.set_auto_mode()
    statsd.close()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(title="Fan Control", lifespan=lifespan)

    script_dir = Path(__file__).parent
    static_dir = script_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = script_dir / "templates" / "index.html"
        if html_path.exists():
            return FileResponse(str(html_path), media_type="text/html")
        return HTMLResponse("<h1>Template not found</h1>", status_code=404)

    @app.websocket("/ws/ui")
    async def websocket_ui(websocket: WebSocket):
        await websocket.accept()
        state.ui_connections.append(websocket)

        initial_state = await state.get_state()
        await websocket.send_text(json.dumps({"type": "state", "data": initial_state}))

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "update_profile":
                    uuid = message.get("uuid")
                    profile = message.get("profile", [])
                    if uuid in state.gpu_config:
                        state.gpu_config[uuid]["fan_profile"] = profile
                        state.save_config()
                        await state.broadcast_profiles_to_vms([uuid])
                        await state.broadcast_to_ui()

                elif msg_type == "update_cpu_profile":
                    state.cpu_fan_profile = message.get("profile", [])
                    state.save_config()
                    await state.broadcast_to_ui()

                elif msg_type == "update_name":
                    uuid = message.get("uuid")
                    name = message.get("name", "")
                    if uuid in state.gpu_config:
                        state.gpu_config[uuid]["name"] = name
                        state.save_config()
                        await state.broadcast_to_ui()

                elif msg_type == "update_host_pci":
                    uuid = message.get("uuid")
                    host_pci = message.get("host_pci", "")
                    if uuid in state.gpu_config:
                        state.gpu_config[uuid]["host_pci"] = host_pci
                        state.save_config()
                        proxmox.set_gpu_config(state.gpu_config)
                        await state.broadcast_to_ui()

        except WebSocketDisconnect:
            pass
        finally:
            if websocket in state.ui_connections:
                state.ui_connections.remove(websocket)

    @app.websocket("/ws/vm")
    async def websocket_vm(websocket: WebSocket):
        await websocket.accept()
        state.vm_connections.append(websocket)

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "init":
                    # VM client initialization with GPU info
                    gpus_info = message.get("gpus", [])

                    # Register each GPU first
                    for gpu_info in gpus_info:
                        uuid = gpu_info.get("uuid")
                        vm_pci = gpu_info.get("pci_in_vm", "")
                        name = gpu_info.get("name", "")

                        if uuid and vm_pci:
                            state.register_gpu(uuid, vm_pci, name)

                    # After registration, get all known GPUs for this connection
                    uuids = [g.get("uuid") for g in gpus_info if g.get("uuid")]
                    known_uuids = [u for u in uuids if state.is_known_gpu(u)]
                    state.ws_uuids[websocket] = known_uuids

                    logger.info(f"VM client registered: {len(known_uuids)} GPU(s)")
                    
                    # Send profiles for all known GPUs (including newly registered)
                    if known_uuids:
                        await state.send_profiles_to_vm(websocket, known_uuids)

                elif msg_type == "telemetry":
                    await state.update_telemetry(message)
                    await state.broadcast_to_ui()

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"VM client error: {e}")
        finally:
            if websocket in state.vm_connections:
                state.vm_connections.remove(websocket)
            if websocket in state.ws_uuids:
                del state.ws_uuids[websocket]

    @app.get("/config")
    async def get_config():
        return {
            "gpus": state.gpu_config,
            "cpu_fan_profile": state.cpu_fan_profile
        }

    @app.post("/config")
    async def update_config(data: dict):
        if "gpus" in data:
            state.gpu_config = data["gpus"]
            proxmox.set_gpu_config(state.gpu_config)

        if "cpu_fan_profile" in data:
            state.cpu_fan_profile = data["cpu_fan_profile"]

        state.save_config()
        await state.broadcast_to_ui()

        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "gpus_configured": len(state.gpu_config),
            "gpus_online": sum(1 for u in state.gpu_config if state.is_gpu_online(u)),
            "cpu_temp": state.cpu_temp,
            "pwm": state.case_fan_pwm_percent,
            "emergency": state.emergency_mode,
            "emergency_reason": state.emergency_reason,
            "vms_total": len(proxmox.vms),
            "vms_running": list(proxmox.running_vms)
        }

    return app
