package com.finfam.companion.ui;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputFilter;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.finfam.companion.api.ApiClient;
import com.finfam.companion.crypto.SecurePrefs;
import com.finfam.companion.queue.PackageAllowlist;
import com.finfam.companion.queue.QueueStore;
import com.finfam.companion.service.FinfamNotificationListenerService;
import com.finfam.companion.service.SyncCoordinator;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class MainActivity extends Activity {
    private EditText pairingCode;
    private EditText deviceName;
    private EditText packageAllowlist;
    private TextView state;
    private Button pairButton;
    private ExecutorService executor;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        executor = Executors.newSingleThreadExecutor();
        setContentView(buildView());
        refreshState();
    }

    private View buildView() {
        int padding = dp(20);
        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(padding, padding, padding, padding);
        root.setBackgroundColor(getColor(com.finfam.companion.R.color.finfam_cream));
        scroll.addView(root);

        TextView title = text("FinFam • leitor Android", 24);
        title.setTextColor(getColor(com.finfam.companion.R.color.finfam_teal));
        root.addView(title, margins(0, 0, 0, 8));

        TextView intro = text("Companion opcional: lê apenas notificações de pacotes que você listar e cria rascunhos para revisão na PWA.", 15);
        intro.setTextColor(getColor(com.finfam.companion.R.color.finfam_teal));
        root.addView(intro, margins(0, 0, 0, 16));

        root.addView(text("Pareamento", 18), margins(0, 0, 0, 6));
        pairingCode = field("Código de 8 dígitos", InputType.TYPE_CLASS_NUMBER, 8);
        root.addView(pairingCode, margins(0, 0, 0, 8));
        deviceName = field("Nome deste aparelho (máx. 80)", InputType.TYPE_CLASS_TEXT, 80);
        root.addView(deviceName, margins(0, 0, 0, 8));
        pairButton = button("Parear com o FinFam");
        pairButton.setOnClickListener(v -> pair());
        root.addView(pairButton, margins(0, 0, 0, 12));

        Button settings = button("Abrir acesso às notificações do Android");
        settings.setOnClickListener(v -> openNotificationSettings());
        root.addView(settings, margins(0, 0, 0, 8));

        root.addView(text("Pacotes autorizados (allowlist exata)", 18), margins(0, 8, 0, 4));
        TextView packageHelp = text("Padrão: nenhum. Digite um package name por linha, exatamente como instalado. Não há leitura de SMS, OTP, Accessibility ou outros pacotes.", 13);
        root.addView(packageHelp, margins(0, 0, 0, 5));
        packageAllowlist = field("ex.: br.com.seubanco.app", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE, 4000);
        packageAllowlist.setMinLines(3);
        packageAllowlist.setGravity(android.view.Gravity.TOP | android.view.Gravity.START);
        root.addView(packageAllowlist, margins(0, 0, 0, 8));
        Button chooseInstalled = button("Selecionar app instalado");
        chooseInstalled.setOnClickListener(v -> chooseInstalledPackage());
        root.addView(chooseInstalled, margins(0, 0, 0, 8));
        Button savePackages = button("Salvar pacotes autorizados");
        savePackages.setOnClickListener(v -> {
            new PackageAllowlist(this).saveText(packageAllowlist.getText().toString());
            state.setText("Allowlist salva. Somente nomes exatos serão considerados.");
            refreshState();
        });
        root.addView(savePackages, margins(0, 0, 0, 12));

        state = text("Estado", 14);
        state.setTextColor(getColor(com.finfam.companion.R.color.finfam_teal));
        root.addView(state, margins(0, 0, 0, 12));

        Button disconnect = button("Desconectar neste aparelho");
        disconnect.setOnClickListener(v -> disconnectLocally());
        root.addView(disconnect, margins(0, 0, 0, 8));

        TextView privacy = text("Privacidade: o parser é local e determinístico. O servidor recebe somente evento normalizado; o texto bruto da notificação, nomes de pagadores em PIX recebido e códigos de autenticação não são enviados nem mantidos.", 13);
        privacy.setTextColor(getColor(com.finfam.companion.R.color.finfam_teal));
        root.addView(privacy, margins(0, 10, 0, 0));
        return scroll;
    }

    private void chooseInstalledPackage() {
        List<String> packages = new PackageAllowlist(this).installedLaunchablePackages(this);
        if (packages.isEmpty()) {
            state.setText("Nenhum app lançável foi encontrado. Digite o package name exato na caixa.");
            return;
        }
        Set<String> alreadySelected = new HashSet<>();
        for (String line : packageAllowlist.getText().toString().split("\\R")) {
            if (!line.trim().isEmpty()) alreadySelected.add(line.trim());
        }
        String[] labels = new String[packages.size()];
        boolean[] checked = new boolean[packages.size()];
        for (int i = 0; i < packages.size(); i++) {
            String packageName = packages.get(i);
            labels[i] = packageName;
            checked[i] = alreadySelected.contains(packageName);
        }
        new AlertDialog.Builder(this)
                .setTitle("Escolha os pacotes exatos")
                .setMultiChoiceItems(labels, checked, (dialog, which, selected) -> checked[which] = selected)
                .setNegativeButton("Cancelar", null)
                .setPositiveButton("Aplicar", (dialog, which) -> {
                    StringBuilder value = new StringBuilder();
                    for (int i = 0; i < packages.size(); i++) if (checked[i]) {
                        if (value.length() > 0) value.append('\n');
                        value.append(packages.get(i));
                    }
                    packageAllowlist.setText(value.toString());
                    new PackageAllowlist(this).saveText(value.toString());
                    refreshState();
                }).show();
    }

    private void pair() {
        String code = pairingCode.getText().toString().trim();
        String name = deviceName.getText().toString().trim();
        if (!code.matches("[0-9]{8}")) {
            state.setText("Informe exatamente 8 dígitos do código exibido na PWA.");
            return;
        }
        if (name.isEmpty() || name.length() > 80) {
            state.setText("Informe um nome de aparelho entre 1 e 80 caracteres.");
            return;
        }
        pairButton.setEnabled(false);
        executor.submit(() -> {
            ApiClient.PairResult result = new ApiClient().pair(code, name);
            runOnUiThread(() -> {
                pairButton.setEnabled(true);
                if (result.token != null) {
                    SecurePrefs prefs = new SecurePrefs(this);
                    boolean replacing = prefs.getToken() != null;
                    if (replacing) new QueueStore(this).clear();
                    prefs.putToken(result.token);
                    prefs.putDeviceId(result.deviceId);
                    prefs.putDeviceName(name);
                    state.setText("Pareado. ID do dispositivo: " + result.deviceId + ". Rascunhos continuam sujeitos à revisão na PWA.");
                    executor.submit(() -> SyncCoordinator.drainOnce(this));
                } else if (result.httpCode == 401 || result.httpCode == 403) {
                    state.setText("Código recusado ou expirado. Gere outro código na PWA.");
                } else {
                    state.setText("Não foi possível parear agora; confira a rede e tente novamente.");
                }
            });
        });
    }

    private void disconnectLocally() {
        new SecurePrefs(this).clearSession();
        state.setText("Token e fila local removidos. Para revogar o dispositivo no servidor, use a PWA FinFam.");
        refreshState();
    }

    private void openNotificationSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
        } catch (RuntimeException failure) {
            state.setText("Abra manualmente Configurações > Acesso às notificações.");
        }
    }

    private void refreshState() {
        if (state == null || packageAllowlist == null) return;
        SecurePrefs prefs = new SecurePrefs(this);
        String token = prefs.getToken();
        packageAllowlist.setText(new PackageAllowlist(this).loadText());
        state.setText("Pareamento: " + (token == null ? "não pareado" : "ativo localmente")
                + "\nAcesso Android: " + (isListenerEnabled() ? "concedido" : "não concedido")
                + "\nPacotes permitidos: " + countPackages(packageAllowlist.getText().toString())
                + "\nA fila local é limitada a " + QueueStore.MAX_ITEMS + " registros cifrados.");
    }

    private boolean isListenerEnabled() {
        String enabled = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        if (enabled == null) return false;
        String expected = new ComponentName(this, FinfamNotificationListenerService.class).flattenToString();
        for (String item : enabled.split(":")) if (expected.equals(item)) return true;
        return false;
    }

    private static int countPackages(String text) {
        int count = 0;
        for (String item : text.split("\\R")) if (!item.trim().isEmpty()) count++;
        return count;
    }

    private TextView text(String value, int size) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        return view;
    }

    private EditText field(String hint, int inputType, int maxLength) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setTextSize(16);
        field.setInputType(inputType);
        field.setSingleLine((inputType & InputType.TYPE_TEXT_FLAG_MULTI_LINE) == 0);
        field.setFilters(new InputFilter[]{new InputFilter.LengthFilter(maxLength)});
        return field;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        return button;
    }

    private LinearLayout.LayoutParams margins(int left, int top, int right, int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        params.setMargins(dp(left), dp(top), dp(right), dp(bottom));
        return params;
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    @Override protected void onResume() {
        super.onResume();
        refreshState();
    }

    @Override protected void onDestroy() {
        if (executor != null) executor.shutdownNow();
        super.onDestroy();
    }
}
