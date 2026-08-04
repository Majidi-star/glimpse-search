/**
 * Glimpse Settings Window - Vanilla JS
 * Locations and File Types tabs fully functional for v0.1
 */

(function() {
    'use strict';

    const API_BASE = '/api';

    // ---- DOM ----
    const tabs = document.querySelectorAll('.tab');
    const panels = document.querySelectorAll('.tab-panel');

    // Locations
    const locationList = document.getElementById('locationList');
    const addLocationBtn = document.getElementById('addLocationBtn');
    const locationsEmpty = document.getElementById('locationsEmpty');

    // File Types
    const filetypeGrid = document.getElementById('filetypeGrid');

    // Performance
    const profileSelect = document.getElementById('profileSelect');
    const maxEffortToggle = document.getElementById('maxEffortToggle');

    // ---- State ----
    let locations = [];
    let fileTypes = [];

    // ---- Helpers ----
    function escapeHtml(str) {
        return str.replace(/[&<>"']/g, c => ({'&':'&','<':'<','>':'>','"':'"',"'":'''}[c]));
    }

    function showToast(message, isError = false) {
        // Simple toast - could be enhanced
        console.log((isError ? 'ERROR: ' : '') + message);
    }

    // ---- Tabs ----
    function switchTab(tabName) {
        tabs.forEach(t => {
            const active = t.dataset.tab === tabName;
            t.classList.toggle('active', active);
            t.setAttribute('aria-selected', active);
        });
        panels.forEach(p => {
            const active = p.id === 'panel-' + tabName;
            p.classList.toggle('active', active);
            p.hidden = !active;
        });
    }

    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            if (!tab.disabled) switchTab(tab.dataset.tab);
        });
    });

    // ---- Locations ----
    async function loadLocations() {
        try {
            const res = await fetch(`${API_BASE}/locations`);
            locations = await res.json();
            renderLocations();
        } catch (e) {
            console.error('Failed to load locations:', e);
        }
    }

    function renderLocations() {
        if (locations.length === 0) {
            locationList.innerHTML = '';
            locationsEmpty.classList.remove('hidden');
            return;
        }
        locationsEmpty.classList.add('hidden');

        locationList.innerHTML = locations.map(loc => `
            <li class="location-item" data-id="${loc.id}">
                <span class="location-path" title="${escapeHtml(loc.path)}">${escapeHtml(loc.path)}</span>
                <div class="location-status">
                    <label class="toggle">
                        <input type="checkbox" ${loc.enabled ? 'checked' : ''} data-action="toggle" data-id="${loc.id}">
                        <span class="slider"></span>
                    </label>
                    <button class="btn btn-danger btn-sm" data-action="remove" data-id="${loc.id}" title="Remove">Remove</button>
                </div>
            </li>
        `).join('');

        // Event delegation
        locationList.querySelectorAll('[data-action="toggle"]').forEach(input => {
            input.addEventListener('change', async (e) => {
                const id = parseInt(e.target.dataset.id, 10);
                const enabled = e.target.checked;
                await toggleLocation(id, enabled);
            });
        });
        locationList.querySelectorAll('[data-action="remove"]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const id = parseInt(e.target.dataset.id, 10);
                if (confirm('Remove this location and all its indexed data?')) {
                    await removeLocation(id);
                }
            });
        });
    }

    async function toggleLocation(id, enabled) {
        try {
            const res = await fetch(`${API_BASE}/locations/${id}`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled})
            });
            if (res.ok) {
                await loadLocations();
            } else {
                showToast('Failed to update location', true);
            }
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    }

    async function removeLocation(id) {
        try {
            const res = await fetch(`${API_BASE}/locations/${id}`, {method: 'DELETE'});
            if (res.ok) {
                await loadLocations();
            } else {
                showToast('Failed to remove location', true);
            }
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    }

    addLocationBtn.addEventListener('click', async () => {
        // Use a simple prompt for v0.1; could use a native folder picker later
        const path = prompt('Enter folder path to index:');
        if (!path) return;

        try {
            const res = await fetch(`${API_BASE}/locations`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path, enabled: true})
            });
            if (res.ok) {
                await loadLocations();
            } else {
                const err = await res.json();
                showToast(err.detail || 'Failed to add location', true);
            }
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    });

    // ---- File Types ----
    async function loadFileTypes() {
        try {
            const res = await fetch(`${API_BASE}/filetypes`);
            fileTypes = await res.json();
            renderFileTypes();
        } catch (e) {
            console.error('Failed to load file types:', e);
        }
    }

    function renderFileTypes() {
        filetypeGrid.innerHTML = fileTypes.map(ft => `
            <div class="filetype-card ${ft.enabled ? '' : 'disabled'} ${ft.supported ? '' : 'not-supported'}" data-category="${ft.category}">
                <div class="filetype-header">
                    <span class="filetype-name">${ft.category}</span>
                    <span class="filetype-badge ${ft.supported ? 'supported' : 'unsupported'}">
                        ${ft.supported ? 'v0.1' : 'Not in v0.1'}
                    </span>
                </div>
                <label class="filetype-toggle">
                    <span>${ft.supported ? 'Enabled for indexing' : 'Coming soon'}</span>
                    <div class="toggle">
                        <input type="checkbox" ${ft.enabled ? 'checked' : ''} ${!ft.supported ? 'disabled' : ''} data-category="${ft.category}">
                        <span class="slider"></span>
                    </div>
                </label>
            </div>
        `).join('');

        filetypeGrid.querySelectorAll('input[data-category]').forEach(input => {
            input.addEventListener('change', async (e) => {
                const cat = e.target.dataset.category;
                const enabled = e.target.checked;
                await toggleFileType(cat, enabled);
            });
        });
    }

    async function toggleFileType(category, enabled) {
        try {
            const res = await fetch(`${API_BASE}/filetypes/${category}`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled})
            });
            if (res.ok) {
                await loadFileTypes();
            } else {
                showToast('Failed to update file type', true);
            }
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    }

    // ---- Performance ----
    async function loadPerf() {
        try {
            const res = await fetch(`${API_BASE}/perf`);
            const data = await res.json();
            profileSelect.value = data.profile;
            maxEffortToggle.checked = data.max_effort;
        } catch (e) {
            console.error('Failed to load perf:', e);
        }
    }

    profileSelect.addEventListener('change', async () => {
        try {
            await fetch(`${API_BASE}/perf`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({profile: profileSelect.value})
            });
            await loadPerf();
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    });

    maxEffortToggle.addEventListener('change', async () => {
        try {
            await fetch(`${API_BASE}/perf/max_effort`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: maxEffortToggle.checked})
            });
            await loadPerf();
        } catch (e) {
            showToast('Error: ' + e.message, true);
        }
    });

    // ---- Init ----
    async function init() {
        // Load all tabs' data
        await Promise.all([loadLocations(), loadFileTypes(), loadPerf()]);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();