/**
 * UI rendering functions for the trading dashboard.
 * Handles all DOM manipulation and Chart.js rendering.
 */

export function renderAuditTable(auditData, auditTableBody, currentPage = 1, itemsPerPage = 10) {
    if (!auditData || auditData.length === 0) {
        auditTableBody.innerHTML = '<tr><td colspan="6" class="loading">No hay logs en la base de datos.</td></tr>';
        return;
    }

    auditTableBody.innerHTML = '';
    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    const pageData = auditData.slice(startIndex, endIndex);

    pageData.forEach((row, index) => {
        const tr = document.createElement('tr');
        tr.className = 'row-anim';
        tr.style.animationDelay = `${index * 0.03}s`;

        const dateObj = new Date(row.created_at);
        const dateStr = dateObj.toLocaleString('es-ES', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });

        const badgeClass = row.event ? `badge ${row.event}` : 'badge default';

        let symbol = "-";
        if (row.data && row.data.symbol) symbol = row.data.symbol;

        const jsonFormatted = JSON.stringify(row.data, null, 2);

        tr.innerHTML = `
            <td>#${row.id}</td>
            <td>${dateStr}</td>
            <td><span class="${badgeClass}">${row.event || 'desconocido'}</span></td>
            <td><strong>${symbol}</strong></td>
            <td style="color: var(--text-muted); font-size: 0.8rem;">${row.cycle_id}</td>
            <td><div class="json-view">${jsonFormatted}</div></td>
        `;
        auditTableBody.appendChild(tr);
    });
}

export function updatePaginationControls(auditData, currentPage, prevBtn, nextBtn, pageInfo, itemsPerPage = 10) {
    if (!auditData || auditData.length === 0) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        pageInfo.textContent = `Página 1 de 1`;
        return;
    }
    const totalPages = Math.ceil(auditData.length / itemsPerPage);
    pageInfo.textContent = `Página ${currentPage} de ${totalPages}`;
    prevBtn.disabled = currentPage === 1;
    nextBtn.disabled = currentPage === totalPages;
}

export function renderNewsTable(newsData, newsTableBody) {
    if (!newsData || newsData.length === 0) {
        newsTableBody.innerHTML = '<tr><td colspan="4" class="loading">No hay noticias de alto impacto en caché.</td></tr>';
        return;
    }

    newsTableBody.innerHTML = '';
    newsData.sort((a, b) => new Date(a.time_utc) - new Date(b.time_utc));

    const now = new Date();

    newsData.forEach((row, index) => {
        const tr = document.createElement('tr');
        tr.className = 'row-anim';
        tr.style.animationDelay = `${index * 0.03}s`;

        const startDateObj = new Date(row.start_time_utc);
        const endDateObj = new Date(row.end_time_utc);

        const startStr = startDateObj.toLocaleString('es-ES', {
            weekday: 'short', day: '2-digit', month: '2-digit',
            hour: '2-digit', minute: '2-digit'
        });
        const endStr = endDateObj.toLocaleString('es-ES', {
            hour: '2-digit', minute: '2-digit'
        });

        const evTime = startDateObj.getTime();
        const blackoutEnd = endDateObj.getTime();

        let isBlackout = false;
        let statusText = "";

        if (now.getTime() >= evTime && now.getTime() <= blackoutEnd) {
            isBlackout = true;
            statusText = "¡BLACKOUT ACTIVO!";
            tr.classList.add('blackout-active');
        } else if (blackoutEnd < now.getTime()) {
            statusText = "Completado";
            tr.classList.add('past-event');
        } else {
            statusText = "Pendiente";
        }

        tr.innerHTML = `
            <td>${startStr}</td>
            <td><strong>${endStr}</strong></td>
            <td><span class="badge default" style="background: rgba(255,255,255,0.15);">${row.currency}</span></td>
            <td><strong>${row.title}</strong></td>
            <td>${isBlackout ? `<span class="badge simple_mt5_error" style="animation: pulse 1s infinite">${statusText}</span>` : `<span style="color:var(--text-muted); font-size:0.85rem">${statusText}</span>`}</td>
        `;
        newsTableBody.appendChild(tr);
    });
}

/**
 * Calcula métricas derivadas de evolution para KPIs y gráficos.
 */
function _computeDerivedMetrics(evolution) {
    if (!evolution || evolution.length === 0) {
        return {
            grossProfit: 0,
            grossLoss: 0,
            maxDrawdown: 0,
            runningMax: [],
            drawdown: [],
            pnlByHour: {},
        };
    }

    let grossProfit = 0;
    let grossLoss = 0;
    let runningMax = -Infinity;
    let maxDrawdown = 0;
    const runningMaxArr = [];
    const drawdownArr = [];
    const pnlByHour = {};

    let prevCumulative = 0;
    evolution.forEach((point, i) => {
        const pnl = i === 0 ? point.cumulative : point.cumulative - prevCumulative;
        prevCumulative = point.cumulative;

        if (pnl > 0) grossProfit += pnl;
        else if (pnl < 0) grossLoss += Math.abs(pnl);

        // Hora del día (UTC)
        const hour = new Date(point.time).getHours();
        pnlByHour[hour] = (pnlByHour[hour] || 0) + pnl;

        // Running max y drawdown
        if (point.cumulative > runningMax) runningMax = point.cumulative;
        runningMaxArr.push(runningMax);
        const dd = point.cumulative - runningMax;
        drawdownArr.push(dd);
        if (dd < maxDrawdown) maxDrawdown = dd;
    });

    return {
        grossProfit,
        grossLoss,
        maxDrawdown,
        runningMax: runningMaxArr,
        drawdown: drawdownArr,
        pnlByHour,
    };
}

export function renderGrafanaCharts(statsData, chartInstances) {
    if (!statsData || statsData.error) return;

    // Destruir instancias previas
    for (const key of Object.keys(chartInstances)) {
        if (chartInstances[key]) chartInstances[key].destroy();
    }

    Chart.defaults.color = '#a0a0b0';
    Chart.defaults.font.family = 'Inter';

    const evolution = statsData.evolution || [];
    const derived = _computeDerivedMetrics(evolution);
    const totalTrades = evolution.length;
    const wins = statsData.wins || 0;
    const losses = statsData.losses || 0;
    const winRate = totalTrades > 0 ? (wins / totalTrades) * 100 : 0;
    const profitFactor = derived.grossLoss > 0 ? derived.grossProfit / derived.grossLoss : (derived.grossProfit > 0 ? 0 : 0);

    // ── KPI Cards ──
    const k = statsData.kpis || {};
    document.getElementById('kpiWinRate').textContent = winRate.toFixed(1) + '%';
    document.getElementById('kpiProfitFactor').textContent = profitFactor.toFixed(2);
    document.getElementById('kpiMaxDrawdown').textContent = derived.maxDrawdown.toFixed(2) + ' %';
    document.getElementById('kpiTotalTrades').textContent = totalTrades;

    // Colores de la paleta
    const COLOR_EQUITY = '#00f2fe';
    const COLOR_DRAWDOWN = '#ef4444';
    const COLOR_POSITIVE = '#10b981';
    const COLOR_NEGATIVE = '#ef4444';
    const COLOR_SYMBOL = '#3b82f6';
    const COLOR_HOUR = '#a855f7';

    // ── 1. Equity curve con drawdown (reemplaza balance evolution) ──
    const ctxEq = document.getElementById('balanceChart').getContext('2d');
    const labelsEq = evolution.map(x => x.time);
    const dataEq = evolution.map(x => x.cumulative);
    chartInstances.balanceChart = new Chart(ctxEq, {
        type: 'line',
        data: {
            labels: labelsEq,
            datasets: [
                {
                    label: 'Equity (€)',
                    data: dataEq,
                    borderColor: COLOR_EQUITY,
                    backgroundColor: 'rgba(0, 242, 254, 0.05)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                    borderWidth: 2,
                },
                {
                    label: 'Drawdown (€)',
                    data: derived.drawdown,
                    borderColor: COLOR_DRAWDOWN,
                    backgroundColor: 'rgba(239, 68, 68, 0.15)',
                    fill: '-1', // rellena hasta el dataset anterior
                    tension: 0.3,
                    pointRadius: 0,
                    borderWidth: 1,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: 'Equity Curve con Drawdown', color: '#fff', font: { size: 13 } },
                legend: { display: true, position: 'top', labels: { color: '#a0a0b0', font: { size: 11 } } }
            },
            scales: {
                y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#a0a0b0' } },
                x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#a0a0b0' } }
            }
        }
    });

    // ── 2. Daily PnL (simplificado) ──
    const ctxDaily = document.getElementById('dailyChart').getContext('2d');
    const labelsDaily = Object.keys(statsData.daily_pnl || {});
    const dataDaily = Object.values(statsData.daily_pnl || {});
    chartInstances.dailyChart = new Chart(ctxDaily, {
        type: 'bar',
        data: {
            labels: labelsDaily,
            datasets: [{
                label: 'PnL Diario (€)',
                data: dataDaily,
                backgroundColor: dataDaily.map(v => v >= 0 ? COLOR_POSITIVE : COLOR_NEGATIVE),
                borderWidth: 0,
                barPercentage: 0.8,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: 'PnL Diario', color: '#fff', font: { size: 13 } },
                legend: { display: false }
            },
            scales: {
                y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#a0a0b0' } },
                x: { grid: { display: false }, ticks: { color: '#a0a0b0', maxRotation: 45, minRotation: 45 } }
            }
        }
    });

    // ── 3. Win/Loss doughnut ──
    const ctxWL = document.getElementById('winLossChart').getContext('2d');
    chartInstances.winLossChart = new Chart(ctxWL, {
        type: 'doughnut',
        data: {
            labels: ['Ganadas', 'Perdidas'],
            datasets: [{
                data: [wins, losses],
                backgroundColor: [COLOR_POSITIVE, COLOR_NEGATIVE],
                borderWidth: 0,
                hoverOffset: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '70%',
            plugins: {
                title: { display: true, text: 'Win / Loss', color: '#fff', font: { size: 13 } },
                legend: { display: true, position: 'bottom', labels: { color: '#a0a0b0', font: { size: 11 } } }
            }
        }
    });

    // ── 4. PnL por símbolo ──
    const ctxSym = document.getElementById('symbolChart').getContext('2d');
    const labelsSym = Object.keys(statsData.by_symbol || {});
    const dataSym = Object.values(statsData.by_symbol || {});
    chartInstances.symbolChart = new Chart(ctxSym, {
        type: 'bar',
        data: {
            labels: labelsSym,
            datasets: [{
                label: 'PnL por Par (€)',
                data: dataSym,
                backgroundColor: dataSym.map(v => v >= 0 ? COLOR_SYMBOL : COLOR_NEGATIVE),
                borderWidth: 0,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: 'PnL por Par', color: '#fff', font: { size: 13 } },
                legend: { display: false }
            },
            scales: {
                x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#a0a0b0' } },
                y: { grid: { display: false }, ticks: { color: '#a0a0b0' } }
            }
        }
    });

    // ── 5. PnL por hora del día ──
    const ctxHour = document.getElementById('hourChart').getContext('2d');
    const hours = Array.from({ length: 24 }, (_, i) => i);
    const hourLabels = hours.map(h => {
        const d = new Date();
        d.setHours(h, 0, 0, 0);
        return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', hour12: false });
    });
    const hourData = hours.map(h => derived.pnlByHour[h] || 0);
    chartInstances.hourChart = new Chart(ctxHour, {
        type: 'bar',
        data: {
            labels: hourLabels,
            datasets: [{
                label: 'PnL por Hora (€)',
                data: hourData,
                backgroundColor: hourData.map(v => v >= 0 ? COLOR_HOUR : COLOR_NEGATIVE),
                borderWidth: 0,
                barPercentage: 0.9,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: { display: true, text: 'PnL por Hora (UTC)', color: '#fff', font: { size: 13 } },
                legend: { display: false }
            },
            scales: {
                y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#a0a0b0' } },
                x: { grid: { display: false }, ticks: { color: '#a0a0b0', maxRotation: 90, minRotation: 90, font: { size: 9 } } }
            }
        }
    });
}
