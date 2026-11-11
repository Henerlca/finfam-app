(() => {
  const header = document.querySelector(".topbar");
  const navigation = document.querySelector("[data-primary-nav]");
  const desktopSlot = document.querySelector("[data-desktop-nav-slot]");
  const moreMenu = navigation?.querySelector(".more-menu");
  const desktopQuery = window.matchMedia("(min-width: 960px)");
  const updateHeaderHeight = () => {
    const height = header?.getBoundingClientRect().height || 150;
    document.documentElement.style.setProperty("--finfam-header-height", `${Math.ceil(height)}px`);
  };
  if (navigation && desktopSlot) {
    const mobilePosition = document.createComment("posição da navegação mobile");
    navigation.before(mobilePosition);
    const placeNavigation = () => {
      if (moreMenu) moreMenu.open = false;
      if (desktopQuery.matches) desktopSlot.append(navigation);
      else mobilePosition.after(navigation);
      updateHeaderHeight();
    };
    desktopQuery.addEventListener("change", placeNavigation);
    placeNavigation();
  }
  if (header && "ResizeObserver" in window) {
    new ResizeObserver(updateHeaderHeight).observe(header);
  }
  window.addEventListener("resize", updateHeaderHeight);
  moreMenu?.addEventListener("toggle", () => {
    moreMenu.querySelector(":scope > summary")?.setAttribute("aria-expanded", String(moreMenu.open));
  });
  document.addEventListener("click", (event) => {
    if (moreMenu?.open && !moreMenu.contains(event.target)) moreMenu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && moreMenu?.open) {
      moreMenu.open = false;
      moreMenu.querySelector(":scope > summary")?.focus();
    }
  });

  const context = document.querySelector("[data-global-context]");
  const form = context?.querySelector("form[data-context-v5]");
  if (!context || !form) return;

  const monthControl = form.elements.namedItem("month");
  const applyButton = form.querySelector("[data-context-apply]");
  const resetOnMonthChange = new Set(["from", "to", "start", "end"]);

  const resetDateRangeFilters = () => {
    form.querySelectorAll("input[type=hidden][name]").forEach((field) => {
      if (resetOnMonthChange.has(field.name)) field.remove();
    });
  };

  // Trocar o mês só prepara o próximo envio; aplicar é uma decisão única e explícita.
  monthControl?.addEventListener("change", resetDateRangeFilters);

  applyButton?.addEventListener("click", (event) => {
    event.preventDefault();
    if (typeof form.requestSubmit === "function") form.requestSubmit(applyButton);
    else form.submit();
  });

  const updateExpandedState = () => {
    context.querySelector("summary")?.setAttribute("aria-expanded", context.open ? "true" : "false");
  };
  context.addEventListener("toggle", updateExpandedState);
  updateExpandedState();

  const syncBox = document.querySelector("[data-family-sync]");
  const syncMessage = syncBox?.querySelector("[data-sync-message]");
  const refreshButton = syncBox?.querySelector("[data-family-refresh]");
  if (syncBox && syncMessage && !syncBox.hidden) {
    let revision = syncMessage.dataset.revision || "0";
    const formatTime = (value) => {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
    };
    const checkSharedUpdate = async () => {
      if (!navigator.onLine || document.hidden) return;
      try {
        const response = await fetch("/api/family-sync", { cache: "no-store", headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const state = await response.json();
        if (state.revision == null || String(state.revision) === "") {
          syncMessage.textContent = state.label || "Sincronização indisponível no momento.";
          return;
        }
        const nextRevision = String(state.revision);
        if (revision && nextRevision !== revision) {
          syncMessage.textContent = state.updated_at ? `Nova atualização familiar · ${formatTime(state.updated_at)}` : "Nova atualização familiar";
          if (refreshButton) refreshButton.hidden = false;
          revision = nextRevision;
          return;
        }
        syncMessage.textContent = state.updated_at ? `Atualizado ${formatTime(state.updated_at)}` : state.label || "Sem alterações registradas.";
      } catch (_) {
        // O estado atual continua disponível; a próxima consulta tentará novamente.
      }
    };
    refreshButton?.addEventListener("click", () => window.location.reload());
    window.addEventListener("online", checkSharedUpdate);
    document.addEventListener("visibilitychange", checkSharedUpdate);
    window.setInterval(checkSharedUpdate, 45000);
    window.setTimeout(checkSharedUpdate, 1200);
  }
})();
