// Phase 1: prove the JSON API is reachable from the browser (no HTMX).
document.addEventListener("DOMContentLoaded", async () => {
    const el = document.getElementById("api-status");
    if (!el) return;
    const url = el.dataset.healthUrl;
    try {
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        const data = await response.json();
        el.textContent = `API állapot: ${data.status}`;
    } catch (error) {
        el.textContent = "API állapot: nem elérhető";
        console.error(error);
    }
});
