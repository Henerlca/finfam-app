# Testes locais do parser

`run-tests.sh` compila `NotificationParser` e os casos sintéticos diretamente com `javac` e executa sem Android SDK, sem rede e sem biblioteca externa. Os casos cobrem compra, débito, PIX recebido, parcelas e rejeições de OTP, fatura, múltiplos valores e falha.

Os textos são sintéticos. A suíte não afirma compatibilidade com bancos reais nem substitui revisão em aparelho.
