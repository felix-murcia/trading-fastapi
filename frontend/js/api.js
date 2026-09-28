/**
 * API client for the trading dashboard.
 * Handles all fetch calls to the backend.
 */

export async function fetchAuditData(apiUrl, token, limit = 200) {
    const headers = {
        "X-Internal-Token": token,
        "Accept": "application/json"
    };
    const response = await fetch(`${apiUrl}/v1/smc/audit?limit=${limit}`, { headers });
    if (!response.ok) throw new Error(`Audit HTTP: ${response.status}`);
    return response.json();
}

export async function fetchNewsData(apiUrl, token) {
    const headers = {
        "X-Internal-Token": token,
        "Accept": "application/json"
    };
    const response = await fetch(`${apiUrl}/v1/smc/news`, { headers });
    if (!response.ok) throw new Error(`News HTTP: ${response.status}`);
    return response.json();
}

export async function fetchStatsData(apiUrl, token) {
    const headers = {
        "X-Internal-Token": token,
        "Accept": "application/json"
    };
    try {
        const response = await fetch(`${apiUrl}/v1/smc/analytics`, { headers });
        if (!response.ok) return null;
        return response.json();
    } catch (e) {
        return null;
    }
}

export async function fetchAllData(apiUrl, token) {
    const [auditData, newsData, statsData] = await Promise.all([
        fetchAuditData(apiUrl, token),
        fetchNewsData(apiUrl, token),
        fetchStatsData(apiUrl, token)
    ]);
    return { auditData, newsData, statsData };
}
