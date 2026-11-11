(() => {
  "use strict";

  const CARD_TYPES = new Set(["Crédito à vista", "Parcelado"]);
  const DEFAULT_CLOSING_DAY = 3;

  const parseClosingDay = (value) => {
    if (typeof value === "boolean" || value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) && Number.isInteger(number) && number >= 1 && number <= 31 ? number : null;
  };

  const parseDate = (value) => {
    const match = String(value || "").trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const candidate = new Date(Date.UTC(year, month - 1, day));
    if (candidate.getUTCFullYear() !== year || candidate.getUTCMonth() !== month - 1 || candidate.getUTCDate() !== day) return null;
    return { year, month, day };
  };

  const daysInMonth = (year, month) => new Date(Date.UTC(year, month, 0)).getUTCDate();

  const monthValue = (year, month) => `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}`;

  const suggestFirstBillMonth = (dateValue, closingDay) => {
    const parsed = parseDate(dateValue);
    const closing = parseClosingDay(closingDay);
    if (!parsed || closing === null) return null;
    const effectiveClosing = Math.min(closing, daysInMonth(parsed.year, parsed.month));
    if (parsed.day <= effectiveClosing) return monthValue(parsed.year, parsed.month);
    return parsed.month === 12 ? monthValue(parsed.year + 1, 1) : monthValue(parsed.year, parsed.month + 1);
  };

  const parseClosingMap = (value) => {
    if (!value) return {};
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_) {
      return {};
    }
  };

  const monthLabel = (value) => {
    const match = String(value || "").match(/^(\d{4})-(\d{2})$/);
    if (!match) return String(value || "mês não definido");
    return `${match[2]}/${match[1]}`;
  };

  const setupForm = (form) => {
    const type = form.querySelector("[data-type-selector]");
    const date = form.querySelector('[name="transaction_date"]');
    const card = form.querySelector('[name="account_card"]');
    const amount = form.querySelector('[name="total_amount"][data-total-amount]');
    const negativeHint = form.querySelector("[data-negative-amount-hint]");
    const firstBill = form.querySelector('[name="first_bill_month"][data-first-bill-month]');
    const field = form.querySelector("[data-card-billing-field]");
    const status = form.querySelector("[data-first-bill-status]");
    const mode = form.querySelector("[data-first-bill-mode]");
    const reset = form.querySelector("[data-reset-first-bill]");
    if (!type || !date || !card || !firstBill) return;

    const closingDays = parseClosingMap(form.dataset.cardClosingDays);
    let currentSuggestion = null;
    let state = firstBill.value ? "manual" : "empty";

    const selectedCard = () => {
      const option = card.options[card.selectedIndex];
      if (!option || !option.value || !option.dataset.closingDay) return null;
      const mapped = parseClosingDay(closingDays[option.value]);
      const optionDay = parseClosingDay(option.dataset.closingDay);
      return { name: option.value, closingDay: mapped || optionDay || DEFAULT_CLOSING_DAY };
    };

    const isCardType = () => CARD_TYPES.has(type.value);
    let wasCardType = isCardType();

    const updateAmountValidation = () => {
      if (!amount) return;
      const allowRefund = type.value === "Crédito à vista" && !!selectedCard();
      if (allowRefund) amount.removeAttribute("min");
      else amount.min = "0.01";
      if (negativeHint) negativeHint.hidden = !allowRefund;
    };

    const renderState = (message) => {
      if (!status) return;
      status.textContent = message;
      status.dataset.mode = state;
      if (mode) {
        mode.textContent = state === "automatic" ? "Automática" : state === "manual" ? "Manual" : "A escolher";
        mode.dataset.mode = state;
      }
      if (reset) {
        reset.hidden = !isCardType();
        reset.disabled = !currentSuggestion;
      }
    };

    const renderInitialState = () => {
      if (!isCardType()) {
        renderState("Escolha Crédito à vista ou Parcelado para sugerir a primeira fatura.");
      } else if (firstBill.value) {
        // Existing edits and notification/AI drafts are intentionally preserved.
        const selected = selectedCard();
        currentSuggestion = selected ? suggestFirstBillMonth(date.value, selected.closingDay) : null;
        state = "manual";
        renderState(`Valor preservado: ${monthLabel(firstBill.value)}. Você pode usar a sugestão abaixo.`);
      } else {
        renderState("Informe data e cartão para ver a sugestão automática.");
      }
    };

    const recalculateFromSource = () => {
      currentSuggestion = null;
      if (!isCardType()) {
        renderInitialState();
        return;
      }
      const selected = selectedCard();
      if (!selected) {
        firstBill.value = "";
        state = "empty";
        renderState("Selecione um cartão para ver a sugestão automática.");
        return;
      }
      currentSuggestion = suggestFirstBillMonth(date.value, selected.closingDay);
      if (!currentSuggestion) {
        firstBill.value = "";
        state = "empty";
        renderState("Informe a data do lançamento para ver a sugestão automática.");
        return;
      }
      firstBill.value = currentSuggestion;
      state = "automatic";
      renderState(`Sugestão automática: ${monthLabel(currentSuggestion)} (fechamento dia ${selected.closingDay}).`);
    };

    const updateVisibility = () => {
      const visible = isCardType();
      if (field) field.hidden = !visible;
      if (reset) reset.hidden = !visible;
    };

    type.addEventListener("change", () => {
      const enteringCardType = !wasCardType && isCardType();
      wasCardType = isCardType();
      updateVisibility();
      if (enteringCardType) recalculateFromSource();
      updateAmountValidation();
    });
    card.addEventListener("change", () => {
      recalculateFromSource();
      updateAmountValidation();
    });
    date.addEventListener("change", recalculateFromSource);
    firstBill.addEventListener("input", () => {
      state = firstBill.value ? "manual" : "empty";
      renderState(firstBill.value ? `Definida manualmente: ${monthLabel(firstBill.value)}.` : "Escolha a primeira fatura ou use a sugestão.");
    });
    reset?.addEventListener("click", () => {
      if (!currentSuggestion) recalculateFromSource();
      if (!currentSuggestion) return;
      firstBill.value = currentSuggestion;
      state = "automatic";
      renderState(`Sugestão automática: ${monthLabel(currentSuggestion)}.`);
    });

    updateVisibility();
    if (form.dataset.cardBillingNew === "1" && isCardType() && !firstBill.value) recalculateFromSource();
    else renderInitialState();
    updateAmountValidation();
  };

  const setupReserveFilter = (form) => {
    const select = form.querySelector("[data-reserve-selector]");
    const date = form.querySelector('[name="transaction_date"]');
    const preserve = form.querySelector("[data-preserve-linked-pot]");
    const clear = form.querySelector("[data-clear-stale-link]");
    const warning = form.querySelector("[data-stale-reserve-warning]");
    const status = form.querySelector("[data-reserve-options-status]");
    if (!select || !date) return;
    let requestNumber = 0;
    const refresh = async () => {
      const month = String(date.value || "").slice(0, 7);
      const selectedBefore = select.value;
      const preservedBefore = preserve?.value || "";
      const currentRequest = ++requestNumber;
      if (!/^\d{4}-\d{2}$/.test(month)) {
        if (status) status.textContent = "Informe uma data válida para listar as reservas.";
        return;
      }
      if (status) status.textContent = "Atualizando reservas disponíveis…";
      try {
        const endpoint = new URL(select.dataset.reserveOptionsUrl || "/api/reserve-options", window.location.href);
        endpoint.searchParams.set("month", month);
        const response = await fetch(`${endpoint.pathname}${endpoint.search}`, {
          credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("reserve options unavailable");
        const payload = await response.json();
        if (currentRequest !== requestNumber) return;
        const names = Array.isArray(payload.reserves) ? payload.reserves.map(String) : [];
        const wanted = selectedBefore || preservedBefore;
        const fragment = document.createDocumentFragment();
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "Sem reserva vinculada";
        fragment.append(emptyOption);
        names.forEach((name) => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = name;
          fragment.append(option);
        });
        select.replaceChildren(fragment);
        if (wanted && names.includes(wanted)) {
          select.value = wanted;
          if (preserve && preservedBefore === wanted) preserve.value = "";
          if (clear) clear.checked = false;
          if (warning) warning.hidden = true;
          if (status) status.textContent = `Reservas de ${monthLabel(payload.month || month)} atualizadas.`;
        } else {
          select.value = "";
          if (preservedBefore) {
            if (warning) {
              warning.hidden = false;
              warning.textContent = `O vínculo atual “${preservedBefore}” não está planejado para ${monthLabel(payload.month || month)}. Escolha uma reserva válida ou marque a remoção explícita.`;
            }
            if (status) status.textContent = "O vínculo atual precisa ser resolvido antes de salvar.";
          } else if (selectedBefore) {
            if (warning) warning.hidden = true;
            if (status) status.textContent = `“${selectedBefore}” não está planejada para ${monthLabel(payload.month || month)}; escolha outra reserva ou salve sem vínculo.`;
          } else if (status) {
            status.textContent = names.length ? `Reservas de ${monthLabel(payload.month || month)} atualizadas.` : `Nenhuma reserva planejada para ${monthLabel(payload.month || month)}.`;
          }
        }
      } catch (_) {
        if (currentRequest === requestNumber && status) status.textContent = "Não foi possível atualizar as reservas. Recarregue o formulário.";
      }
    };
    select.addEventListener("change", () => {
      if (preserve) preserve.value = "";
      if (clear) clear.checked = false;
      if (warning) warning.hidden = true;
      if (status) status.textContent = "Vínculo atualizado no formulário; confirme ao salvar.";
    });
    date.addEventListener("change", refresh);
    refresh();
  };

  document.querySelectorAll("form[data-transaction-form]").forEach((form) => {
    setupForm(form);
    setupReserveFilter(form);
  });

  // Expose only pure operations for standalone browser tests; the form state
  // remains private so another page cannot accidentally mutate it.
  window.cardBilling = Object.freeze({ suggestFirstBillMonth });
})();
