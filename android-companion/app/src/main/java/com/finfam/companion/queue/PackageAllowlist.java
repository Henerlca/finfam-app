package com.finfam.companion.queue;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ResolveInfo;

import com.finfam.companion.crypto.SecurePrefs;

import java.util.HashSet;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Allowlist explicitamente vazia por padrão; nunca usa prefixos ou curingas. */
public final class PackageAllowlist {
    private final SecurePrefs prefs;

    public PackageAllowlist(Context context) { prefs = new SecurePrefs(context.getApplicationContext()); }

    public synchronized void saveText(String text) {
        StringBuilder normalized = new StringBuilder();
        for (String line : (text == null ? "" : text).split("[\\r\\n,]+")) {
            String item = line.trim().toLowerCase(Locale.ROOT);
            if (item.matches("[a-z][a-z0-9_]*(?:\\.[a-z0-9_]+)+")) {
                if (normalized.length() > 0) normalized.append('\n');
                normalized.append(item);
            }
        }
        prefs.putAllowlist(normalized.toString());
    }

    public synchronized String loadText() { return prefs.getAllowlist(); }

    public synchronized boolean isAllowed(String packageName) {
        if (packageName == null) return false;
        Set<String> allowed = new HashSet<>();
        for (String line : loadText().split("\\R")) if (!line.trim().isEmpty()) allowed.add(line.trim());
        return allowed.contains(packageName.toLowerCase(Locale.ROOT));
    }

    /** Apps com launcher visível; a escolha final continua sendo feita pelo usuário na UI. */
    public List<String> installedLaunchablePackages(Context context) {
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<String> result = new ArrayList<>();
        for (ResolveInfo info : context.getPackageManager().queryIntentActivities(launcher, 0)) {
            String packageName = info.activityInfo == null ? "" : info.activityInfo.packageName;
            if (!packageName.isEmpty() && !context.getPackageName().equals(packageName)
                    && !result.contains(packageName)) result.add(packageName);
        }
        Collections.sort(result);
        return result;
    }
}
