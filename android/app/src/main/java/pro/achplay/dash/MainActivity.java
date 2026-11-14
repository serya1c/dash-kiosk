package pro.achplay.dash;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * Киоск-приложение: открывает дашборд на весь экран, не даёт экрану гаснуть,
 * переподключается при потере сети. Адрес берётся из настроек (Prefs),
 * долгое нажатие по экрану открывает экран настроек.
 */
public class MainActivity extends Activity {

    private static final long RETRY_MS = 5000L;
    private static final long RELOAD_MS = 6L * 60L * 60L * 1000L;

    private WebView web;
    private String dashUrl;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean offlineShown = false;

    private final Runnable retry = new Runnable() {
        @Override
        public void run() {
            if (web == null) return;
            offlineShown = false;
            web.loadUrl(dashUrl);
        }
    };

    private final Runnable periodicReload = new Runnable() {
        @Override
        public void run() {
            if (web != null && !offlineShown) {
                web.reload();
            }
            handler.postDelayed(this, RELOAD_MS);
        }
    };

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        dashUrl = Prefs.url(this);
        if (dashUrl.isEmpty()) {
            startActivity(new Intent(this, SettingsActivity.class));
            finish();
            return;
        }

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#070b10"));

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " KioskDash/1.1");

        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                offlineShown = false;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showOffline();
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                showOffline();
            }
        });

        web.setOnLongClickListener(new View.OnLongClickListener() {
            @Override
            public boolean onLongClick(View v) {
                startActivity(new Intent(MainActivity.this, SettingsActivity.class));
                return true;
            }
        });

        setContentView(web);
        goFullscreen();
        web.loadUrl(dashUrl);
        handler.postDelayed(periodicReload, RELOAD_MS);
    }

    private void showOffline() {
        if (offlineShown || web == null) return;
        offlineShown = true;
        String html = "<!doctype html><html><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
                + "<body style='margin:0;height:100vh;background:#070b10;color:#7d93a6;"
                + "font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;text-align:center'>"
                + "<div><div style='font-size:22px;color:#e9f0f5'>Нет связи с дашбордом</div>"
                + "<div style='font-size:14px;margin-top:10px'>" + dashUrl + " · повтор каждые 5 секунд</div>"
                + "<div style='font-size:13px;margin-top:18px;color:#4a5f70'>долгое нажатие по экрану — настройки</div></div>"
                + "</body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        handler.removeCallbacks(retry);
        handler.postDelayed(retry, RETRY_MS);
    }

    private void goFullscreen() {
        View decor = getWindow().getDecorView();
        decor.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) goFullscreen();
    }

    @Override
    protected void onResume() {
        super.onResume();
        goFullscreen();
        if (web == null) return;
        String current = Prefs.url(this);
        if (!current.isEmpty() && !current.equals(dashUrl)) {
            dashUrl = current;
            offlineShown = false;
            web.loadUrl(dashUrl);
            return;
        }
        web.onResume();
        if (offlineShown) {
            handler.removeCallbacks(retry);
            handler.postDelayed(retry, 800);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) web.onPause();
    }

    @Override
    public void onBackPressed() {
        // Киоск: «назад» не выходит из дашборда.
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
