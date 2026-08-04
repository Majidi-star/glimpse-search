/**
 * Glimpse Search Popup - Vanilla JS
 * Debounced search, keyboard nav, open file
 */

(function() {
    'use strict';

    // ---- Config ----
    const API_BASE = '/api';
    const DEBOUNCE_MS = 120;
    const MAX_HISTORY = 10;

    // ---- State ----
    let results = [];
    let selectedIndex = -1;
    let debounceTimer = null;
    let lastQuery = '';
    let abortController = null;

    // ---- DOM ----
    const input = document.getElementById('searchInput');
    const resultsList = document.getElementById('resultsList');
    const emptyState = document.getElementById('emptyState');
    const loadingState = document.getElementById('loadingState');
    const statusBadge = document.getElementById('statusBadge');

    // ---- Icons (inline SVG) ----
    const ICONS = {
        text: `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h12v12H2z"/><path d="M4 5h8M4 8h8M4 11h5"/></svg>`,
        code: `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M6 2L3 5v6l3 3 3-3V5L6 2zm4 0v12"/></svg>`,
        pdf: `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h12v12H2z"/><path d="M5 5h6v6H5z"/><text x="8" y="11" font-size="6" text-anchor="middle" fill="white">PDF</text></svg>`,
        default: `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h12v12H2z"/></svg>`,
    };

    // ---- Helpers ----
    function escapeHtml(str) {
        return str.replace(/[&<>"']/g, c => ({'&':'&','<':'<','>':'>','"':'"',"'":'''}[c]));
    }

    function formatTime(ts) {
        const d = new Date(ts * 1000);
        const now = new Date();
        const diff = now - d;
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
        if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
        if (diff < 604800000) return Math.floor(diff/86400000) + 'd ago';
        return d.toLocaleDateString();
    }

    function getIcon(type) {
        return ICONS[type] || ICONS.default;
    }

    // ---- Render ----
    function renderResults(hits, query) {
        results = hits;
        selectedIndex = -1;

        if (hits.length === 0) {
            resultsList.innerHTML = '';
            emptyState.classList.remove('hidden');
            loadingState.classList.add('hidden');
            return;
        }

        emptyState.classList.add('hidden');
        loadingState.classList.add('hidden');

        const html = hits.map((hit, i) => `
            <div class="result-item" data-index="${i}" tabindex="0" role="option" aria-selected="false">
                <span class="result-icon">${getIcon(hit.file_type)}</span>
                <div class="result-content">
                    <div class="result-path">${escapeHtml(hit.path)}</div>
                    <div class="result-snippet">${highlight(escapeHtml(hit.snippet), query)}</div>
                </div>
                <div class="result-meta">
                    <span class="result-score">${(hit.score * 100).toFixed(0)}%</span>
                    <span class="result-time">${formatTime(hit.mtime)}</span>
                </div>
            </div>
        `).join('');

        resultsList.innerHTML = html;
    }

    function highlight(text, query) {
        if (!query.trim()) return text;
        const terms = query.trim().split(/\s+/).filter(t => t.length > 1);
        let result = text;
        for (const term of terms) {
            const regex = new RegExp(`(${escapeRegex(term)})`, 'gi');
            result = result.replace(regex, '<mark>$1</mark>');
        }
        return result;
    }

    function escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function updateSelection() {
        const items = resultsList.querySelectorAll('.result-item');
        items.forEach((el, i) => {
            const selected = i === selectedIndex;
            el.classList.toggle('selected', selected);
            el.setAttribute('aria-selected', selected);
            if (selected) {
                el.scrollIntoView({block: 'nearest'});
            }
        });
    }

    // ---- Search ----
    function doSearch(query) {
        if (abortController) abortController.abort();
        abortController = new AbortController();

        loadingState.classList.remove('hidden');
        emptyState.classList.add('hidden');
        statusBadge.textContent = 'Searching…';
        statusBadge.classList.add('indexing');
        statusBadge.classList.remove('hidden');

        fetch(`${API_BASE}/search?q=${encodeURIComponent(query)}&top_k=50`, { signal: abortController.signal })
            .then(r => r.json())
            .then(data => {
                statusBadge.classList.remove('indexing');
                statusBadge.classList.add('hidden');
                renderResults(data.hits, query);
            })
            .catch(err => {
                if (err.name !== 'AbortError') {
                    console.error('Search error:', err);
                    statusBadge.textContent = 'Error';
                    statusBadge.classList.remove('indexing');
                }
            });
    }

    function debouncedSearch() {
        const query = input.value.trim();
        if (query === lastQuery) return;
        lastQuery = query;

        clearTimeout(debounceTimer);
        if (!query) {
            renderResults([], '');
            return;
        }
        debounceTimer = setTimeout(() => doSearch(query), DEBOUNCE_MS);
    }

    // ---- Open file ----
    function openResult(index) {
        const hit = results[index];
        if (!hit) return;

        fetch(`${API_BASE}/open`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: hit.path})
        }).then(r => r.json()).then(data => {
            if (!data.success) {
                console.warn('Open failed:', data.error);
            }
        });
    }

    // ---- Keyboard ----
    function handleKeydown(e) {
        const items = resultsList.querySelectorAll('.result-item');
        if (!items.length) return;

        switch (e.key) {
            case 'ArrowDown':
                e.preventDefault();
                selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
                updateSelection();
                break;
            case 'ArrowUp':
                e.preventDefault();
                selectedIndex = Math.max(selectedIndex - 1, 0);
                updateSelection();
                break;
            case 'Enter':
                e.preventDefault();
                if (selectedIndex >= 0) openResult(selectedIndex);
                break;
            case 'Escape':
                window.close(); // pywebview handles this
                break;
        }
    }

    // ---- Init ----
    function init() {
        input.addEventListener('input', debouncedSearch);
        input.addEventListener('keydown', handleKeydown);
        document.addEventListener('keydown', handleKeydown);

        // Focus input on load
        input.focus();

        // Check state periodically
        setInterval(async () => {
            try {
                const res = await fetch(`${API_BASE}/state`);
                const data = await res.json();
                if (data.queue_depth > 0) {
                    statusBadge.textContent = `Indexing ${data.queue_depth}…`;
                    statusBadge.classList.add('indexing');
                    statusBadge.classList.remove('hidden');
                } else {
                    statusBadge.classList.add('hidden');
                }
            } catch {}
        }, 3000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();