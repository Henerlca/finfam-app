package com.finfam.companion.parser;

import java.math.BigDecimal;

/** Dados normalizados; nunca contém título, corpo ou texto bruto da notificação. */
public final class ParsedNotification {
    private final String eventId;
    private final String sourcePackage;
    private final String occurredAt;
    private final BigDecimal amount;
    private final String merchant;
    private final String typeHint;
    private final Integer installmentCount;

    public ParsedNotification(
            String eventId,
            String sourcePackage,
            String occurredAt,
            BigDecimal amount,
            String merchant,
            String typeHint,
            Integer installmentCount) {
        this.eventId = eventId;
        this.sourcePackage = sourcePackage;
        this.occurredAt = occurredAt;
        this.amount = amount;
        this.merchant = merchant;
        this.typeHint = typeHint;
        this.installmentCount = installmentCount;
    }

    public String getEventId() { return eventId; }
    public String getSourcePackage() { return sourcePackage; }
    public String getOccurredAt() { return occurredAt; }
    public BigDecimal getAmount() { return amount; }
    public String getMerchant() { return merchant; }
    public String getTypeHint() { return typeHint; }
    public Integer getInstallmentCount() { return installmentCount; }
}
