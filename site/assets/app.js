(() => {
  const searchInput = document.querySelector("#catalog-search");
  const sourceFilter = document.querySelector("#source-filter");
  const repositoryCards = [...document.querySelectorAll("[data-repository]")];
  const resultsStatus = document.querySelector("#results-status");
  const noResults = document.querySelector("#no-results");
  const toast = document.querySelector("#copy-toast");
  let toastTimer;

  const normalize = (value) => value.trim().toLocaleLowerCase();

  function applyFilters() {
    const query = normalize(searchInput.value);
    const selectedSource = sourceFilter.value;
    let visibleCharts = 0;
    let visibleRepositories = 0;

    repositoryCards.forEach((card) => {
      const sourceMatches = selectedSource === "all" || card.dataset.repository === selectedSource;
      const repositoryMatches = card.dataset.search.includes(query);
      const rows = [...card.querySelectorAll("[data-chart-row]")];
      let visibleRows = 0;

      rows.forEach((row) => {
        const matches = sourceMatches && (!query || repositoryMatches || row.dataset.search.includes(query));
        row.hidden = !matches;
        if (matches) visibleRows += 1;
      });

      const isEmptyRepository = rows.length === 0;
      const showEmptyRepository = isEmptyRepository && sourceMatches && (!query || repositoryMatches);
      const showCard = visibleRows > 0 || showEmptyRepository;
      card.hidden = !showCard;
      if (showCard) visibleRepositories += 1;
      visibleCharts += visibleRows;
    });

    const chartNoun = visibleCharts === 1 ? "chart" : "charts";
    const sourceNoun = visibleRepositories === 1 ? "source" : "sources";
    resultsStatus.textContent = `${visibleCharts} ${chartNoun} across ${visibleRepositories} ${sourceNoun}`;
    noResults.hidden = visibleRepositories !== 0;
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 1800);
  }

  async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    try {
      await copyText(button.dataset.copy);
      const originalLabel = button.dataset.copyLabel || "Copy";
      button.textContent = "Copied";
      showToast("Copied to clipboard");
      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1400);
    } catch {
      showToast("Could not copy automatically");
    }
  });

  document.addEventListener("keydown", (event) => {
    const element = document.activeElement;
    const isTyping = element && ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName);
    if (event.key === "/" && !isTyping) {
      event.preventDefault();
      searchInput.focus();
    }
    if (event.key === "Escape" && element === searchInput) {
      searchInput.value = "";
      searchInput.blur();
      applyFilters();
    }
  });

  searchInput.addEventListener("input", applyFilters);
  sourceFilter.addEventListener("change", applyFilters);
  applyFilters();
})();
