package pro.achplay.dash;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.URL;

/** Экран настроек: адрес дашборда, проверка доступности, демо-стенд. */
public class SettingsActivity extends Activity {

    private static final String DEMO_URL = "https://demodash.shkredovsv.ru/";
    private EditText input;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);
        root.setBackgroundColor(Color.parseColor("#070b10"));

        TextView title = new TextView(this);
        title.setText("Адрес дашборда");
        title.setTextColor(Color.parseColor("#e9f0f5"));
        title.setTextSize(22);
        root.addView(title);

        TextView hint = new TextView(this);
        hint.setText("Например http://192.168.1.10:8090/ — адрес машины, где запущен сервер дашборда.");
        hint.setTextColor(Color.parseColor("#7d93a6"));
        hint.setTextSize(13);
        hint.setPadding(0, pad / 2, 0, pad);
        root.addView(hint);

        input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(Prefs.url(this));
        input.setHint("http://192.168.1.10:8090/");
        input.setTextColor(Color.parseColor("#e9f0f5"));
        input.setHintTextColor(Color.parseColor("#54697a"));
        root.addView(input);

        Button save = new Button(this);
        save.setText("Сохранить и открыть");
        save.setOnClickListener(v -> {
            String url = Prefs.normalize(input.getText().toString());
            if (url.isEmpty()) {
                Toast.makeText(this, "Введите адрес", Toast.LENGTH_SHORT).show();
                return;
            }
            Prefs.setUrl(this, url);
            finish();
        });
        root.addView(save, buttonParams());

        Button check = new Button(this);
        check.setText("Проверить связь");
        check.setOnClickListener(v -> checkAsync(Prefs.normalize(input.getText().toString())));
        root.addView(check, buttonParams());

        Button demo = new Button(this);
        demo.setText("Открыть демо-стенд");
        demo.setOnClickListener(v -> {
            input.setText(DEMO_URL);
            Prefs.setUrl(this, DEMO_URL);
            finish();
        });
        root.addView(demo, buttonParams());

        TextView foot = new TextView(this);
        foot.setText("В киоске настройки открываются долгим нажатием по экрану.");
        foot.setTextColor(Color.parseColor("#54697a"));
        foot.setTextSize(12);
        foot.setPadding(0, pad, 0, 0);
        foot.setGravity(Gravity.START);
        root.addView(foot);

        setContentView(root);
        getWindow().setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    }

    private LinearLayout.LayoutParams buttonParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (10 * getResources().getDisplayMetrics().density);
        return lp;
    }

    private void checkAsync(String url) {
        if (url.isEmpty()) {
            Toast.makeText(this, "Введите адрес", Toast.LENGTH_SHORT).show();
            return;
        }
        Toast.makeText(this, "Проверяю…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            String result;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(4000);
                c.setReadTimeout(4000);
                c.setRequestMethod("GET");
                int code = c.getResponseCode();
                result = (code >= 200 && code < 400) ? "Доступен (HTTP " + code + ")" : "Ответ HTTP " + code;
                c.disconnect();
            } catch (Exception e) {
                result = "Недоступен: " + e.getClass().getSimpleName();
            }
            final String msg = result;
            handler.post(() -> Toast.makeText(this, msg, Toast.LENGTH_LONG).show());
        }).start();
    }
}
