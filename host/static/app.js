// ============================================================================
// Fan Control - Web UI Client
// ============================================================================

let ws;
let state = null;
let editors = {};
let cpuProfileEditor = null;
let editingUuid = null;  // Track which GPU name is being edited
let editingProfile = null;  // Track which profile is being edited (uuid or 'cpu')
let coordsTimeout = null;  // For hiding coords after 3 seconds
let expandedProfiles = {};  // Track which profiles are expanded

// Default profile: 3 points
const DEFAULT_PROFILE = [[50, 30], [70, 70], [85, 100]];

function connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(protocol + '//' + window.location.host + '/ws/ui');

    ws.onopen = () => {
        document.getElementById('statusDot').classList.add('connected');
        document.getElementById('statusText').textContent = 'Connected';
    };

    ws.onclose = () => {
        document.getElementById('statusDot').classList.remove('connected');
        document.getElementById('statusText').textContent = 'Disconnected - Reconnecting...';
        setTimeout(connect, 2000);
    };

    ws.onerror = (err) => console.error('WebSocket error:', err);

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'state') {
            state = msg.data;
            updateDisplay(state);
        }
    };
}

function getTempClass(temp) {
    if (temp === null || temp === undefined) return '';
    if (temp < 60) return 'cool';
    if (temp < 80) return 'warm';
    return 'hot';
}

function getGpuStatusBadge(gpu) {
    if (!gpu.is_online) {
        if (gpu.gpu_status && gpu.gpu_status.status === 'vm') {
            const vmName = gpu.vm_name || `VM ${gpu.vm_id}`;
            const runningText = gpu.vm_running ? ' (running)' : ' (stopped)';
            return `<span class="gpu-status-badge offline">${vmName}${runningText} - OFFLINE</span>`;
        }
        return `<span class="gpu-status-badge offline">OFFLINE</span>`;
    }

    if (gpu.gpu_status && gpu.gpu_status.status === 'vm') {
        const vmName = gpu.vm_name || `VM ${gpu.vm_id}`;
        if (gpu.vm_running) {
            return `<span class="gpu-status-badge vm running">${vmName}</span>`;
        } else {
            return `<span class="gpu-status-badge vm">${vmName} (stopped)</span>`;
        }
    }

    return `<span class="gpu-status-badge free">FREE</span>`;
}

function formatTime(isoString) {
    if (!isoString) return 'Never';
    return new Date(isoString).toLocaleTimeString('ru-RU');
}

function updateDisplay(data) {
    // Update case fans PWM
    if (data.case_fans) {
        const pwmVal = data.case_fans.pwm_percent;
        document.getElementById('caseFansPwm').textContent = (pwmVal !== null && pwmVal !== undefined) ? pwmVal : '--';
    }

    // Update emergency banner
    if (data.emergency) {
        const banner = document.getElementById('emergencyBanner');
        if (data.emergency.active) {
            banner.classList.add('active');
            document.getElementById('emergencyReason').textContent = 'GPU in running VM is not sending telemetry';
        } else {
            banner.classList.remove('active');
        }
    }

    // Update CPU group card
    if (data.cpu) {
        document.getElementById('cpuGroupTemp').textContent = data.cpu.temp || '--';
        document.getElementById('cpuGroupPwm').textContent = (data.cpu.pwm_percent !== null && data.cpu.pwm_percent !== undefined) ? data.cpu.pwm_percent : '--';

        if (data.cpu.temps_by_socket) {
            const socketList = document.getElementById('cpuSocketList');
            const sockets = Object.entries(data.cpu.temps_by_socket);
            if (sockets.length > 0) {
                socketList.innerHTML = sockets.map(([id, temp]) => `
                    <div class="cpu-socket-item">
                        <span class="cpu-socket-name">Socket ${id}</span>
                        <span class="cpu-socket-temp">${temp > 0 ? temp + '°C' : '--'}</span>
                    </div>
                `).join('');
            } else {
                socketList.innerHTML = '<div style="color:#666;text-align:center;">No data</div>';
            }
        }

        // Don't reinitialize editor if editing cpu profile
        if (!cpuProfileEditor && editingProfile !== 'cpu') {
            const profile = data.cpu.profile;
            cpuProfileEditor = {
                points: normalizeToThreePoints(profile),
                dragging: -1,
                isAuto: !profile || profile.length === 0
            };
            drawGroupProfile('cpu');
            setupCanvasEvents('cpu', true);
        }
    }

    // Update GPU cards
    const container = document.getElementById('gpuCards');

    if (!data.gpus || data.gpus.length === 0) {
        container.innerHTML = '<div class="no-data">No GPUs configured</div>';
        return;
    }

    // Check if we're editing name or profile - don't rebuild the entire card
    if (editingUuid || editingProfile) {
        data.gpus.forEach(gpu => {
            if (gpu.uuid === editingUuid || gpu.uuid === editingProfile) return;
            updateGpuCardData(gpu);
        });
        return;
    }

    // Full rebuild
    container.innerHTML = data.gpus.map(gpu => renderGpuCard(gpu)).join('');

    // Initialize editors for GPUs
    data.gpus.forEach(gpu => {
        if (!editors[gpu.uuid]) {
            const profile = gpu.fan_profile;
            editors[gpu.uuid] = {
                points: normalizeToThreePoints(profile),
                dragging: -1,
                isAuto: !profile || profile.length === 0
            };
        }
        drawProfile(gpu.uuid);
        setupCanvasEvents(gpu.uuid, false);
    });
}

function normalizeToThreePoints(profile) {
    if (!profile || profile.length === 0) {
        return DEFAULT_PROFILE.slice();
    }
    if (profile.length === 3) {
        return profile.slice();
    }
    if (profile.length < 3) {
        const result = profile.slice();
        while (result.length < 3) {
            const last = result[result.length - 1] || [50, 30];
            result.push([last[0] + 15, Math.min(100, last[1] + 20)]);
        }
        return result;
    }
    return [profile[0], profile[Math.floor(profile.length / 2)], profile[profile.length - 1]];
}

function updateGpuCardData(gpu) {
    const card = document.querySelector(`[data-uuid="${gpu.uuid}"]`);
    if (!card) return;

    const tempCore = card.querySelector('.temp-item:nth-child(1) .temp-value');
    const tempHotspot = card.querySelector('.temp-item:nth-child(2) .temp-value');
    const tempMemory = card.querySelector('.temp-item:nth-child(3) .temp-value');

    if (tempCore) {
        tempCore.innerHTML = `${gpu.temp_core !== null ? gpu.temp_core : '--'}<span class="temp-unit">°C</span>`;
        tempCore.className = `temp-value ${getTempClass(gpu.temp_core)}`;
    }
    if (tempHotspot) {
        tempHotspot.innerHTML = `${gpu.temp_hotspot !== null ? gpu.temp_hotspot : '--'}<span class="temp-unit">°C</span>`;
        tempHotspot.className = `temp-value ${getTempClass(gpu.temp_hotspot)}`;
    }
    if (tempMemory) {
        tempMemory.innerHTML = `${gpu.temp_memory !== null ? gpu.temp_memory : '--'}<span class="temp-unit">°C</span>`;
        tempMemory.className = `temp-value ${getTempClass(gpu.temp_memory)}`;
    }

    const infoValues = card.querySelectorAll('.info-value');
    if (infoValues.length >= 2) {
        // Fan: actual%, target: X%
        let fanText = gpu.fan_percent !== null ? gpu.fan_percent + '%' : '--';
        if (gpu.fan_target !== null && gpu.fan_target !== undefined && gpu.fan_target >= 0) {
            fanText += ', target: ' + gpu.fan_target + '%';
        }
        infoValues[0].textContent = fanText;
        infoValues[1].textContent = gpu.power_watts !== null ? gpu.power_watts.toFixed(1) + 'W' : '--';
    }

    const lastSeen = card.querySelector('.last-seen');
    if (lastSeen) {
        lastSeen.innerHTML = `Updated: ${formatTime(gpu.last_seen)}${!gpu.is_online ? ' <span style="color:#ff4444">(OFFLINE)</span>' : ''}`;
    }

    card.classList.toggle('online', gpu.is_online);
    card.classList.toggle('offline', !gpu.is_online);
}

function renderGpuCard(gpu) {
    const isExpanded = expandedProfiles[gpu.uuid] ? 'expanded' : '';
    const iconClass = expandedProfiles[gpu.uuid] ? 'expanded' : '';
    return `
        <div class="gpu-card ${gpu.is_online ? 'online' : 'offline'}" data-uuid="${gpu.uuid}">
            <div class="gpu-name-row">
                <div>
                    <span class="gpu-display-name ${gpu.name ? '' : 'empty'}" id="name-${gpu.uuid}">
                        ${gpu.name || 'Unknown GPU'}
                    </span>
                    ${getGpuStatusBadge(gpu)}
                </div>
                <button class="edit-name-btn" onclick="editName('${gpu.uuid}')" title="Edit name">✎</button>
            </div>
            <div class="gpu-uuid">${gpu.uuid}</div>

            <div class="temp-grid">
                <div class="temp-item">
                    <div class="temp-label">Core</div>
                    <div class="temp-value ${getTempClass(gpu.temp_core)}">
                        ${gpu.temp_core !== null && gpu.temp_core !== undefined ? gpu.temp_core : '--'}
                        <span class="temp-unit">°C</span>
                    </div>
                </div>
                <div class="temp-item">
                    <div class="temp-label">Hotspot</div>
                    <div class="temp-value ${getTempClass(gpu.temp_hotspot)}">
                        ${gpu.temp_hotspot !== null && gpu.temp_hotspot !== undefined ? gpu.temp_hotspot : '--'}
                        <span class="temp-unit">°C</span>
                    </div>
                </div>
                <div class="temp-item">
                    <div class="temp-label">Memory</div>
                    <div class="temp-value ${getTempClass(gpu.temp_memory)}">
                        ${gpu.temp_memory !== null && gpu.temp_memory !== undefined ? gpu.temp_memory : '--'}
                        <span class="temp-unit">°C</span>
                    </div>
                </div>
            </div>

            <div class="info-row">
                <div class="info-item">
                    <span class="info-label">Fan:</span>
                    <span class="info-value">${
                        gpu.fan_percent !== null && gpu.fan_percent !== undefined 
                            ? (gpu.fan_target !== null && gpu.fan_target !== undefined && gpu.fan_target >= 0
                                ? gpu.fan_percent + '%, target: ' + gpu.fan_target + '%' 
                                : gpu.fan_percent + '%') 
                            : '--'
                    }</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Power:</span>
                    <span class="info-value">${gpu.power_watts !== null && gpu.power_watts !== undefined ? gpu.power_watts.toFixed(1) + 'W' : '--'}</span>
                </div>
            </div>

            <div class="profile-toggle">
                <button class="profile-toggle-btn" onclick="toggleProfile('${gpu.uuid}')">
                    <span class="profile-toggle-icon ${iconClass}" id="toggle-icon-${gpu.uuid}">▶</span>
                    Fan Profile
                    <span class="profile-coords" id="coords-${gpu.uuid}"></span>
                </button>
            </div>
            <div class="profile-section ${isExpanded}" id="profile-section-${gpu.uuid}">
                <div class="profile-canvas-container">
                    <canvas id="canvas-${gpu.uuid}" class="profile-canvas" width="400" height="150"></canvas>
                </div>
                <div class="profile-controls">
                    <span class="profile-status" id="status-${gpu.uuid}"></span>
                    <button class="profile-btn clear-btn" onclick="confirmSetAuto('${gpu.uuid}')">Auto</button>
                    <button class="profile-btn" onclick="confirmApplyProfile('${gpu.uuid}')">Apply</button>
                </div>
            </div>

            <div class="last-seen">
                Updated: ${formatTime(gpu.last_seen)}
                ${!gpu.is_online ? ' <span style="color:#ff4444">(OFFLINE)</span>' : ''}
            </div>
        </div>
    `;
}

// ============================================================================
// Coordinate Display Helper
// ============================================================================

function showCoords(id, temp, pwm) {
    const coordsEl = document.getElementById('coords-' + id);
    if (!coordsEl) return;
    
    coordsEl.textContent = `${temp}°C - ${pwm}%`;
    coordsEl.classList.add('visible');
    
    // Clear previous timeout
    if (coordsTimeout) {
        clearTimeout(coordsTimeout);
    }
    
    // Hide after 3 seconds
    coordsTimeout = setTimeout(() => {
        coordsEl.classList.remove('visible');
    }, 3000);
}

// ============================================================================
// Profile Toggle
// ============================================================================

window.toggleProfile = function(id) {
    const section = document.getElementById('profile-section-' + id);
    const icon = document.getElementById('toggle-icon-' + id);
    
    if (!section) return;
    
    const isExpanded = section.classList.contains('expanded');
    
    if (isExpanded) {
        section.classList.remove('expanded');
        if (icon) icon.classList.remove('expanded');
        expandedProfiles[id] = false;
    } else {
        section.classList.add('expanded');
        if (icon) icon.classList.add('expanded');
        expandedProfiles[id] = true;
    }
}

// ============================================================================
// Confirm Dialog
// ============================================================================

let confirmCallback = null;

function showConfirm(title, message, isDanger = false) {
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    
    const okBtn = document.getElementById('confirmOk');
    okBtn.className = 'confirm-btn ' + (isDanger ? 'danger' : 'yes');
    
    document.getElementById('confirmOverlay').classList.add('visible');
}

function hideConfirm() {
    document.getElementById('confirmOverlay').classList.remove('visible');
    confirmCallback = null;
}

document.getElementById('confirmCancel').addEventListener('click', hideConfirm);
document.getElementById('confirmOk').addEventListener('click', () => {
    if (confirmCallback) {
        confirmCallback();
    }
    hideConfirm();
});

// ============================================================================
// Profile Editor Functions - 3 fixed points, drag only, vertical extensions
// ============================================================================

function drawProfile(uuid) {
    const canvas = document.getElementById('canvas-' + uuid);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const editor = editors[uuid];
    if (!editor) return;

    // Use fixed canvas size, scale for display
    const w = 400;
    const h = 150;
    const padding = 30;

    ctx.clearRect(0, 0, w, h);

    // Grid
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
        const y = padding + (h - 2 * padding) * (1 - i / 5);
        ctx.beginPath();
        ctx.moveTo(padding, y);
        ctx.lineTo(w - padding, y);
        ctx.stroke();
    }
    for (let i = 0; i <= 5; i++) {
        const x = padding + (w - 2 * padding) * (i / 5);
        ctx.beginPath();
        ctx.moveTo(x, padding);
        ctx.lineTo(x, h - padding);
        ctx.stroke();
    }

    // Labels - Y axis (PWM %)
    ctx.fillStyle = '#666';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 5; i++) {
        const y = padding + (h - 2 * padding) * (1 - i / 5);
        const label = (i * 20) + '%';
        ctx.fillText(label, padding - 5, y + 3);
    }
    
    // Labels - X axis (Temperature)
    ctx.textAlign = 'center';
    for (let i = 0; i <= 5; i++) {
        const x = padding + (w - 2 * padding) * (i / 5);
        const temp = 30 + i * 14;
        ctx.fillText(temp + '°C', x, h - 8);
    }

    const pts = editor.points.slice().sort((a, b) => a[0] - b[0]);

    // Color: gray if auto, blue otherwise
    const lineColor = editor.isAuto ? '#666' : '#00d9ff';
    const pointColor = editor.isAuto ? '#888' : '#00d9ff';

    // Draw curve with VERTICAL extensions
    if (pts.length === 3) {
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 2;
        ctx.beginPath();

        const p0x = padding + (pts[0][0] - 30) / 70 * (w - 2 * padding);
        const p0y = h - padding - pts[0][1] / 100 * (h - 2 * padding);
        const p1x = padding + (pts[1][0] - 30) / 70 * (w - 2 * padding);
        const p1y = h - padding - pts[1][1] / 100 * (h - 2 * padding);
        const p2x = padding + (pts[2][0] - 30) / 70 * (w - 2 * padding);
        const p2y = h - padding - pts[2][1] / 100 * (h - 2 * padding);

        ctx.moveTo(p0x, h - padding);
        ctx.lineTo(p0x, p0y);
        ctx.lineTo(p1x, p1y);
        ctx.lineTo(p2x, p2y);
        ctx.lineTo(p2x, padding);

        ctx.stroke();

        // Draw points
        pts.forEach((pt, i) => {
            const x = padding + (pt[0] - 30) / 70 * (w - 2 * padding);
            const y = h - padding - pt[1] / 100 * (h - 2 * padding);

            ctx.fillStyle = i === editor.dragging ? '#ff4444' : pointColor;
            ctx.beginPath();
            ctx.arc(x, y, 6, 0, 2 * Math.PI);
            ctx.fill();
        });
    }
}

function setupCanvasEvents(id, isGroup) {
    const canvas = document.getElementById('canvas-' + id);
    if (!canvas) return;
    
    const editor = isGroup 
        ? (id === 'cpu' ? cpuProfileEditor : gpuGroupProfileEditor)
        : editors[id];
    if (!editor) return;

    const padding = 30;
    const canvasW = 400;
    const canvasH = 150;

    const getCoordsFromEvent = (e) => {
        const rect = canvas.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        const x = clientX - rect.left;
        const y = clientY - rect.top;
        
        // Scale from screen coords to canvas coords
        const scaleX = canvasW / rect.width;
        const scaleY = canvasH / rect.height;
        
        const canvasX = x * scaleX;
        const canvasY = y * scaleY;
        
        const temp = Math.round(30 + (canvasX - padding) / (canvasW - 2 * padding) * 70);
        const pwm = Math.round(100 - (canvasY - padding) / (canvasH - 2 * padding) * 100);
        return [Math.max(30, Math.min(100, temp)), Math.max(0, Math.min(100, pwm))];
    };

    const findPointFromEvent = (e) => {
        const rect = canvas.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        const x = clientX - rect.left;
        const y = clientY - rect.top;
        
        // Scale from screen coords to canvas coords
        const scaleX = canvasW / rect.width;
        const scaleY = canvasH / rect.height;

        for (let i = 0; i < editor.points.length; i++) {
            const px = padding + (editor.points[i][0] - 30) / 70 * (canvasW - 2 * padding);
            const py = canvasH - padding - editor.points[i][1] / 100 * (canvasH - 2 * padding);
            
            // Convert to screen coords for hit test
            const screenPx = px / scaleX;
            const screenPy = py / scaleY;
            
            if (Math.hypot(x - screenPx, y - screenPy) < 25) return i;
        }
        return -1;
    };

    const onDragStart = (e) => {
        e.preventDefault();
        const idx = findPointFromEvent(e);
        if (idx >= 0) {
            editor.dragging = idx;
            editor.isAuto = false;
            editingProfile = id;  // Mark as editing
            
            // Show coords
            const [temp, pwm] = editor.points[idx];
            showCoords(id, temp, pwm);
            
            if (isGroup) {
                drawGroupProfile(id);
            } else {
                drawProfile(id);
            }
        }
    };

    const onDragMove = (e) => {
        if (editor.dragging >= 0) {
            e.preventDefault();
            const [temp, pwm] = getCoordsFromEvent(e);
            editor.points[editor.dragging] = [temp, pwm];
            
            // Update coords display
            showCoords(id, temp, pwm);
            
            if (isGroup) {
                drawGroupProfile(id);
            } else {
                drawProfile(id);
            }
        }
    };

    const onDragEnd = () => {
        editor.dragging = -1;
        editingProfile = null;  // Clear editing state
    };

    // Mouse events
    canvas.addEventListener('mousedown', onDragStart);
    canvas.addEventListener('mousemove', onDragMove);
    canvas.addEventListener('mouseup', onDragEnd);
    canvas.addEventListener('mouseleave', onDragEnd);

    // Touch events
    canvas.addEventListener('touchstart', onDragStart, { passive: false });
    canvas.addEventListener('touchmove', onDragMove, { passive: false });
    canvas.addEventListener('touchend', onDragEnd);
    canvas.addEventListener('touchcancel', onDragEnd);
}

// ============================================================================
// Apply / Auto with Confirmation
// ============================================================================

window.confirmApplyProfile = function(uuid) {
    confirmCallback = () => applyProfile(uuid);
    showConfirm('Apply Profile', 'Apply this fan profile to the GPU?', false);
}

window.confirmSetAuto = function(uuid) {
    confirmCallback = () => setAutoProfile(uuid);
    showConfirm('Set Auto Mode', 'Reset to automatic fan control? The profile will be cleared.', true);
}

window.confirmApplyGroupProfile = function(type) {
    if (type !== 'cpu') return;  // Only CPU profile is supported now
    confirmCallback = () => applyCpuProfile();
    showConfirm('Apply Profile', 'Apply this fan profile to CPU?', false);
}

function applyProfile(uuid) {
    if (!ws || !editors[uuid]) return;

    const editor = editors[uuid];
    const profile = editor.points.slice().sort((a, b) => a[0] - b[0]);

    editor.isAuto = false;

    ws.send(JSON.stringify({
        type: 'update_profile',
        uuid: uuid,
        profile: profile
    }));

    const status = document.getElementById('status-' + uuid);
    if (status) {
        status.textContent = 'Saved!';
        status.classList.add('saved');
        setTimeout(() => {
            status.textContent = '';
            status.classList.remove('saved');
        }, 2000);
    }

    drawProfile(uuid);
}

function setAutoProfile(uuid) {
    if (!ws || !editors[uuid]) return;

    editors[uuid].isAuto = true;

    ws.send(JSON.stringify({
        type: 'update_profile',
        uuid: uuid,
        profile: []
    }));

    drawProfile(uuid);
}

// ============================================================================
// Name Editing
// ============================================================================

window.editName = function(uuid) {
    const nameEl = document.getElementById('name-' + uuid);
    if (!nameEl) return;

    const gpu = state?.gpus?.find(g => g.uuid === uuid);
    const currentName = gpu?.name || '';
    const parentDiv = nameEl.parentElement;

    editingUuid = uuid;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'name-input';
    input.value = currentName;
    input.placeholder = 'Enter name...';
    input.id = 'input-' + uuid;

    input.onblur = () => {
        saveName(uuid, input.value);
        editingUuid = null;
    };

    input.onkeydown = (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            saveName(uuid, input.value);
            editingUuid = null;
            restoreNameDisplay(uuid, input.value);
        }
        if (e.key === 'Escape') {
            editingUuid = null;
            restoreNameDisplay(uuid, currentName);
        }
    };

    parentDiv.innerHTML = '';
    parentDiv.appendChild(input);
    input.focus();
    input.select();
}

function restoreNameDisplay(uuid, name) {
    const gpu = state?.gpus?.find(g => g.uuid === uuid);
    const nameEl = document.getElementById('name-' + uuid);
    if (!nameEl) return;

    const parentDiv = nameEl.parentElement;
    parentDiv.innerHTML = `
        <span class="gpu-display-name ${name ? '' : 'empty'}" id="name-${uuid}">
            ${name || 'Unknown GPU'}
        </span>
        ${getGpuStatusBadge(gpu)}
    `;
}

function saveName(uuid, name) {
    if (!ws) return;

    ws.send(JSON.stringify({
        type: 'update_name',
        uuid: uuid,
        name: name
    }));

    if (state) {
        const gpu = state.gpus?.find(g => g.uuid === uuid);
        if (gpu) {
            gpu.name = name;
        }
    }

    restoreNameDisplay(uuid, name);
}

// ============================================================================
// Group Profile Functions
// ============================================================================

function drawGroupProfile(type) {
    const canvas = document.getElementById('canvas-' + type);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const editor = type === 'cpu' ? cpuProfileEditor : gpuGroupProfileEditor;
    if (!editor) return;

    // Use fixed canvas size, scale for display
    const w = 400;
    const h = 150;
    const padding = 30;
    const color = type === 'cpu' ? '#ff9800' : '#4a9eff';

    ctx.clearRect(0, 0, w, h);

    // Grid
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
        const y = padding + (h - 2 * padding) * (1 - i / 5);
        ctx.beginPath();
        ctx.moveTo(padding, y);
        ctx.lineTo(w - padding, y);
        ctx.stroke();
    }
    for (let i = 0; i <= 5; i++) {
        const x = padding + (w - 2 * padding) * (i / 5);
        ctx.beginPath();
        ctx.moveTo(x, padding);
        ctx.lineTo(x, h - padding);
        ctx.stroke();
    }

    // Labels - Y axis (PWM %)
    ctx.fillStyle = '#666';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 5; i++) {
        const y = padding + (h - 2 * padding) * (1 - i / 5);
        const label = (i * 20) + '%';
        ctx.fillText(label, padding - 5, y + 3);
    }
    
    // Labels - X axis (Temperature)
    ctx.textAlign = 'center';
    for (let i = 0; i <= 5; i++) {
        const x = padding + (w - 2 * padding) * (i / 5);
        const temp = 30 + i * 14;
        ctx.fillText(temp + '°C', x, h - 8);
    }

    const pts = editor.points.slice().sort((a, b) => a[0] - b[0]);
    const lineColor = editor.isAuto ? '#666' : color;

    if (pts.length === 3) {
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 2;
        ctx.beginPath();

        const p0x = padding + (pts[0][0] - 30) / 70 * (w - 2 * padding);
        const p0y = h - padding - pts[0][1] / 100 * (h - 2 * padding);
        const p1x = padding + (pts[1][0] - 30) / 70 * (w - 2 * padding);
        const p1y = h - padding - pts[1][1] / 100 * (h - 2 * padding);
        const p2x = padding + (pts[2][0] - 30) / 70 * (w - 2 * padding);
        const p2y = h - padding - pts[2][1] / 100 * (h - 2 * padding);

        ctx.moveTo(p0x, h - padding);
        ctx.lineTo(p0x, p0y);
        ctx.lineTo(p1x, p1y);
        ctx.lineTo(p2x, p2y);
        ctx.lineTo(p2x, padding);

        ctx.stroke();

        // Draw points
        pts.forEach((pt, i) => {
            const x = padding + (pt[0] - 30) / 70 * (w - 2 * padding);
            const y = h - padding - pt[1] / 100 * (h - 2 * padding);

            ctx.fillStyle = i === editor.dragging ? '#ff4444' : lineColor;
            ctx.beginPath();
            ctx.arc(x, y, 6, 0, 2 * Math.PI);
            ctx.fill();
        });
    }
}

function applyCpuProfile() {
    if (!ws || !cpuProfileEditor) return;

    cpuProfileEditor.isAuto = false;
    const profile = cpuProfileEditor.points.slice().sort((a, b) => a[0] - b[0]);

    ws.send(JSON.stringify({
        type: 'update_cpu_profile',
        profile: profile
    }));

    const status = document.getElementById('cpuProfileStatus');
    if (status) {
        status.textContent = 'Saved!';
        status.classList.add('saved');
        setTimeout(() => {
            status.textContent = '';
            status.classList.remove('saved');
        }, 2000);
    }

    drawGroupProfile('cpu');
}

// Start connection
connect();
