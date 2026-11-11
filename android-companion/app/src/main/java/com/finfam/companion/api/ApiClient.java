package com.finfam.companion.api;

import com.finfam.companion.BuildConfig;
import com.finfam.companion.parser.ParsedNotification;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;

/** Cliente que usa a URL HTTPS escolhida explicitamente ao compilar. */
public final class ApiClient {
    public static final String BASE_URL = BuildConfig.API_BASE_URL;
    private static final int CONNECT_TIMEOUT_MS = 5000;
    private static final int READ_TIMEOUT_MS = 10000;
    private static final int MAX_RETRIES = 2;

    public PairResult pair(String code, String deviceName) {
        try {
            JSONObject json = new JSONObject()
                    .put("code", code)
                    .put("device_name", deviceName);
            Response response = request("POST", "/api/notificacoes/parear", null, json.toString());
            if (response.code >= 200 && response.code < 300) {
                JSONObject result = new JSONObject(response.body);
                String token = result.getString("token");
                int deviceId = result.getInt("device_id");
                if (token.trim().isEmpty() || deviceId <= 0) throw new Exception("resposta incompleta");
                return new PairResult(token, deviceId, response.code);
            }
            return new PairResult(null, 0, response.code);
        } catch (Exception failure) {
            return new PairResult(null, 0, -1);
        }
    }

    public ReceiveResult receive(String token, ParsedNotification event) {
        try {
            JSONObject json = new JSONObject()
                    .put("event_id", event.getEventId())
                    .put("source_package", event.getSourcePackage())
                    .put("occurred_at", event.getOccurredAt())
                    .put("amount", event.getAmount())
                    .put("merchant", event.getMerchant())
                    .put("type_hint", event.getTypeHint())
                    .put("installment_count", event.getInstallmentCount() == null
                            ? JSONObject.NULL : event.getInstallmentCount());
            Response response = request("POST", "/api/notificacoes/receber", token, json.toString());
            boolean accepted = response.code >= 200 && response.code < 300;
            if (accepted) {
                JSONObject result = new JSONObject(response.body);
                int draftId = result.optInt("draft_id", 0);
                String status = result.optString("status", "");
                if (draftId <= 0 || status.trim().isEmpty()) {
                    return new ReceiveResult(false, response.code, 0, "");
                }
                return new ReceiveResult(true, response.code, draftId, status);
            }
            return new ReceiveResult(false, response.code, 0, "");
        } catch (Exception failure) {
            return new ReceiveResult(false, -1, 0, "");
        }
    }

    public StatusResult status(String token) {
        try {
            Response response = request("GET", "/api/notificacoes/status", token, null);
            if (response.code >= 200 && response.code < 300) {
                JSONObject result = new JSONObject(response.body);
                return new StatusResult(result.optBoolean("active", false), result.optInt("device_id", 0), response.code);
            }
            return new StatusResult(false, 0, response.code);
        } catch (Exception failure) {
            return new StatusResult(false, 0, -1);
        }
    }

    private Response request(String method, String path, String token, String body) throws Exception {
        if (!BASE_URL.startsWith("https://") || BASE_URL.contains("example.invalid")
                || path == null || !path.startsWith("/")) {
            throw new IllegalArgumentException("endpoint inseguro");
        }
        Exception last = null;
        for (int attempt = 0; attempt <= MAX_RETRIES; attempt++) {
            HttpURLConnection connection = null;
            try {
                URL url = new URL(BASE_URL + path);
                connection = (HttpURLConnection) url.openConnection();
                connection.setRequestMethod(method);
                connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
                connection.setReadTimeout(READ_TIMEOUT_MS);
                connection.setUseCaches(false);
                connection.setRequestProperty("Accept", "application/json");
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                if (token != null && !token.isEmpty()) connection.setRequestProperty("Authorization", "Bearer " + token);
                if (body != null) {
                    connection.setDoOutput(true);
                    byte[] payload = body.getBytes(StandardCharsets.UTF_8);
                    try (OutputStream output = connection.getOutputStream()) { output.write(payload); }
                }
                int code = connection.getResponseCode();
                String responseBody = read(code >= 400 ? connection.getErrorStream() : connection.getInputStream());
                if (code >= 500 && attempt < MAX_RETRIES) {
                    sleepBackoff(attempt);
                    continue;
                }
                return new Response(code, responseBody);
            } catch (Exception failure) {
                last = failure;
                if (attempt < MAX_RETRIES) sleepBackoff(attempt);
            } finally {
                if (connection != null) connection.disconnect();
            }
        }
        throw last == null ? new Exception("rede indisponível") : last;
    }

    private static String read(InputStream stream) throws Exception {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line);
        }
        return result.toString();
    }

    private static void sleepBackoff(int attempt) {
        try { TimeUnit.MILLISECONDS.sleep(250L * (attempt + 1)); }
        catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
    }

    private static final class Response {
        final int code; final String body;
        Response(int code, String body) { this.code = code; this.body = body; }
    }

    public static final class PairResult {
        public final String token; public final int deviceId; public final int httpCode;
        PairResult(String token, int deviceId, int httpCode) { this.token = token; this.deviceId = deviceId; this.httpCode = httpCode; }
    }
    public static final class ReceiveResult {
        public final boolean accepted; public final int httpCode; public final int draftId; public final String status;
        ReceiveResult(boolean accepted, int httpCode, int draftId, String status) { this.accepted = accepted; this.httpCode = httpCode; this.draftId = draftId; this.status = status; }
    }
    public static final class StatusResult {
        public final boolean active; public final int deviceId; public final int httpCode;
        StatusResult(boolean active, int deviceId, int httpCode) { this.active = active; this.deviceId = deviceId; this.httpCode = httpCode; }
    }
}
