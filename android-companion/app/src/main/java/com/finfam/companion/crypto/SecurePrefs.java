package com.finfam.companion.crypto;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.GeneralSecurityException;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** SharedPreferences com valores cifrados; a chave fica no Android Keystore. */
public final class SecurePrefs {
    private static final String PREFS = "finfam_secure_state";
    private static final String KEY_ALIAS = "finfam_companion_aes_gcm_v1";
    private static final String ANDROID_KEYSTORE = "AndroidKeyStore";
    private static final String TOKEN = "token";
    private static final String DEVICE_ID = "device_id";
    private static final String DEVICE_NAME = "device_name";
    private static final String ALLOWLIST = "allowlist";
    private static final String QUEUE = "pending_queue";

    private final SharedPreferences preferences;

    public SecurePrefs(Context context) {
        preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public synchronized void putToken(String value) { put(TOKEN, value); }
    public synchronized String getToken() { return get(TOKEN); }
    public synchronized void putDeviceId(int value) { put(DEVICE_ID, Integer.toString(value)); }
    public synchronized int getDeviceId() {
        try { return Integer.parseInt(get(DEVICE_ID)); } catch (Exception ignored) { return 0; }
    }
    public synchronized void putDeviceName(String value) { put(DEVICE_NAME, value); }
    public synchronized String getDeviceName() { return get(DEVICE_NAME); }
    public synchronized void putAllowlist(String value) { put(ALLOWLIST, value == null ? "" : value); }
    public synchronized String getAllowlist() {
        String value = get(ALLOWLIST);
        return value == null ? "" : value;
    }
    public synchronized void putQueueJson(String value) { put(QUEUE, value); }
    public synchronized String getQueueJson() { return get(QUEUE); }
    public synchronized void clearQueue() { remove(QUEUE); }
    public synchronized void clearSession() {
        remove(TOKEN);
        remove(DEVICE_ID);
        remove(DEVICE_NAME);
        remove(QUEUE);
    }

    private void put(String name, String value) {
        if (value == null) {
            remove(name);
            return;
        }
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] encrypted = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
            String packed = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
                    + "." + Base64.encodeToString(encrypted, Base64.NO_WRAP);
            preferences.edit().putString(name, packed).apply();
        } catch (GeneralSecurityException failure) {
            throw new IllegalStateException("Não foi possível cifrar estado local", failure);
        }
    }

    private String get(String name) {
        String packed = preferences.getString(name, null);
        if (packed == null) return null;
        try {
            String[] pieces = packed.split("\\.", 2);
            if (pieces.length != 2) throw new GeneralSecurityException("estado inválido");
            byte[] iv = Base64.decode(pieces[0], Base64.DEFAULT);
            byte[] ciphertext = Base64.decode(pieces[1], Base64.DEFAULT);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (Exception failure) {
            // Não tenta recuperar nem registra conteúdo; estado corrompido é descartado.
            remove(name);
            return null;
        }
    }

    private void remove(String name) { preferences.edit().remove(name).apply(); }

    private SecretKey key() throws GeneralSecurityException {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        if (keyStore.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) keyStore.getEntry(KEY_ALIAS, null)).getSecretKey();
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            throw new GeneralSecurityException("Android 6+ é necessário para AES-GCM no Keystore");
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
        return generator.generateKey();
    }
}
