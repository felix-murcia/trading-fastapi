document.addEventListener('DOMContentLoaded', () => {
    // Inputs & buttons
    const apiInput = document.getElementById('apiUrl');
    const tokenInput = document.getElementById('apiToken');
    const refreshBtn = document.getElementById('refreshBtn');

    // Audit table elements
    const auditTableBody = document.getElementById('auditTableBody');
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');
    const pageInfo = document.getElementById('pageInfo');

    // News table elements
    const newsTableBody = document.getElementById('newsTableBody');

    // Tabs
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    // State
    let auditData = [];
    let newsData = [];
    let statsData = null;
    let chartInstances = {};
    let currentPage = 1;
    const itemsPerPage = 10;

    // Load saved credentials
    if (localStorage.getItem('apiUrl')) apiInput.value = localStorage.getItem('apiUrl');
    if (localStorage.getItem('apiToken')) tokenInput.value = localStorage.getItem('apiToken');

    // Tabs logic
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            btn.classList.add('active');
            const target = btn.getAttribute('data-target');
            document.getElementById(target).classList.add('active');
        });
    });

    async function fetchAllData() {
        refreshBtn.textContent = "Cargando...";
        refreshBtn.disabled = true;

        let apiUrl = apiInput.value.trim();
        if (apiUrl.endsWith('/')) apiUrl = apiUrl.slice(0, -1);
        const token = tokenInput.value.trim();

        // Save credentials locally
        if (apiUrl) localStorage.setItem('apiUrl', apiUrl);
        if (token) localStorage.setItem('apiToken', token);

        const headers = {
            "X-Internal-Token": token,
            "Accept": "application/json"
        };

        try {
            const [auditRes, newsRes, statsRes] = await Promise.all([
                fetch(`${apiUrl}/v1/smc/audit?limit=200`, { headers }),
                fetch(`${apiUrl}/v1/smc/news`, { headers }),
                fetch(`${apiUrl}/v1/smc/analytics`, { headers }).catch(e => null)
            ]);

            if (!auditRes.ok) throw new Error(`Audit HTTP: ${auditRes.status}`);
            if (!newsRes.ok) throw new Error(`News HTTP: ${newsRes.status}`);

            auditData = await auditRes.json();
            newsData = await newsRes.json();
            try { if (statsRes && statsRes.ok) statsData = await statsRes.json(); } catch (e) { }

            currentPage = 1;
            renderAuditTable();
            renderNewsTable();
            if (statsData && !statsData.error) renderGrafanaCharts();

        } catch (err) {
            console.error(err);
            auditTableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--error);">Error al cargar logs: ${err.message}</td></tr>`;
            newsTableBody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--error);">Error al cargar noticias: ${err.message}</td></tr>`;
            auditData = [];
            newsData = [];
            updatePaginationControls();
        } finally {
            refreshBtn.textContent = "Cargar Datos";
            refreshBtn.disabled = false;
        }
    }

    /* --- Audit Table Logic --- */
    function renderAuditTable() {
        if (!auditData || auditData.length === 0) {
            auditTableBody.innerHTML = '<tr><td colspan="6" class="loading">No hay logs en la base de datos.</td></tr>';
            updatePaginationControls();
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

        updatePaginationControls();
    }

    function updatePaginationControls() {
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

    prevBtn.addEventListener('click', () => {
        if (currentPage > 1) { currentPage--; renderAuditTable(); }
    });

    nextBtn.addEventListener('click', () => {
        const totalPages = Math.ceil(auditData.length / itemsPerPage);
        if (currentPage < totalPages) { currentPage++; renderAuditTable(); }
    });

    /* --- News Table Logic --- */
    function renderNewsTable() {
        if (!newsData || newsData.length === 0) {
            newsTableBody.innerHTML = '<tr><td colspan="4" class="loading">No hay noticias de alto impacto en caché.</td></tr>';
            return;
        }

        newsTableBody.innerHTML = '';
        // Sort news by date ascending
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

    function renderGrafanaCharts() {
        if (chartInstances.balanceChart) chartInstances.balanceChart.destroy();
        if (chartInstances.dailyChart) chartInstances.dailyChart.destroy();
        if (chartInstances.winLossChart) chartInstances.winLossChart.destroy();
        if (chartInstances.symbolChart) chartInstances.symbolChart.destroy();

        Chart.defaults.color = '#a0a0b0';
        Chart.defaults.font.family = 'Inter';

        // 1. Balance Evolution
        const ctxBal = document.getElementById('balanceChart').getContext('2d');
        const labelsBal = statsData.evolution.map(x => x.time);
        const dataBal = statsData.evolution.map(x => x.cumulative);
        chartInstances.balanceChart = new Chart(ctxBal, {
            type: 'line',
            data: {
                labels: labelsBal,
                datasets: [{
                    label: 'Evolución de Saldo (€)',
                    data: dataBal,
                    borderColor: '#00f2fe',
                    backgroundColor: 'rgba(0, 242, 254, 0.1)',
                    fill: true,
                    tension: 0.3
                }]
            },
            options: { responsive: true, plugins: { title: { display: true, text: 'Curva de Capital (30 Días)', color: '#fff' } } }
        });

        // 2. Daily PnL
        const ctxDaily = document.getElementById('dailyChart').getContext('2d');
        const labelsDaily = Object.keys(statsData.daily_pnl);
        const dataDaily = Object.values(statsData.daily_pnl);
        chartInstances.dailyChart = new Chart(ctxDaily, {
            type: 'bar',
            data: {
                labels: labelsDaily,
                datasets: [{
                    label: 'PnL Diario (€)',
                    data: dataDaily,
                    backgroundColor: dataDaily.map(v => v >= 0 ? 'rgba(76, 175, 80, 0.7)' : 'rgba(244, 67, 54, 0.7)')
                }]
            },
            options: { responsive: true, plugins: { title: { display: true, text: 'Rentabilidad Diaria', color: '#fff' } } }
        });

        // 3. Win/Loss
        const ctxWL = document.getElementById('winLossChart').getContext('2d');
        chartInstances.winLossChart = new Chart(ctxWL, {
            type: 'doughnut',
            data: {
                labels: ['Ganadas', 'Perdidas'],
                datasets: [{
                    data: [statsData.wins, statsData.losses],
                    backgroundColor: ['#4caf50', '#f44336'],
                    borderWidth: 0
                }]
            },
            options: { responsive: true, plugins: { title: { display: true, text: 'Tasa de Acierto Global', color: '#fff' } } }
        });

        // 4. By Symbol
        const ctxSym = document.getElementById('symbolChart').getContext('2d');
        const labelsSym = Object.keys(statsData.by_symbol);
        const dataSym = Object.values(statsData.by_symbol);
        chartInstances.symbolChart = new Chart(ctxSym, {
            type: 'bar',
            data: {
                labels: labelsSym,
                datasets: [{
                    label: 'PnL por Activo (€)',
                    data: dataSym,
                    backgroundColor: dataSym.map(v => v >= 0 ? '#2196f3' : '#ff9800')
                }]
            },
            options: { indexAxis: 'y', responsive: true, plugins: { title: { display: true, text: 'Rendimiento por Par', color: '#fff' } } }
        });
        // 5. Update KPI Cards
        if (statsData.kpis) {
            const k = statsData.kpis;
            document.getElementById('kpiBalance').textContent = k.balance.toFixed(2) + ' €';

            const startBalance = k.balance - k.profit_7d;
            const retPct = startBalance > 0 ? (k.profit_7d / startBalance) * 100 : 0;
            const retEl = document.getElementById('kpiReturn');
            retEl.textContent = (retPct > 0 ? '+' : '') + retPct.toFixed(2) + '%';
            retEl.className = retPct >= 0 ? 'positive' : 'negative';

            document.getElementById('kpiBest').textContent = (k.best_trade > 0 ? '+' : '') + k.best_trade.toFixed(2) + ' €';
            document.getElementById('kpiWorst').textContent = k.worst_trade.toFixed(2) + ' €';
            document.getElementById('kpiConsWins').textContent = k.max_win_streak + ' op';
            document.getElementById('kpiConsLosses').textContent = k.max_loss_streak + ' op';
            document.getElementById('kpiConsProfit').textContent = '+' + k.max_consec_profit.toFixed(2) + ' €';
        }
    }

    refreshBtn.addEventListener('click', fetchAllData);

    // Initial load
    fetchAllData();
});
