(() => {
  // Remove apenas a cópia cifrada legada; nada é apagado no ledger compartilhado.
  try {
    const cleanupFlag = "finfam-legacy-offline-vault-cleaned";
    if (!window.localStorage.getItem(cleanupFlag)) {
      const request = window.indexedDB?.deleteDatabase("finfam-offline-vault-v1");
      if (!request) window.localStorage.setItem(cleanupFlag, "1");
      else request.onsuccess = () => window.localStorage.setItem(cleanupFlag, "1");
    }
  } catch (_) {}
  const offlineBanner = document.querySelector("[data-offline-banner]");
  const updateOfflineNotice = () => {
    if (offlineBanner) offlineBanner.hidden = navigator.onLine;
  };
  window.addEventListener("online", updateOfflineNotice);
  window.addEventListener("offline", updateOfflineNotice);
  updateOfflineNotice();

  const installButton = document.querySelector("[data-install-app]");
  let installPrompt = null;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    if (installButton) installButton.hidden = false;
  });

  installButton?.addEventListener("click", async () => {
    if (!installPrompt) return;
    await installPrompt.prompt();
    installPrompt = null;
    installButton.hidden = true;
  });

  document.querySelectorAll("form[data-transaction-form]").forEach((form) => {
    const type = form.querySelector("[data-type-selector]");
    const repeat = form.querySelector('[name="repeat_monthly"]');
    const destination = form.querySelector("[data-transfer-destination]");
    const installmentCount = form.querySelector("[data-installment-count]");
    const recurringTypes = new Set(["Receita", "Fixo", "Reserva", "Caixinha", "Saque Caixinha", "Transferência"]);
    const updateFields = () => {
      const selected = type?.value || "";
      const isCard = selected === "Crédito à vista" || selected === "Parcelado";
      const isRecurring = recurringTypes.has(selected);
      form.querySelectorAll("[data-show-for]").forEach((field) => {
        const condition = field.dataset.showFor;
        field.hidden = condition === "card" ? !isCard
          : condition === "installments" ? selected !== "Parcelado"
          : condition === "transfer" ? selected !== "Transferência"
          : condition === "repeat-end" ? !isRecurring || !repeat?.checked
          : false;
      });
      const repeatLabel = form.querySelector("[data-repeat-toggle]");
      if (repeatLabel) repeatLabel.hidden = !isRecurring;
      if (!isRecurring && repeat) repeat.checked = false;
      if (destination) destination.required = selected === "Transferência";
      if (installmentCount) {
        const isInstallmentPurchase = selected === "Parcelado";
        installmentCount.disabled = !isInstallmentPurchase;
        installmentCount.required = isInstallmentPurchase;
        if (isInstallmentPurchase && Number(installmentCount.value) < 2) installmentCount.value = "2";
      }
    };
    type?.addEventListener("change", updateFields);
    repeat?.addEventListener("change", updateFields);
    updateFields();
  });

  document.querySelectorAll("[data-category-suggestion]").forEach((picker) => {
    picker.addEventListener("change", () => {
      const input = picker.form?.querySelector('[name="category"]');
      if (input && picker.value) input.value = picker.value;
      picker.value = "";
      input?.focus();
    });
  });

  const assistantPreview = document.querySelector("[data-assistant-preview]");
  document.querySelectorAll("[data-card-selector]").forEach((selector) => {
    selector.addEventListener("change", () => selector.form?.requestSubmit());
  });

  if (assistantPreview) {
    const form = assistantPreview.closest("form");
    const targetInput = form?.querySelector("[data-target-amount]");
    const sourceInput = form?.querySelector("[data-source-amount]");
    const targetSelect = form?.querySelector('[name="target_pot"]');
    const sourceSelect = form?.querySelector('[name="source_pot"]');
    const startMonth = form?.querySelector('[name="start_month"]');
    const reserveSchedule = JSON.parse(form?.dataset.potValues || "{}");
    const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
    const updateAssistantPreview = () => {
      const targetNow = Number(assistantPreview.dataset.targetCurrent || 0);
      const sourceNow = Number(assistantPreview.dataset.sourceCurrent || 0);
      const targetNext = Number(targetInput?.value || 0);
      const sourceNext = Number(sourceInput?.value || 0);
      const targetDelta = targetNext - targetNow;
      const sourceDelta = sourceNext - sourceNow;
      const totalDelta = targetNext + sourceNext - targetNow - sourceNow;
      assistantPreview.querySelector("[data-target-preview]").textContent = money.format(targetNext);
      assistantPreview.querySelector("[data-source-preview]").textContent = money.format(sourceNext);
      assistantPreview.querySelector("[data-target-delta]").textContent = `${targetDelta >= 0 ? "+" : "−"}${money.format(Math.abs(targetDelta))} / mês`;
      assistantPreview.querySelector("[data-source-delta]").textContent = `${sourceDelta >= 0 ? "+" : "−"}${money.format(Math.abs(sourceDelta))} / mês`;
      const net = assistantPreview.querySelector("[data-total-delta]");
      net.textContent = `${totalDelta >= 0 ? "+" : "−"}${money.format(Math.abs(totalDelta))} / mês`;
      net.classList.toggle("negative", Math.abs(totalDelta) > 0.01);
    };
    const resetAssistantBase = () => {
      const monthValues = reserveSchedule[startMonth?.value || ""] || {};
      const targetNow = Number(monthValues[targetSelect?.value || ""] || 0);
      const sourceNow = Number(monthValues[sourceSelect?.value || ""] || 0);
      assistantPreview.dataset.targetCurrent = String(targetNow);
      assistantPreview.dataset.sourceCurrent = String(sourceNow);
      if (targetInput) targetInput.value = targetNow.toFixed(2);
      if (sourceInput) sourceInput.value = sourceNow.toFixed(2);
      updateAssistantPreview();
    };
    targetInput?.addEventListener("input", updateAssistantPreview);
    sourceInput?.addEventListener("input", updateAssistantPreview);
    targetSelect?.addEventListener("change", resetAssistantBase);
    sourceSelect?.addEventListener("change", resetAssistantBase);
    startMonth?.addEventListener("change", resetAssistantBase);
    updateAssistantPreview();
  }

  const globalContext = document.querySelector("[data-global-context]");
  const assistantDialog = document.querySelector("[data-assistant-dialog]");
  if (assistantDialog) {
    const assistantForm = assistantDialog.querySelector("[data-assistant-form]");
    const assistantMessages = assistantDialog.querySelector("[data-assistant-messages]");
    const assistantQuestion = assistantDialog.querySelector("[data-assistant-question]");
    const assistantConsent = assistantDialog.querySelector("[data-assistant-consent]");
    const assistantStatus = assistantDialog.querySelector("[data-assistant-status]");
    const assistantSubmit = assistantDialog.querySelector("[data-assistant-submit]");
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const assistantHistory = [];
    let assistantPending = false;

    const getAssistantContext = () => {
      const form = globalContext?.querySelector("[data-context-v5]");
      const value = (name, fallback) => form?.elements.namedItem(name)?.value || globalContext?.dataset[name] || fallback;
      return { month: value("month", ""), person: value("person", "Família"), horizon: value("horizon", "12") };
    };
    const setAssistantStatus = (message, isError = false) => {
      if (!assistantStatus) return;
      assistantStatus.textContent = message;
      assistantStatus.classList.toggle("negative", isError);
    };
    const trimAssistantHistory = () => {
      while (assistantHistory.length > 4) assistantHistory.shift();
      if (!assistantMessages) return;
      const messageNodes = assistantMessages.querySelectorAll("[data-assistant-message]");
      while (messageNodes.length > 4) {
        messageNodes[0].remove();
        break;
      }
    };
    const appendAssistantMessage = (role, content) => {
      if (!assistantMessages) return null;
      const article = document.createElement("article");
      article.className = `assistant-message assistant-message-${role}`;
      article.dataset.assistantMessage = role;
      const label = document.createElement("strong");
      label.textContent = role === "user" ? "Você" : "Assistente";
      const text = document.createElement("p");
      text.textContent = content;
      article.append(label, text);
      assistantMessages.append(article);
      trimAssistantHistory();
      return article;
    };
    const appendHidden = (form, name, value) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value == null ? "" : String(value);
      form.append(input);
    };
    const appendProposal = (container, proposal, context) => {
      const card = document.createElement("section");
      card.className = "assistant-proposal";
      const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
      const formatMoney = (value) => {
        const amount = Number(value);
        return Number.isFinite(amount) ? money.format(amount) : "valor a conferir";
      };
      const formatDelta = (value) => {
        const amount = Number(value);
        if (!Number.isFinite(amount)) return "variação a conferir";
        return `${amount >= 0 ? "+" : "−"}${money.format(Math.abs(amount))}`;
      };
      const monthLabel = (value) => {
        const match = String(value || "").slice(0, 7).match(/^(\d{4})-(\d{2})$/);
        const month = match ? Number(match[2]) : 0;
        if (!match || month < 1 || month > 12) return String(value || "mês a conferir");
        return new Intl.DateTimeFormat("pt-BR", { month: "short", year: "numeric", timeZone: "UTC" })
          .format(new Date(Date.UTC(Number(match[1]), month - 1, 1)));
      };
      const appendStep = (flow, label, content) => {
        const paragraph = document.createElement("p");
        paragraph.className = "assistant-proposal-step";
        const heading = document.createElement("strong");
        heading.textContent = label;
        paragraph.append(heading, document.createTextNode(content));
        flow.append(paragraph);
      };
      const title = document.createElement("h3");
      title.textContent = String(proposal?.title || "Sugestão para revisar");
      const transaction = proposal?.entry_summary || proposal?.action?.transaction;
      const previewRows = (Array.isArray(proposal?.preview) ? proposal.preview : [])
        .filter((row) => row && typeof row === "object")
        .slice(0, 24);
      const previewMonths = [...new Set(previewRows
        .map((row) => String(row.month || "").slice(0, 7))
        .filter((value) => /^\d{4}-\d{2}$/.test(value)))];
      const flow = document.createElement("div");
      flow.className = "assistant-proposal-flow";
      const firstPreview = previewRows[0];
      const situation = firstPreview
        ? `Antes da proposta, o saldo projetado em ${monthLabel(firstPreview.month)} era ${formatMoney(firstPreview.base_balance)}.`
        : "Nenhuma alteração foi aplicada; a situação atual continua preservada para conferência.";
      appendStep(flow, "Situação atual", situation);
      let change = "A proposta é informativa e não altera dados por conta própria.";
      if (transaction) {
        const kind = proposal?.action?.kind === "edit_transaction" ? "editar" : "criar";
        const type = String(transaction.type || "lançamento");
        const description = String(transaction.description || "").trim();
        const amount = transaction.total_amount != null ? ` de ${formatMoney(transaction.total_amount)}` : "";
        const date = String(transaction.transaction_date || transaction.first_bill_month || "data a conferir");
        change = `${kind[0].toUpperCase()}${kind.slice(1)} ${type}${amount}${description ? ` · ${description}` : ""} em ${date}.`;
      }
      appendStep(flow, "Mudança sugerida", change);
      appendStep(flow, "Meses afetados", previewMonths.length
        ? previewMonths.map(monthLabel).join(", ")
        : "o app ainda não conseguiu recalcular meses para esta proposta");
      card.append(title, flow);

      const impact = document.createElement("div");
      impact.className = "assistant-proposal-impact";
      const impactTitle = document.createElement("strong");
      impactTitle.textContent = "Impacto recalculado pelo app · somente leitura";
      impact.append(impactTitle);
      if (previewRows.length) {
        previewRows.forEach((row) => {
          const balanceBase = Number(row.base_balance);
          const balanceChanged = Number(row.changed_balance);
          const budgetBase = Number(row.base_budget);
          const budgetChanged = Number(row.changed_budget);
          const line = document.createElement("p");
          line.className = "assistant-proposal-impact-row";
          const balanceDelta = Number.isFinite(balanceBase) && Number.isFinite(balanceChanged)
            ? formatDelta(balanceChanged - balanceBase) : "variação a conferir";
          const budgetDelta = Number.isFinite(budgetBase) && Number.isFinite(budgetChanged)
            ? formatDelta(budgetChanged - budgetBase) : "variação a conferir";
          line.textContent = `${monthLabel(row.month)} · saldo ${formatMoney(balanceBase)} → ${formatMoney(balanceChanged)} (${balanceDelta}); resultado do mês ${budgetDelta}.`;
          impact.append(line);
        });
      } else {
        const missing = document.createElement("p");
        missing.className = "assistant-proposal-missing-impact";
        missing.textContent = "Sem recálculo local disponível para esta proposta; confira as premissas antes de qualquer ação.";
        impact.append(missing);
      }
      card.append(impact);

      const evidence = document.createElement("p");
      evidence.textContent = `Evidência: ${String(proposal?.evidence || "Não informada")}`;
      const period = document.createElement("p");
      period.textContent = `Período informado: ${String(proposal?.period || "a conferir")}`;
      const uncertainty = document.createElement("p");
      uncertainty.className = "muted";
      uncertainty.textContent = String(proposal?.review_note || proposal?.uncertainty || "Confira as premissas antes de salvar.");
      card.append(evidence, period, uncertainty);

      const reviewForm = document.createElement("form");
      reviewForm.method = "post";
      reviewForm.action = "/assistente/acao/revisar";
      reviewForm.className = "assistant-proposal-form";
      appendHidden(reviewForm, "_csrf", csrf);
      appendHidden(reviewForm, "token", proposal?.review_token || "");
      appendHidden(reviewForm, "_context_month", context.month);
      appendHidden(reviewForm, "_context_person", context.person);
      appendHidden(reviewForm, "_context_horizon", context.horizon);
      appendHidden(reviewForm, "month", context.month);
      appendHidden(reviewForm, "person", context.person);
      appendHidden(reviewForm, "horizon", context.horizon);
      const reviewButton = document.createElement("button");
      reviewButton.className = "button button-secondary";
      reviewButton.type = "submit";
      reviewButton.textContent = "Revisar";
      reviewButton.setAttribute("aria-label", "Revisar proposta no formulário normal");
      if (!proposal?.review_token) reviewButton.disabled = true;
      reviewForm.append(reviewButton);
      if (proposal?.review_token) card.append(reviewForm);
      container.append(card);
    };
    const appendAssistantResponse = (answer, proposals, context) => {
      const article = appendAssistantMessage("assistant", answer);
      if (!article) return;
      const proposalList = document.createElement("div");
      proposalList.className = "assistant-proposals";
      (Array.isArray(proposals) ? proposals : []).forEach((proposal) => appendProposal(proposalList, proposal, context));
      if (proposalList.childElementCount) article.append(proposalList);
    };
    const openAssistant = () => {
      if (typeof assistantDialog.showModal === "function") {
        if (!assistantDialog.open) assistantDialog.showModal();
      } else {
        assistantDialog.setAttribute("open", "");
        assistantDialog.classList.add("is-open");
      }
      window.setTimeout(() => assistantQuestion?.focus(), 0);
    };
    const closeAssistant = () => {
      if (typeof assistantDialog.close === "function" && assistantDialog.open) assistantDialog.close();
      else {
        assistantDialog.removeAttribute("open");
        assistantDialog.classList.remove("is-open");
      }
    };
    document.querySelectorAll("[data-assistant-open]").forEach((trigger) => {
      trigger.addEventListener("click", (event) => {
        if (typeof assistantDialog.showModal === "function" || assistantDialog) {
          event.preventDefault();
          openAssistant();
        }
      });
    });
    assistantDialog.querySelector("[data-assistant-close]")?.addEventListener("click", closeAssistant);
    assistantDialog.addEventListener("click", (event) => {
      if (event.target === assistantDialog) closeAssistant();
    });
    assistantDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeAssistant();
    });
    assistantDialog.querySelector("[data-assistant-clear]")?.addEventListener("click", () => {
      assistantHistory.length = 0;
      assistantMessages?.replaceChildren();
      if (assistantQuestion) assistantQuestion.value = "";
      if (assistantConsent) assistantConsent.checked = false;
      setAssistantStatus("Conversa limpa. Autorize o envio antes da próxima consulta.");
      assistantQuestion?.focus();
    });
    assistantForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (assistantPending) return;
      const question = assistantQuestion?.value.trim() || "";
      if (!assistantConsent?.checked) {
        setAssistantStatus("Marque o consentimento para consultar a IA.", true);
        assistantConsent?.focus();
        return;
      }
      if (!question) {
        setAssistantStatus("Escreva uma pergunta antes de enviar.", true);
        assistantQuestion?.focus();
        return;
      }
      const context = getAssistantContext();
      const history = assistantHistory.slice(-4);
      appendAssistantMessage("user", question);
      assistantHistory.push({ role: "user", content: question });
      trimAssistantHistory();
      assistantPending = true;
      if (assistantSubmit) assistantSubmit.disabled = true;
      setAssistantStatus("Consultando a IA…");
      try {
        const response = await fetch("/api/assistente/chat", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({
            question,
            ai_consent: true,
            _csrf: csrf,
            month: context.month,
            person: context.person,
            horizon: context.horizon,
            history,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.error) throw new Error(String(data.error || data.detail || "Não foi possível consultar a IA."));
        const answer = String(data.answer || "A IA não retornou uma resposta.");
        assistantHistory.push({ role: "assistant", content: answer });
        trimAssistantHistory();
        appendAssistantResponse(answer, data.proposals, context);
        setAssistantStatus("Resposta recebida. Propostas ficam pendentes de revisão.");
      } catch (error) {
        setAssistantStatus(error instanceof Error ? error.message : "Não foi possível consultar a IA.", true);
      } finally {
        assistantPending = false;
        if (assistantConsent) assistantConsent.checked = false;
        if (assistantSubmit) assistantSubmit.disabled = false;
        if (assistantQuestion) assistantQuestion.value = "";
      }
    });
  }

  const simulationResults = document.querySelector("[data-simulation-results]");
  const submittedAmount = Number(new URLSearchParams(window.location.search).get("amount") || 0);
  if (simulationResults && Number.isFinite(submittedAmount) && submittedAmount > 0) {
    window.requestAnimationFrame(() => {
      simulationResults.focus({ preventScroll: true });
      simulationResults.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

})();

// Revalidar a sessão e os dados quando o navegador restaura uma página por Voltar/Avançar.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

// Atualizações do shell são sempre manuais: nunca recarregar enquanto houver um rascunho.
if ("serviceWorker" in navigator && window.isSecureContext) {
  const hasDirtyForm = () => [...document.querySelectorAll("form")].some((form) =>
    [...form.elements].some((field) => {
      if (field.disabled || field.type === "hidden" || field.type === "submit" || field.type === "button") return false;
      if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
      if (field.tagName === "SELECT") {
        const options = [...field.options];
        if (field.multiple) return options.some((option) => option.selected !== option.defaultSelected);
        const defaults = options.map((option, index) => option.defaultSelected ? index : -1).filter((index) => index >= 0);
        const initialIndex = defaults.length ? defaults[defaults.length - 1] : (options.length ? 0 : -1);
        return field.selectedIndex !== initialIndex;
      }
      return field.value !== field.defaultValue;
    })
  );

  const showUpdateNotice = () => {
    if (document.querySelector("[data-app-update-notice]")) return;
    const notice = document.createElement("div");
    notice.className = "shell-notices";
    notice.dataset.appUpdateNotice = "true";
    notice.setAttribute("role", "status");
    notice.setAttribute("aria-live", "polite");
    const message = document.createElement("span");
    message.textContent = "Atualização disponível.";
    const update = document.createElement("button");
    update.className = "button button-primary";
    update.type = "button";
    update.textContent = "Atualizar";
    update.addEventListener("click", () => {
      if (hasDirtyForm()) {
        message.textContent = "Atualização disponível. Termine ou salve o rascunho antes de atualizar.";
        return;
      }
      window.location.reload();
    });
    notice.append(message, update);
    document.body.prepend(notice);
  };

  window.addEventListener("load", async () => {
    const hadController = Boolean(navigator.serviceWorker.controller);
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (hadController) showUpdateNotice();
    }, { once: true });
    try {
      const registration = await navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" });
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (worker.state === "installed" && navigator.serviceWorker.controller) showUpdateNotice();
        });
      });
    } catch (_) {
      // O shell continua utilizável mesmo quando o navegador não aceita o SW.
    }
  }, { once: true });
}
