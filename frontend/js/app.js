/**
 * Main entry point for the trading dashboard.
 * Orchestrates API calls and UI rendering.
 */
import { fetchAllData, fetchStatsData } from './api.js';
import { renderAuditTable, updatePaginationControls, renderNewsTable, renderGrafanaCharts } from './ui.js';

document.addEventListener('DOMContentLoaded', () => {
    // Inputs & buttons
    const apiInput = document.getElementById('apiUrl');
    const tokenInput = document.getElementById('apiToken');
    const refreshBtn = document.getElementById('refreshBtn');

    // Filter controls
    const filterFrom = document.getElementById('filterFrom');
    const filterTo = document.getElementById('filterTo');
    const filterGranularity = document.getElementById('filterGranularity');
    const filterSymbol = document.getElementById('filterSymbol');
    const resetFiltersBtn = document.getElementById('resetFilters');
    const statsError = document.getElementById('statsError');

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

    // Filter state
    let filters = {
        from: '',
        to: '',
        granularity: 'D',
        symbol: '',
    };

    // Load saved credentials
    if (localStorage.getItem('apiUrl')) apiInput.value = localStorage.getItem('apiUrl');
    if (localStorage.getItem('apiToken')) tokenInput.value = localStorage.getItem('apiToken');

    // Load saved filters
    const savedFrom = localStorage.getItem('filterFrom');
    const savedTo = localStorage.getItem('filterTo');
    const savedGranularity = localStorage.getItem('filterGranularity');
    const savedSymbol = localStorage.getItem('filterSymbol');
    if (savedFrom) { filterFrom.value = savedFrom; filters.from = savedFrom; }
    if (savedTo) { filterTo.value = savedTo; filters.to = savedTo; }
    if (savedGranularity) { filterGranularity.value = savedGranularity; filters.granularity = savedGranularity; }
    if (savedSymbol) { filterSymbol.value = savedSymbol; filters.symbol = savedSymbol; }

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

    function showStatsError(msg) {
        statsError.textContent = msg;
        statsError.style.display = msg ? 'block' : 'none';
    }

    async function loadData() {
        refreshBtn.textContent = "Cargando...";
        refreshBtn.disabled = true;

        let apiUrl = apiInput.value.trim();
        if (apiUrl.endsWith('/')) apiUrl = apiUrl.slice(0, -1);
        const token = tokenInput.value.trim();

        // Save credentials locally
        if (apiUrl) localStorage.setItem('apiUrl', apiUrl);
        if (token) localStorage.setItem('apiToken', token);

        try {
            const data = await fetchAllData(apiUrl, token);
            auditData = data.auditData;
            newsData = data.newsData;
            statsData = data.statsData;

            currentPage = 1;
            renderAuditTable(auditData, auditTableBody, currentPage, itemsPerPage);
            renderNewsTable(newsData, newsTableBody);
            if (statsData && !statsData.error) {
                showStatsError('');
                renderGrafanaCharts(statsData, chartInstances);
            } else if (statsData && statsData.error) {
                showStatsError(statsData.error);
            }

        } catch (err) {
            console.error(err);
            showStatsError(`Error al cargar datos: ${err.message}`);
            auditTableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--error);">Error al cargar logs: ${err.message}</td></tr>`;
            newsTableBody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--error);">Error al cargar noticias: ${err.message}</td></tr>`;
            auditData = [];
            newsData = [];
            updatePaginationControls(auditData, currentPage, prevBtn, nextBtn, pageInfo, itemsPerPage);
        } finally {
            refreshBtn.textContent = "Cargar Datos";
            refreshBtn.disabled = false;
        }
    }

    async function loadStats() {
        let apiUrl = apiInput.value.trim();
        if (apiUrl.endsWith('/')) apiUrl = apiUrl.slice(0, -1);
        const token = tokenInput.value.trim();

        try {
            showStatsError('');
            const data = await fetchStatsData(apiUrl, token, filters);
            if (data.error) {
                showStatsError(data.error);
                return;
            }
            statsData = data;
            renderGrafanaCharts(statsData, chartInstances);
        } catch (err) {
            console.error(err);
            showStatsError(`Error al cargar estadísticas: ${err.message}`);
        }
    }

    prevBtn.addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            renderAuditTable(auditData, auditTableBody, currentPage, itemsPerPage);
            updatePaginationControls(auditData, currentPage, prevBtn, nextBtn, pageInfo, itemsPerPage);
        }
    });

    nextBtn.addEventListener('click', () => {
        const totalPages = Math.ceil(auditData.length / itemsPerPage);
        if (currentPage < totalPages) {
            currentPage++;
            renderAuditTable(auditData, auditTableBody, currentPage, itemsPerPage);
            updatePaginationControls(auditData, currentPage, prevBtn, nextBtn, pageInfo, itemsPerPage);
        }
    });

    refreshBtn.addEventListener('click', loadData);

    // Filter change listeners — recarga solo analytics, no audit ni news
    [filterFrom, filterTo, filterGranularity, filterSymbol].forEach(el => {
        el.addEventListener('change', () => {
            filters.from = filterFrom.value;
            filters.to = filterTo.value;
            filters.granularity = filterGranularity.value;
            filters.symbol = filterSymbol.value;

            // Persist
            localStorage.setItem('filterFrom', filters.from);
            localStorage.setItem('filterTo', filters.to);
            localStorage.setItem('filterGranularity', filters.granularity);
            localStorage.setItem('filterSymbol', filters.symbol);

            loadStats();
        });
    });

    resetFiltersBtn.addEventListener('click', () => {
        filterFrom.value = '';
        filterTo.value = '';
        filterGranularity.value = 'D';
        filterSymbol.value = '';

        filters = { from: '', to: '', granularity: 'D', symbol: '' };

        localStorage.removeItem('filterFrom');
        localStorage.removeItem('filterTo');
        localStorage.removeItem('filterGranularity');
        localStorage.removeItem('filterSymbol');

        loadStats();
    });

    // Initial load
    loadData();
});
