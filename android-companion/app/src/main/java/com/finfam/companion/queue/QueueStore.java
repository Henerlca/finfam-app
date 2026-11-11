package com.finfam.companion.queue;

import android.content.Context;
import com.finfam.companion.crypto.SecurePrefs;
import com.finfam.companion.parser.ParsedNotification;
import org.json.JSONArray;
import org.json.JSONObject;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;

/** Fila offline cifrada: nunca guarda texto bruto; um lock atende todas as instâncias. */
public final class QueueStore {
    public static final int MAX_ITEMS = 50;
    private static final Object QUEUE_LOCK = new Object();
    private final SecurePrefs prefs;

    public QueueStore(Context context) { prefs = new SecurePrefs(context.getApplicationContext()); }

    public void append(ParsedNotification event) {
        synchronized (QUEUE_LOCK) {
            JSONArray current = read();
            for (int i = 0; i < current.length(); i++) {
                JSONObject item = current.optJSONObject(i);
                if (item != null && event.getEventId().equals(item.optString("event_id"))) return;
            }
            while (current.length() >= MAX_ITEMS) current.remove(0);
            current.put(toJson(event));
            prefs.putQueueJson(current.toString());
        }
    }

    public List<ParsedNotification> snapshot() {
        synchronized (QUEUE_LOCK) {
            JSONArray current = read();
            List<ParsedNotification> result = new ArrayList<>();
            for (int i = 0; i < current.length(); i++) {
                ParsedNotification event = fromJson(current.optJSONObject(i));
                if (event != null) result.add(event);
            }
            return result;
        }
    }

    public void removeEvent(String eventId) {
        synchronized (QUEUE_LOCK) {
            JSONArray current = read();
            JSONArray kept = new JSONArray();
            for (int i = 0; i < current.length(); i++) {
                JSONObject item = current.optJSONObject(i);
                if (item != null && !eventId.equals(item.optString("event_id"))) kept.put(item);
            }
            if (kept.length() == 0) prefs.clearQueue(); else prefs.putQueueJson(kept.toString());
        }
    }

    public void clear() { synchronized (QUEUE_LOCK) { prefs.clearQueue(); } }

    private JSONArray read() {
        String value = prefs.getQueueJson();
        if (value == null || value.trim().isEmpty()) return new JSONArray();
        try { return new JSONArray(value); } catch (Exception invalid) {
            prefs.clearQueue();
            return new JSONArray();
        }
    }

    private static JSONObject toJson(ParsedNotification event) {
        JSONObject json = new JSONObject();
        try {
            json.put("event_id", event.getEventId());
            json.put("source_package", event.getSourcePackage());
            json.put("occurred_at", event.getOccurredAt());
            json.put("amount", event.getAmount());
            json.put("merchant", event.getMerchant());
            json.put("type_hint", event.getTypeHint());
            if (event.getInstallmentCount() == null) json.put("installment_count", JSONObject.NULL);
            else json.put("installment_count", event.getInstallmentCount());
        } catch (Exception impossible) {
            throw new IllegalStateException("evento normalizado inválido", impossible);
        }
        return json;
    }

    private static ParsedNotification fromJson(JSONObject json) {
        if (json == null) return null;
        try {
            String eventId = json.getString("event_id");
            String sourcePackage = json.getString("source_package");
            String occurredAt = json.getString("occurred_at");
            BigDecimal amount = new BigDecimal(json.get("amount").toString());
            String merchant = json.optString("merchant", "");
            String typeHint = json.getString("type_hint");
            Integer count = json.isNull("installment_count") ? null : json.getInt("installment_count");
            return new ParsedNotification(eventId, sourcePackage, occurredAt, amount, merchant, typeHint, count);
        } catch (Exception invalid) { return null; }
    }
}
