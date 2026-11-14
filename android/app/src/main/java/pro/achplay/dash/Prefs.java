package pro.achplay.dash;

import android.content.Context;
import android.content.SharedPreferences;

/** Хранение адреса дашборда. */
public final class Prefs {

    private static final String FILE = "kiosk";
    private static final String KEY_URL = "url";

    private Prefs() {
    }

    public static String url(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE);
        String value = sp.getString(KEY_URL, "");
        return value == null ? "" : value.trim();
    }

    public static void setUrl(Context ctx, String url) {
        String normalized = normalize(url);
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
                .edit().putString(KEY_URL, normalized).apply();
    }

    public static String normalize(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) return "";
        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            value = "http://" + value;
        }
        if (!value.endsWith("/")) {
            value = value + "/";
        }
        return value;
    }
}
