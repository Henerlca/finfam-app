package com.finfam.companion.parser;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.text.Normalizer;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Parser determinístico e conservador. Ambiguidade sempre retorna null. */
public final class NotificationParser {
    private static final Pattern AMOUNT = Pattern.compile(
            "(?i)R\\$\\s*((?:[0-9]{1,3}(?:\\.[0-9]{3})+|[0-9]+)(?:,[0-9]{2})?)\\b");
    private static final Pattern INSTALLMENTS = Pattern.compile(
            "(?:parcelad[oa]\\s+(?:em|de)\\s+|parcela\\s+\\d+\\s+de\\s+|\\b)([0-9]{1,2})\\s+parcelas?\\b");
    private static final Pattern MERCHANT = Pattern.compile(
            "\\b(?:em|no|na|de)\\s+([\\p{L}][\\p{L}\\s&'.-]{0,100}?)(?=\\s+(?:no\\s+valor|valor|em\\s+\\d|foi|as\\s+\\d)|\\s+r\\$|[.;!?,]|$)");

    private static final String CREDIT = "Crédito à vista";
    private static final String DEBIT_PIX = "Débito/Pix";
    private static final String INCOME = "Receita";

    private NotificationParser() { }

    public static ParsedNotification parse(
            String sourcePackage,
            String notificationKey,
            long postTimeMillis,
            String title,
            String body) {
        if (sourcePackage == null || sourcePackage.trim().isEmpty()
                || notificationKey == null || notificationKey.trim().isEmpty()
                || postTimeMillis <= 0) {
            return null;
        }

        String folded = fold((title == null ? "" : title) + " " + (body == null ? "" : body));
        if (folded.length() == 0 || folded.length() > 20000 || containsForbidden(folded)) {
            return null;
        }

        Matcher amountMatcher = AMOUNT.matcher(folded);
        BigDecimal amount = null;
        int amountCount = 0;
        while (amountMatcher.find()) {
            amountCount++;
            if (amountCount > 1) return null;
            String number = amountMatcher.group(1).replace(".", "").replace(',', '.');
            try {
                amount = new BigDecimal(number).setScale(2, RoundingMode.UNNECESSARY);
            } catch (RuntimeException invalidAmount) {
                return null;
            }
        }
        if (amount == null || amount.signum() <= 0) return null;

        boolean receivedPix = containsAny(folded,
                "pix recebido", "recebimento via pix", "recebimento pix",
                "voce recebeu um pix", "voce recebeu pix");
        boolean debitPix = containsAny(folded,
                "pix enviado", "pix realizado", "pix efetuado", "pix transferido",
                "debito realizado", "debito aprovado", "compra no debito",
                "compra debitada");
        boolean credit = containsAny(folded,
                "compra aprovada", "pagamento aprovado", "compra no credito",
                "compra no cartao", "cartao de credito", "credito a vista",
                "transacao aprovada");
        int kinds = (receivedPix ? 1 : 0) + (debitPix ? 1 : 0) + (credit ? 1 : 0);
        if (kinds != 1) return null;

        Integer installments = parseInstallments(folded);
        if (installments != null && installments < 1) return null;
        if (containsAny(folded, "parcelado", "parcelada") && installments == null) return null;
        if (receivedPix) installments = null;
        if (installments != null && installments == 1) installments = null;

        String typeHint = receivedPix ? INCOME : (debitPix ? DEBIT_PIX : CREDIT);
        String merchant = receivedPix ? "" : extractMerchant(folded, typeHint);
        String occurredAt = Instant.ofEpochMilli(postTimeMillis).toString();
        String eventId = sha256Hex(sourcePackage.trim() + "\u001f"
                + notificationKey.trim() + "\u001f"
                + postTimeMillis + "\u001f"
                + typeHint + "\u001f"
                + amount.toPlainString());
        return new ParsedNotification(
                eventId,
                sourcePackage.trim(),
                occurredAt,
                amount,
                merchant,
                typeHint,
                installments);
    }

    private static boolean containsForbidden(String text) {
        return containsAny(text,
                "senha", "codigo de verificacao", "codigo de autenticacao",
                "codigo de seguranca", "autenticacao", "verificacao",
                "one time password", "token de acesso", "\u0000")
                || Pattern.compile("\\botp\\b").matcher(text).find()
                || containsAny(text,
                "fatura", "vencimento", "boleto", "saldo", "promocao",
                "promocional", "cashback", "estorno", "reversao", "revertida",
                "falhou", "falha", "negada", "negado", "recusada", "recusado",
                "cancelada", "cancelado", "transacao nao concluida", "erro na transacao");
    }

    private static Integer parseInstallments(String text) {
        Matcher matcher = INSTALLMENTS.matcher(text);
        Integer value = null;
        while (matcher.find()) {
            try {
                int found = Integer.parseInt(matcher.group(1));
                if (value != null && value != found) return null;
                value = found;
            } catch (NumberFormatException ignored) {
                return null;
            }
        }
        return value;
    }

    private static String extractMerchant(String text, String typeHint) {
        Matcher matcher = MERCHANT.matcher(text);
        while (matcher.find()) {
            String candidate = cleanMerchant(matcher.group(1));
            if (candidate.isEmpty()) continue;
            String lower = candidate.toLowerCase(Locale.ROOT);
            if (lower.startsWith("cartao") || lower.startsWith("credito")
                    || lower.startsWith("debito") || lower.startsWith("pix")
                    || lower.startsWith("valor") || lower.startsWith("r ")) {
                continue;
            }
            return candidate;
        }
        return "";
    }

    private static String cleanMerchant(String candidate) {
        String withoutDigits = candidate.replaceAll("[0-9]", "");
        String clean = withoutDigits.replaceAll("[^\\p{L}\\s&'.-]", " ")
                .replaceAll("\\s+", " ").trim();
        while (clean.startsWith(".") || clean.startsWith("-")) clean = clean.substring(1).trim();
        while (clean.endsWith(".") || clean.endsWith("-")) clean = clean.substring(0, clean.length() - 1).trim();
        if (clean.length() > 100) clean = clean.substring(0, 100).trim();
        return clean;
    }

    private static boolean containsAny(String text, String... needles) {
        for (String needle : needles) if (text.contains(needle)) return true;
        return false;
    }

    private static String fold(String input) {
        String normalized = Normalizer.normalize(input, Normalizer.Form.NFD)
                .replaceAll("\\p{M}+", "");
        return normalized.toLowerCase(Locale.ROOT).replaceAll("\\s+", " ").trim();
    }

    private static String sha256Hex(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder(64);
            for (byte item : digest) result.append(String.format(Locale.ROOT, "%02x", item & 0xff));
            return result.toString();
        } catch (Exception impossible) {
            throw new IllegalStateException("SHA-256 indisponível", impossible);
        }
    }
}
