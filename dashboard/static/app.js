// Dashboard JavaScript Application
let currentModel = '';
let currentFilter = 'all';
let currentSearch = '';
let refreshIntervalTimer = null;
let refreshRate = 5000;
let accuracyChart = null;
let metricsChart = null;
let autoScrollLogs = true;

document.addEventListener('DOMContentLoaded', () => {
    initApp();
});

async function initApp() {
    setupEventListeners();
    await loadModels();
    await refreshAllData();
    setupAutoRefresh();
}

function setupEventListeners() {
    // Model Selector
    const modelSelect = document.getElementById('model-select');
    if (modelSelect) {
        modelSelect.addEventListener('change', (e) => {
            currentModel = e.target.value;
            refreshAllData();
        });
    }

    // Refresh rate
    const refreshRateSelect = document.getElementById('refresh-rate-select');
    if (refreshRateSelect) {
        refreshRateSelect.addEventListener('change', (e) => {
            refreshRate = parseInt(e.target.value, 10) * 1000;
            setupAutoRefresh();
        });
    }

    // Manual refresh button
    const btnManualRefresh = document.getElementById('btn-manual-refresh');
    if (btnManualRefresh) {
        btnManualRefresh.addEventListener('click', () => {
            const icon = document.getElementById('refresh-icon');
            if (icon) icon.classList.add('fa-spin');
            refreshAllData().then(() => {
                if (icon) setTimeout(() => icon.classList.remove('fa-spin'), 500);
            });
        });
    }

    // Status Filter buttons
    const filterBtns = document.querySelectorAll('.filter-btn');
    filterBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            filterBtns.forEach(b => {
                b.classList.remove('bg-blue-600', 'text-white');
                b.classList.add('text-gray-400');
            });
            e.target.classList.add('bg-blue-600', 'text-white');
            e.target.classList.remove('text-gray-400');

            currentFilter = e.target.getAttribute('data-status');
            loadRunsTable();
        });
    });

    // Search input
    let searchDebounce = null;
    const tableSearch = document.getElementById('table-search');
    if (tableSearch) {
        tableSearch.addEventListener('input', (e) => {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(() => {
                currentSearch = e.target.value.trim();
                loadRunsTable();
            }, 300);
        });
    }

    // Modals
    const btnCloseModal = document.getElementById('btn-close-modal');
    if (btnCloseModal) btnCloseModal.addEventListener('click', closeModal);
    
    const queryModal = document.getElementById('query-modal');
    if (queryModal) {
        queryModal.addEventListener('click', (e) => {
            if (e.target.id === 'query-modal') closeModal();
        });
    }

    const btnAccessInfo = document.getElementById('btn-access-info');
    if (btnAccessInfo) {
        btnAccessInfo.addEventListener('click', () => {
            const modal = document.getElementById('access-modal');
            if (modal) modal.classList.remove('hidden');
        });
    }

    const btnCloseAccessModal = document.getElementById('btn-close-access-modal');
    if (btnCloseAccessModal) {
        btnCloseAccessModal.addEventListener('click', () => {
            const modal = document.getElementById('access-modal');
            if (modal) modal.classList.add('hidden');
        });
    }

    const btnDismissAccess = document.getElementById('btn-dismiss-access');
    if (btnDismissAccess) {
        btnDismissAccess.addEventListener('click', () => {
            const modal = document.getElementById('access-modal');
            if (modal) modal.classList.add('hidden');
        });
    }

    // Auto-scroll toggle for logs
    const btnToggleAutoscroll = document.getElementById('btn-toggle-autoscroll');
    if (btnToggleAutoscroll) {
        btnToggleAutoscroll.addEventListener('click', (e) => {
            autoScrollLogs = !autoScrollLogs;
            e.currentTarget.innerHTML = `<i class="fa-solid fa-arrow-down-short-wide"></i> Auto-scroll: ${autoScrollLogs ? 'ON' : 'OFF'}`;
        });
    }

    // Copy logs
    const btnCopyLogs = document.getElementById('btn-copy-logs');
    if (btnCopyLogs) {
        btnCopyLogs.addEventListener('click', () => {
            const logBox = document.getElementById('log-container');
            if (logBox) {
                navigator.clipboard.writeText(logBox.innerText).then(() => {
                    alert('Logs copied to clipboard!');
                });
            }
        });
    }
}

function setupAutoRefresh() {
    if (refreshIntervalTimer) clearInterval(refreshIntervalTimer);
    if (refreshRate > 0) {
        refreshIntervalTimer = setInterval(() => {
            refreshAllData();
        }, refreshRate);
    }
}

async function refreshAllData() {
    await Promise.all([
        loadStatus(),
        loadRunsTable(),
        loadLogs()
    ]);
}

async function loadModels() {
    try {
        const res = await fetch('/api/models');
        const data = await res.json();
        const select = document.getElementById('model-select');
        if (select) {
            select.innerHTML = '';
            data.models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                select.appendChild(opt);
            });
        }

        currentModel = data.default || (data.models && data.models.length > 0 ? data.models[0] : 'qwen3.8-27b-lora_custom');
        if (select) select.value = currentModel;
    } catch (err) {
        console.error('Failed to load models:', err);
    }
}

async function loadStatus() {
    if (!currentModel) return;
    try {
        const res = await fetch(`/api/status?model=${encodeURIComponent(currentModel)}`);
        const data = await res.json();
        updateUIWithStatus(data);
    } catch (err) {
        console.error('Failed to fetch status:', err);
    }
}

function updateUIWithStatus(data) {
    // Process status pill
    const proc = data.process_status || {};
    const pill = document.getElementById('process-status-pill');
    const statusText = document.getElementById('status-text');
    const statusDot = document.getElementById('status-dot');
    const statusPing = document.getElementById('status-ping');

    if (pill && statusText && statusDot && statusPing) {
        if (proc.is_running) {
            pill.className = "flex items-center space-x-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-emerald-950/80 text-emerald-300 border border-emerald-700/60 shadow-lg shadow-emerald-900/20";
            statusText.textContent = `Running (${proc.active_processes.length} Active)`;
            statusDot.className = "relative inline-flex rounded-full h-2 w-2 bg-emerald-400";
            statusPing.classList.remove('hidden');
        } else {
            pill.className = "flex items-center space-x-2 px-3 py-1.5 rounded-full text-xs font-semibold bg-gray-800 text-gray-400 border border-gray-700";
            statusText.textContent = data.runs_completed >= data.total_target_queries ? "Completed" : "Idle";
            statusDot.className = "relative inline-flex rounded-full h-2 w-2 bg-gray-400";
            statusPing.classList.add('hidden');
        }
    }

    // KPI values
    const target = data.total_target_queries || 830;
    document.getElementById('kpi-runs-completed').textContent = `${data.runs_completed} / ${target}`;
    document.getElementById('kpi-runs-pct').textContent = `${data.runs_progress_pct}%`;
    document.getElementById('bar-runs').style.width = `${data.runs_progress_pct}%`;

    document.getElementById('kpi-evals-completed').textContent = `${data.evals_completed} / ${target}`;
    document.getElementById('kpi-evals-pct').textContent = `${data.evals_progress_pct}%`;
    document.getElementById('bar-evals').style.width = `${data.evals_progress_pct}%`;

    document.getElementById('kpi-accuracy').textContent = `${data.accuracy_pct}%`;
    document.getElementById('kpi-breakdown-counts').textContent = `${data.correct_count} C / ${data.incorrect_count} I`;
    document.getElementById('kpi-precision').textContent = `${data.precision_pct}%`;
    document.getElementById('kpi-recall').textContent = `${data.recall_pct}%`;

    document.getElementById('kpi-avg-tools').textContent = data.avg_tool_calls || '0.0';
    document.getElementById('kpi-last-activity').textContent = data.last_activity || 'N/A';
}

async function loadRunsTable() {
    if (!currentModel) return;
    try {
        let url = `/api/runs?model=${encodeURIComponent(currentModel)}`;
        if (currentFilter && currentFilter !== 'all') {
            url += `&status=${encodeURIComponent(currentFilter)}`;
        }
        if (currentSearch) {
            url += `&search=${encodeURIComponent(currentSearch)}`;
        }

        const res = await fetch(url);
        const data = await res.json();
        renderRunsTable(data.results || []);
    } catch (err) {
        console.error('Failed to load runs table:', err);
    }
}

function renderRunsTable(results) {
    const tbody = document.getElementById('runs-table-body');
    const badge = document.getElementById('table-count-badge');
    if (badge) badge.textContent = `${results.length} items`;

    if (!results || results.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="9" class="px-4 py-8 text-center text-gray-500 font-mono">
                    No run files or evaluations found matching current filter.
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = '';
    results.forEach(item => {
        const tr = document.createElement('tr');
        tr.className = "border-b border-gray-800/60 hover:bg-gray-800/40 transition";

        // Status badge
        let statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-gray-800 text-gray-400">Pending</span>';
        if (item.judge_correct === true) {
            statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">Correct</span>';
        } else if (item.judge_correct === false) {
            statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">Incorrect</span>';
        } else if (item.parse_error) {
            statusBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">Parse Error</span>';
        }

        const qPreview = item.question ? (item.question.length > 60 ? item.question.substring(0, 60) + '...' : item.question) : 'Pending question';
        const pAnswer = item.predicted_answer ? (item.predicted_answer.length > 40 ? item.predicted_answer.substring(0, 40) + '...' : item.predicted_answer) : '-';
        const cAnswer = item.correct_answer || '-';
        const recallVal = item.recall !== null && item.recall !== undefined ? `${item.recall}%` : '-';

        tr.innerHTML = `
            <td class="px-4 py-3 font-mono font-bold text-blue-400">#${item.query_id}</td>
            <td class="px-4 py-3 text-gray-300 max-w-xs truncate" title="${escapeHtml(item.question)}">${escapeHtml(qPreview)}</td>
            <td class="px-4 py-3 font-medium text-emerald-300 max-w-xs truncate" title="${escapeHtml(item.predicted_answer)}">${escapeHtml(pAnswer)}</td>
            <td class="px-4 py-3 font-medium text-blue-300 max-w-xs truncate" title="${escapeHtml(item.correct_answer)}">${escapeHtml(cAnswer)}</td>
            <td class="px-4 py-3 text-center">${statusBadge}</td>
            <td class="px-4 py-3 text-center font-mono text-gray-300">${recallVal}</td>
            <td class="px-4 py-3 text-center font-mono text-purple-300">${item.tool_calls || 0}</td>
            <td class="px-4 py-3 text-right text-gray-500 font-mono text-[11px]">${item.timestamp}</td>
            <td class="px-4 py-3 text-center">
                <button onclick="openQueryModal('${item.query_id}')" class="px-2.5 py-1 bg-gray-800 hover:bg-gray-700 text-blue-400 hover:text-white rounded border border-gray-700 font-medium transition text-[11px]">
                    Inspect
                </button>
            </td>
        `;

        tbody.appendChild(tr);
    });
}

async function openQueryModal(queryId) {
    try {
        const res = await fetch(`/api/run/${encodeURIComponent(currentModel)}/${queryId}`);
        if (!res.ok) {
            alert(`Query details not found for #${queryId}`);
            return;
        }
        const data = await res.json();
        
        const runData = data.run_data || {};
        const evalData = data.eval_data || {};
        const judgeRes = evalData.judge_result || {};

        document.getElementById('modal-qid').textContent = `Query #${queryId}`;
        document.getElementById('modal-question').textContent = evalData.question || 'N/A';
        document.getElementById('modal-predicted-answer').textContent = judgeRes.extracted_final_answer || 'N/A';
        document.getElementById('modal-correct-answer').textContent = evalData.correct_answer || 'N/A';
        document.getElementById('modal-judge-reasoning').textContent = judgeRes.reasoning || 'No judge reasoning available.';
        document.getElementById('modal-full-response').textContent = evalData.response || runData.response || JSON.stringify(runData, null, 2);

        const badge = document.getElementById('modal-status-badge');
        if (judgeRes.correct === true) {
            badge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
            badge.textContent = "CORRECT";
        } else if (judgeRes.correct === false) {
            badge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30";
            badge.textContent = "INCORRECT";
        } else {
            badge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-gray-800 text-gray-400";
            badge.textContent = "PENDING EVAL";
        }

        document.getElementById('query-modal').classList.remove('hidden');
    } catch (err) {
        console.error('Error opening query modal:', err);
    }
}

function closeModal() {
    document.getElementById('query-modal').classList.add('hidden');
}

async function loadLogs() {
    const logBox = document.getElementById('log-container');
    if (!logBox) return;
    try {
        const res = await fetch('/api/logs?lines=150');
        const data = await res.json();
        
        const fnEl = document.getElementById('log-filename');
        if (fnEl) fnEl.textContent = data.filename || 'None';
        
        if (data.logs && data.logs.length > 0) {
            logBox.textContent = data.logs.join('\n');
            if (autoScrollLogs) {
                logBox.scrollTop = logBox.scrollHeight;
            }
        } else {
            logBox.textContent = "No log messages recorded yet.";
        }
    } catch (err) {
        console.error('Failed to load logs:', err);
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
