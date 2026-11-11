package com.finfam.companion.parser;

import java.math.BigDecimal;

public final class ParserSelfTest {
    private static final long POST_TIME = 1735689600000L;

    public static void main(String[] args) {
        ParsedNotification bancoA = parse("Compra aprovada em Mercado Aurora no valor de R$ 1.234,56");
        check(bancoA != null, "compra sintética do banco A deve aceitar");
        check(bancoA.getAmount().compareTo(new BigDecimal("1234.56")) == 0, "valor BR deve normalizar");
        check("Crédito à vista".equals(bancoA.getTypeHint()), "tipo crédito");
        check("mercado aurora".equals(bancoA.getMerchant()), "merchant explícito sem texto bruto");

        ParsedNotification bancoB = parse("Débito realizado em Padaria Central R$ 18,90");
        check(bancoB != null && "Débito/Pix".equals(bancoB.getTypeHint()), "débito sintético do banco B");
        check(bancoB != null && "padaria central".equals(bancoB.getMerchant()), "merchant antes do valor monetário");

        ParsedNotification bancoC = parse("PIX recebido de Joao da Silva: R$ 250,00");
        check(bancoC != null && "Receita".equals(bancoC.getTypeHint()), "PIX recebido sintético do banco C");
        check(bancoC.getMerchant().isEmpty(), "PIX recebido não pode vazar nome do pagador");

        ParsedNotification parcelado = parse("Compra aprovada em Loja X, parcelado em 3 parcelas de R$ 99,90");
        check(parcelado != null && Integer.valueOf(3).equals(parcelado.getInstallmentCount()), "parcelamento");

        check(parse("Seu código de autenticação é 123456 R$ 1,00") == null, "OTP deve rejeitar");
        check(parse("Fatura vence hoje: R$ 1.000,00") == null, "fatura deve rejeitar");
        check(parse("Compra aprovada em A R$ 10,00 e em B R$ 20,00") == null, "múltiplos valores devem rejeitar");
        check(parse("Compra aprovada R$ 10,00 falhou") == null, "falha deve rejeitar");
        check(parse("Compra aprovada em Loja X R$ 10,00") != null, "formato mínimo deve aceitar");

        ParsedNotification same = parse("Compra aprovada em Loja X R$ 10,00");
        check(same.getEventId().length() == 64, "event_id sha256 hex");
        check(!same.getOccurredAt().isEmpty(), "occurred_at ISO");
        System.out.println("ParserSelfTest: OK (" + 11 + " casos)");
    }

    private static ParsedNotification parse(String body) {
        return NotificationParser.parse(
                "br.synthetic.bank", "key-1", POST_TIME, "FinFam teste", body);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
