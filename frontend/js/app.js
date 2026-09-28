/**
 * Main entry point for the trading dashboard.
 * Orchestrates API calls and UI rendering.
 */
import { fetchAllData } from './api.js';
import { renderAuditTable, updatePaginationControls, renderNewsTable, renderGrafanaCharts } from './ui.js';

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
            if (statsData && !statsData.error) renderGrafanaCharts(statsData, chartInstances);

        } catch (err) {
            console.error(err);
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

    // Initial load
    loadData();
});
