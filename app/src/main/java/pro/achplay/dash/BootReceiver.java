package pro.achplay.dash;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/** Поднимает дашборд после загрузки планшета и после обновления приложения. */
public class BootReceiver extends BroadcastReceiver {

    private static final String TAG = "KioskBoot";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || intent.getAction() == null) return;
        Log.i(TAG, "boot event: " + intent.getAction());
        Intent launch = new Intent(context, MainActivity.class);
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 10+: старт активити из BOOT_COMPLETED разрешён.
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        }
        try {
            context.startActivity(launch);
        } catch (Exception e) {
            Log.w(TAG, "cannot start activity: " + e.getMessage());
        }
    }
}
