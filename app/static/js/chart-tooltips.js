(() => {
  const charts = document.querySelectorAll("[data-chart-interactive]");
  let activeChart = null;
  const dismissActive = () => activeChart?.hide();

  charts.forEach((viewport) => {
    let points;
    try { points = JSON.parse(viewport.dataset.chartPoints || "[]"); }
    catch (_) { return; }
    if (!Array.isArray(points) || !points.length) return;
    const svg = viewport.querySelector("svg");
    const tooltip = viewport.querySelector("[data-chart-tooltip]");
    const heading = viewport.querySelector("[data-chart-tooltip-label]");
    const values = viewport.querySelector("[data-chart-tooltip-values]");
    const crosshair = viewport.querySelector("[data-chart-crosshair]");
    const circles = [...viewport.querySelectorAll("[data-chart-point-index]")];
    if (!svg || !tooltip || !heading || !values || !crosshair) return;
    circles.forEach((circle) => circle.querySelector("title")?.remove());
    let selected = -1;
    let pinned = false;
    const hide = () => {
      tooltip.hidden = true;
      crosshair.setAttribute("hidden", "");
      circles.forEach((circle) => circle.classList.remove("chart-point-active"));
      pinned = false;
      if (activeChart === controller) activeChart = null;
    };
    const controller = {
      hide,
      reposition: () => pinned ? positionTooltip() : hide(),
    };

    const screenPoint = (point) => {
      const matrix = svg.getScreenCTM();
      if (!matrix) return null;
      const position = svg.createSVGPoint();
      position.x = Number(point.x);
      position.y = Number(svg.dataset.plotTop);
      return position.matrixTransform(matrix);
    };
    const positionTooltip = () => {
      const anchor = screenPoint(points[selected]);
      if (!anchor || tooltip.hidden) return;
      const margin = 10;
      const box = tooltip.getBoundingClientRect();
      const bottom = viewport.getBoundingClientRect().bottom;
      const right = Math.max(margin, window.innerWidth - box.width - margin);
      const left = Math.max(margin, Math.min(anchor.x - box.width / 2, right));
      let top = anchor.y - box.height - margin;
      if (top < margin) top = Math.min(bottom + margin, window.innerHeight - box.height - margin);
      top = Math.max(margin, Math.min(top, window.innerHeight - box.height - margin));
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    };
    const show = (index, shouldPin = false) => {
      if (activeChart && activeChart !== controller) dismissActive();
      activeChart = controller;
      pinned = shouldPin;
      const point = points[index];
      if (!point) return;
      if (selected !== index) {
        selected = index;
        heading.textContent = point.label;
        values.replaceChildren();
        (point.entries || []).forEach((entry) => {
          const item = document.createElement("li");
          const name = document.createElement("span");
          const label = document.createElement("span");
          label.textContent = entry.label;
          const status = document.createElement("small");
          status.textContent = entry.status || "";
          name.append(label, status);
          const value = document.createElement("strong");
          value.textContent = entry.value;
          item.append(name, value);
          values.append(item);
        });
      }
      tooltip.hidden = false;
      crosshair.removeAttribute("hidden");
      crosshair.setAttribute("x1", point.x);
      crosshair.setAttribute("x2", point.x);
      circles.forEach((circle) => circle.classList.toggle("chart-point-active", Number(circle.dataset.chartPointIndex) === point.index));
      positionTooltip();
    };
    const nearestPoint = (event) => {
      const matrix = svg.getScreenCTM();
      if (!matrix) return -1;
      const position = svg.createSVGPoint();
      position.x = event.clientX;
      position.y = event.clientY;
      const local = position.matrixTransform(matrix.inverse());
      // A área inteira da coluna é utilizável; não é preciso acertar o círculo.
      if (local.x < Number(svg.dataset.plotLeft) - 8 || local.x > Number(svg.dataset.plotRight) + 8 ||
          local.y < Number(svg.dataset.plotTop) - 8 || local.y > Number(svg.dataset.plotBottom) + 8) return -1;
      let nearest = 0;
      points.forEach((point, index) => {
        if (Math.abs(point.x - local.x) < Math.abs(points[nearest].x - local.x)) nearest = index;
      });
      return nearest;
    };
    svg.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "mouse" || pinned) return;
      const index = nearestPoint(event);
      if (index < 0) hide();
      else show(index);
    });
    svg.addEventListener("pointerleave", (event) => {
      if (event.pointerType === "mouse" && !pinned) hide();
    });
    svg.addEventListener("click", (event) => {
      const index = nearestPoint(event);
      if (index < 0) return;
      viewport.focus({ preventScroll: true });
      show(index, true);
    });
    viewport.addEventListener("focus", () => show(Math.max(0, selected), true));
    viewport.addEventListener("focusout", (event) => {
      if (!viewport.contains(event.relatedTarget)) hide();
    });
    viewport.addEventListener("keydown", (event) => {
      if (event.target !== viewport) return;
      let index = Math.max(0, selected);
      if (event.key === "ArrowRight") index = Math.min(points.length - 1, index + 1);
      else if (event.key === "ArrowLeft") index = Math.max(0, index - 1);
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = points.length - 1;
      else if (event.key === "Escape") { hide(); return; }
      else return;
      event.preventDefault();
      show(index, true);
    });
    viewport.querySelector("[data-chart-tooltip-close]")?.addEventListener("click", hide);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("[data-chart-interactive]")) dismissActive();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") dismissActive();
  });
  // Foco/toque acompanha a rolagem da página; rolar o detalhe não o fecha.
  window.addEventListener("scroll", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-chart-tooltip]")) return;
    activeChart?.reposition();
  }, { passive: true, capture: true });
  window.addEventListener("resize", dismissActive, { passive: true });
})();
