package com.nativecam.bridge;

import android.app.Activity;
import android.content.Intent;
import android.content.res.Configuration;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Drawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * 手机端控制界面: 拍摄画面预览、点击对焦/测光、EV/ISO 滑动条、分辨率/画质、重启/停止。
 *
 * - 预览: 后台线程轮询本机 /frame, 降采样显示 (与 PC 端共用同一相机流);
 *   点击画面 => /setcam?region=x,y (归一化) + AF 触发, 与原生相机"点按对焦"等价。
 * - 横屏真适配: 横屏时双栏布局 (左=大预览, 右=固定宽控制列), 竖屏时单列;
 *   旋转经 onConfigurationChanged 重建布局, 滑条/输入状态保留。
 * - 控制走本机 HTTP 服务 (127.0.0.1:8888), 与 CameraService 解耦。
 */
public class MainActivity extends Activity {
    private static final String TAG = "NativeCamBridge";
    private static final String BASE = "http://127.0.0.1:8888";

    private TextView statusText;
    private ImageView previewView;
    private Spinner sizeSpinner;
    private ArrayAdapter<String> sizeAdapter;
    private EditText qualityEdit;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final List<String> sizes = new ArrayList<>();
    private volatile boolean uiRunning = true;
    // 当前显示的 (降采样) bitmap 尺寸, 用于点击坐标 -> 归一化换算
    private volatile int bmpW = 0, bmpH = 0;

    // 曝光微调滑动条 (默认全自动; 拖动即实时下发 /setcam)
    private SeekBar evBar;
    private TextView evValue;
    private SeekBar isoBar;
    private TextView isoValue;
    private LinearLayout isoRow;
    private TextView isoLabel;
    private int evBarMin = -6;
    private int evBarMaxSaved = 6;
    private int isoMinCur = 100, isoMaxCur = 6400;
    private float focusMaxCur = 20f;  // 最大屈光度（从设备读取后更新）
    private volatile boolean rangesReady = false;
    private boolean isoSupported = false;
    private long lastSeekSentMs = 0;

    // 手动对焦距离滑动条 (AF=OFF, focus=屈光度)
    private SeekBar focusBar;
    private TextView focusValue;
    private LinearLayout focusRow;
    private TextView focusLabel;
    private int focusProgressSaved = 0;  // 0=自动, 1..200=手动 (0.1~20.0 屈光度)

    // 摄像头选择 (来自 /cameras): [id, label]
    private final List<String[]> camerasCache = new ArrayList<>();
    private LinearLayout camBtns;
    private volatile String selectedCameraId = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);   // 去掉标题栏 (UI 内已有标题)
        buildUi();
        loadCameras();

        // 确保服务在跑
        Intent i = new Intent(this, CameraService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(i);
        } else {
            startService(i);
        }

        loadSizes();
        startStatusRefresh();
        startPreview();
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        // 旋转时重建布局 (Manifest 已声明 configChanges, 不会走 onCreate)
        buildUi();
    }

    // ================================================================ UI 构建
    private void buildUi() {
        // 重建前保留用户输入/滑条状态
        String qualityText = qualityEdit != null ? qualityEdit.getText().toString() : "95";
        int evProgress = evBar != null ? evBar.getProgress() : (0 - evBarMin);
        int isoProgress = isoBar != null ? isoBar.getProgress() : 0;
        focusProgressSaved = focusBar != null ? focusBar.getProgress() : 0;

        boolean landscape = getResources().getConfiguration().orientation
                == Configuration.ORIENTATION_LANDSCAPE;

        if (landscape) {
            // ── 横屏: 左 = 大预览(全高), 右 = 固定宽控制列 ──
            LinearLayout root = new LinearLayout(this);
            root.setOrientation(LinearLayout.HORIZONTAL);
            root.setBackgroundColor(0xFF1E1E1E);

            ImageView pv = createPreview();
            LinearLayout.LayoutParams plp =
                    new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 1f);
            root.addView(pv, plp);

            ScrollView right = new ScrollView(this);
            right.setFillViewport(true);
            right.setBackgroundColor(0xFF1E1E1E);
            LinearLayout col = new LinearLayout(this);
            col.setOrientation(LinearLayout.VERTICAL);
            right.addView(col, new ScrollView.LayoutParams(
                    ScrollView.LayoutParams.MATCH_PARENT,
                    ScrollView.LayoutParams.WRAP_CONTENT));
            createControls(col, qualityText);
            restoreSliderState(evProgress, isoProgress);

            float density = getResources().getDisplayMetrics().density;
            LinearLayout.LayoutParams rlp =
                    new LinearLayout.LayoutParams((int) (350 * density),
                            LinearLayout.LayoutParams.MATCH_PARENT);
            root.addView(right, rlp);
            setContentView(root);
        } else {
            // ── 竖屏: 单列 (预览占剩余高度, 其余滚动) ──
            ScrollView scroll = new ScrollView(this);
            scroll.setFillViewport(true);
            scroll.setBackgroundColor(0xFF1E1E1E);
            LinearLayout root = new LinearLayout(this);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setPadding(32, 24, 32, 24);

            ImageView pv = createPreview();
            LinearLayout.LayoutParams plp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.2f);
            plp.setMargins(0, 12, 0, 12);
            root.addView(pv, plp);

            createControls(root, qualityText);
            restoreSliderState(evProgress, isoProgress);

            scroll.addView(root, new ScrollView.LayoutParams(
                    ScrollView.LayoutParams.MATCH_PARENT,
                    ScrollView.LayoutParams.WRAP_CONTENT));
            setContentView(scroll);
        }
        applyChrome();
    }

    /** 横屏时隐藏状态栏/导航栏进入沉浸全屏, 最大化有效显示面积。 */
    private void applyChrome() {
        boolean landscape = getResources().getConfiguration().orientation
                == Configuration.ORIENTATION_LANDSCAPE;
        if (landscape) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
        }
    }

    /** 创建预览控件 (点击 = 对焦/测光)。 */
    private ImageView createPreview() {
        previewView = new ImageView(this);
        previewView.setBackgroundColor(0xFF000000);
        previewView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        previewView.setOnTouchListener((v, ev) -> {
            if (ev.getAction() != MotionEvent.ACTION_DOWN) return false;
            handlePreviewTap(ev);
            return true;
        });
        return previewView;
    }

    /** 创建控制列的全部控件 (竖屏在预览下方, 横屏在右侧栏)。 */
    private void createControls(LinearLayout col, String qualityText) {
        col.setPadding(28, 20, 28, 20);

        TextView title = new TextView(this);
        title.setText("NativeCam 控制");
        title.setTextSize(18f);
        title.setTextColor(0xFF4EC9B0);
        col.addView(title);

        statusText = new TextView(this);
        statusText.setText("正在连接服务 ...");
        statusText.setTextSize(12f);
        statusText.setTextColor(0xFFDDDDDD);
        statusText.setPadding(0, 10, 0, 10);
        col.addView(statusText);

        TextView hint = new TextView(this);
        hint.setText("点击画面任意位置 = 对焦/测光到该处");
        hint.setTextSize(11f);
        hint.setTextColor(0xFF808080);
        col.addView(hint);

        col.addView(makeButton("恢复全自动 (曝光/对焦/测光)", v -> {
            doGet("/setcam?af=auto&region=clear");
            doGet("/setcam?ae=auto&ev=auto&iso=auto");
            resetSliders();
        }));

        // -------- 摄像头选择 --------
        TextView camLabel = new TextView(this);
        camLabel.setText("摄像头 (切换后相机会重启几秒):");
        camLabel.setTextSize(11f);
        camLabel.setTextColor(0xFF9CDCFE);
        camLabel.setPadding(0, 14, 0, 0);
        col.addView(camLabel);

        camBtns = new LinearLayout(this);
        camBtns.setOrientation(LinearLayout.VERTICAL);
        col.addView(camBtns);
        if (camerasCache.isEmpty()) {
            TextView loading = new TextView(this);
            loading.setText("枚举摄像头中...");
            loading.setTextColor(0xFF808080);
            loading.setTextSize(11f);
            camBtns.addView(loading);
        } else {
            refreshCamButtons();
        }

        // -------- 曝光微调: EV / ISO 滑动条 --------
        TextView expLabel = new TextView(this);
        expLabel.setText("EV 曝光补偿 (0 = 自动基准, 过曝往左拉)");
        expLabel.setTextSize(11f);
        expLabel.setTextColor(0xFF9CDCFE);
        expLabel.setPadding(0, 14, 0, 0);
        col.addView(expLabel);

        LinearLayout evRow = new LinearLayout(this);
        evRow.setOrientation(LinearLayout.HORIZONTAL);
        evRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        evBar = new SeekBar(this);
        evBar.setMax(Math.max(1, evBarMaxSaved - evBarMin));
        LinearLayout.LayoutParams evlp =
                new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        evRow.addView(evBar, evlp);
        evValue = new TextView(this);
        evValue.setTextColor(0xFFFFFFFF);
        evValue.setPadding(14, 0, 0, 0);
        evRow.addView(evValue, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        col.addView(evRow);

        isoLabel = new TextView(this);
        isoLabel.setText("ISO (拖动即切换手动曝光, 快门沿用自动值)");
        isoLabel.setTextSize(11f);
        isoLabel.setTextColor(0xFF9CDCFE);
        isoLabel.setPadding(0, 10, 0, 0);
        col.addView(isoLabel);

        isoRow = new LinearLayout(this);
        isoRow.setOrientation(LinearLayout.HORIZONTAL);
        isoRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        isoBar = new SeekBar(this);
        isoBar.setMax(100);
        LinearLayout.LayoutParams isolp =
                new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        isoRow.addView(isoBar, isolp);
        isoValue = new TextView(this);
        isoValue.setTextColor(0xFFFFFFFF);
        isoValue.setPadding(14, 0, 0, 0);
        isoRow.addView(isoValue, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        col.addView(isoRow);

        evBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                if (!fromUser) return;
                int ev = progress + evBarMin;
                evValue.setText(String.valueOf(ev));
                throttleSend("/setcam?ev=" + ev);
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
                doGet("/setcam?ev=" + (sb.getProgress() + evBarMin));
            }
        });

        isoBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                if (!fromUser) return;
                int iso = progressToIso(progress);
                isoValue.setText(String.valueOf(iso));
                throttleSend("/setcam?iso=" + iso);
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
                doGet("/setcam?iso=" + progressToIso(sb.getProgress()));
            }
        });

        // -------- 手动对焦距离 --------
        focusLabel = new TextView(this);
        focusLabel.setText("对焦距离 (最左=自动对焦, 右拉=手动近距离)");
        focusLabel.setTextSize(11f);
        focusLabel.setTextColor(0xFF9CDCFE);
        focusLabel.setPadding(0, 14, 0, 0);
        col.addView(focusLabel);

        focusRow = new LinearLayout(this);
        focusRow.setOrientation(LinearLayout.HORIZONTAL);
        focusRow.setGravity(android.view.Gravity.CENTER_VERTICAL);
        focusBar = new SeekBar(this);
        focusBar.setMax(200);  // 0=自动, 1..200 => 0.1~20.0 屈光度
        LinearLayout.LayoutParams focuslp =
                new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        focusRow.addView(focusBar, focuslp);
        focusValue = new TextView(this);
        focusValue.setTextColor(0xFFFFFFFF);
        focusValue.setPadding(14, 0, 0, 0);
        focusValue.setText("自动");
        focusRow.addView(focusValue, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        col.addView(focusRow);

        focusBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                if (!fromUser) return;
                if (progress == 0) {
                    focusValue.setText("自动");
                    throttleSend("/setcam?af=auto");
                } else {
                    float diopter = progressToDiopter(progress);
                    float distCm = 100f / diopter;
                    focusValue.setText(String.format("%.1fD (%.0fcm)", diopter, distCm));
                    throttleSend("/setcam?focus=" + diopter);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
                int p = sb.getProgress();
                if (p == 0) {
                    doGet("/setcam?af=auto");
                } else {
                    doGet("/setcam?focus=" + progressToDiopter(p));
                }
            }
        });

        // -------- 分辨率 --------
        TextView sizeLabel = new TextView(this);
        sizeLabel.setText("分辨率 (应用后立即生效):");
        sizeLabel.setTextSize(11f);
        sizeLabel.setTextColor(0xFF9CDCFE);
        sizeLabel.setPadding(0, 14, 0, 0);
        col.addView(sizeLabel);

        sizeSpinner = new Spinner(this);
        if (sizeAdapter == null) {
            sizeAdapter = new ArrayAdapter<>(this,
                    android.R.layout.simple_spinner_dropdown_item, sizes);
        }
        sizeSpinner.setAdapter(sizeAdapter);
        col.addView(sizeSpinner);

        col.addView(makeButton("应用该分辨率", v -> {
            Object sel = sizeSpinner.getSelectedItem();
            if (sel != null) {
                doGet("/setres?size=" + sel.toString());
            }
        }));

        col.addView(makeButton("恢复最高分辨率 (自动)", v -> doGet("/setres?auto=1")));

        // -------- 画质 --------
        TextView qLabel = new TextView(this);
        qLabel.setText("JPEG 画质 (1-100):");
        qLabel.setTextSize(11f);
        qLabel.setTextColor(0xFF9CDCFE);
        qLabel.setPadding(0, 12, 0, 0);
        col.addView(qLabel);

        qualityEdit = new EditText(this);
        qualityEdit.setText(qualityText);
        qualityEdit.setTextColor(0xFFDDDDDD);
        qualityEdit.setSingleLine(true);
        col.addView(qualityEdit);

        col.addView(makeButton("应用画质", v ->
                doGet("/setquality?q=" + qualityEdit.getText().toString().trim())));

        col.addView(makeButton("重启相机", v -> doGet("/restart")));

        col.addView(makeButton("停止服务并退出", v -> {
            doGet("/stop");
            Toast.makeText(this, "服务已停止", Toast.LENGTH_SHORT).show();
            finish();
        }));

        // 滑条可见性 (设备不支持手动 ISO 时隐藏)
        applySliderVisibility();
        if (!rangesReady) {
            evBar.setProgress(0 - evBarMin);
            evValue.setText("0");
        }
    }

    /** 旋转重建后恢复滑条位置 (buildUi 里捕获的快照, 控件已创建)。 */
    private void restoreSliderState(int evProgress, int isoProgress) {
        if (evBar != null) {
            evBar.setProgress(evProgress);
            evValue.setText(String.valueOf(evProgress + evBarMin));
        }
        if (isoBar != null) {
            isoBar.setProgress(isoProgress);
            isoValue.setText(String.valueOf(progressToIso(isoProgress)));
        }
        if (focusBar != null) {
            focusBar.setProgress(focusProgressSaved);
            focusValue.setText(focusProgressSaved == 0 ? "自动" :
                    String.format("%.1fD (%.0fcm)", progressToDiopter(focusProgressSaved),
                            100f / progressToDiopter(focusProgressSaved)));
        }
    }

    // ================================================================ 交互逻辑
    /** 点击预览画面 -> 归一化坐标 -> /setcam?region (对焦+测光到该处)。 */
    private void handlePreviewTap(MotionEvent ev) {
        int w = bmpW, h = bmpH;
        Drawable d = previewView.getDrawable();
        int vw = previewView.getWidth(), vh = previewView.getHeight();
        if (w <= 0 || h <= 0 || d == null || vw <= 0 || vh <= 0) {
            Toast.makeText(this, "画面尚未就绪", Toast.LENGTH_SHORT).show();
            return;
        }
        // FIT_CENTER 缩放换算: 显示坐标 -> bitmap 坐标
        float dw = d.getIntrinsicWidth(), dh = d.getIntrinsicHeight();
        float scale = Math.min(vw / dw, vh / dh);
        float dx = (vw - dw * scale) / 2f;
        float dy = (vh - dh * scale) / 2f;
        float ix = (ev.getX() - dx) / scale;
        float iy = (ev.getY() - dy) / scale;
        float nx = Math.max(0f, Math.min(1f, ix / w));
        float ny = Math.max(0f, Math.min(1f, iy / h));
        nx = Math.min(nx, 0.98f);
        ny = Math.min(ny, 0.98f);
        Toast.makeText(this,
                String.format("对焦/测光: (%.0f%%, %.0f%%)", nx * 100, ny * 100),
                Toast.LENGTH_SHORT).show();
        doGet(String.format("/setcam?region=%.4f,%.4f,0.20,0.20", nx, ny));
    }

    /** 轮询 /frame 降采样显示 (约 6-8 fps, 仅用于取景, 不影响 PC 端取流)。 */
    private void startPreview() {
        new Thread(() -> {
            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inSampleSize = 4;   // 大图降到 1/4, 避免解码大 JPEG 卡 UI
            while (uiRunning) {
                try {
                    byte[] jpg = httpGetBytes(BASE + "/frame");
                    Bitmap bmp = BitmapFactory.decodeByteArray(jpg, 0, jpg.length, opts);
                    if (bmp != null) {
                        bmpW = bmp.getWidth();
                        bmpH = bmp.getHeight();
                        ui.post(() -> {
                            if (uiRunning && previewView != null) {
                                previewView.setImageBitmap(bmp);
                            }
                        });
                    }
                } catch (Exception ignored) {
                }
                try {
                    Thread.sleep(120);
                } catch (InterruptedException e) {
                    break;
                }
            }
        }).start();
    }

    private Button makeButton(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextSize(12f);
        b.setOnClickListener(l);
        return b;
    }

    /** 拖动时节流发送 (250ms), 松手时 onStopTrackingTouch 会发最终值。 */
    private void throttleSend(String path) {
        long now = System.currentTimeMillis();
        if (now - lastSeekSentMs < 250) return;
        lastSeekSentMs = now;
        doGet(path);
    }

    /** ISO 对数映射: progress 0..100 -> isoMin..isoMax, 按 50 取整。 */
    private int progressToIso(int p) {
        double lo = Math.max(50, isoMinCur);
        double hi = Math.max(lo + 1, isoMaxCur);
        int iso = (int) Math.round(lo * Math.pow(hi / lo, p / 100.0));
        return Math.max(50, Math.round(iso / 50f) * 50);
    }

    /** 对焦距离映射: progress 1..200 -> 屈光度 (指数映射, 范围由设备实际能力决定)。 */
    private float progressToDiopter(int p) {
        float fmax = Math.max(1f, focusMaxCur);
        return (float) (0.5 * Math.pow(fmax / 0.5, p / 200.0));
    }

    /** 屈光度 -> progress (progressToDiopter 的反函数)。 */
    private int diopterToProgress(float diopter) {
        float fmax = Math.max(1f, focusMaxCur);
        if (diopter <= 0.5f) return 1;
        if (diopter >= fmax) return 200;
        return Math.max(1, Math.min(200,
                (int) Math.round(200.0 * Math.log(diopter / 0.5) / Math.log(fmax / 0.5))));
    }

    /** 滑条可见性: 设备不支持手动 ISO 时隐藏 ISO 条。 */
    private void applySliderVisibility() {
        boolean showIso = rangesReady && isoSupported;
        if (isoRow != null) isoRow.setVisibility(showIso ? View.VISIBLE : View.GONE);
        if (isoLabel != null) isoLabel.setVisibility(showIso ? View.VISIBLE : View.GONE);
    }

    /** 从 /info 的 cam 里取 EV/ISO 范围, 配置滑动条 (只执行一次)。 */
    private void applyRanges(JSONObject cam) {
        if (cam == null) return;
        // 更新对焦距离范围（设备实际能力）
        float fmax = (float) cam.optDouble("focusMax", 0);
        if (fmax > 0) focusMaxCur = fmax;
        // 同步对焦滑条位置 (服务端可能被 PC 端或其它方式修改)
        float fd = (float) cam.optDouble("focusDist", -1);
        if (focusBar != null && !focusBar.isPressed()) {
            int newProg = fd > 0 ? diopterToProgress(fd) : 0;
            if (Math.abs(newProg - focusBar.getProgress()) > 1) {
                ui.post(() -> {
                    focusBar.setProgress(newProg);
                    focusValue.setText(newProg == 0 ? "自动" :
                            String.format("%.1fD (%.0fcm)", fd, 100f / fd));
                });
            }
        }
        if (rangesReady) return;
        rangesReady = true;
        final int em = cam.optInt("evMin", -6);
        final int ex = cam.optInt("evMax", 6);
        final int imin = cam.optInt("isoMin", -1);
        final int imax = cam.optInt("isoMax", -1);
        ui.post(() -> {
            evBarMin = Math.min(em, 0);
            evBarMaxSaved = Math.max(ex, evBarMin + 1);
            isoMinCur = Math.max(50, imin);
            isoMaxCur = Math.max(isoMinCur + 1, imax);
            isoSupported = imin > 0 && imax > imin;
            if (evBar != null) {
                evBar.setMax(Math.max(1, evBarMaxSaved - evBarMin));
                evBar.setProgress(0 - evBarMin);   // 停在 ev=0
                evValue.setText("0");
            }
            applySliderVisibility();
        });
    }

    /** 恢复全自动时把滑动条复位到默认位置。 */
    private void resetSliders() {
        ui.post(() -> {
            if (evBar != null) {
                evBar.setProgress(0 - evBarMin);
                evValue.setText("0");
            }
            if (isoValue != null) isoValue.setText("auto");
            if (focusBar != null) {
                focusBar.setProgress(0);
                focusValue.setText("自动");
            }
        });
    }

    /** 枚举手机摄像头列表并生成切换按钮。 */
    private void loadCameras() {
        new Thread(() -> {
            try {
                JSONArray arr = new JSONArray(httpGet(BASE + "/cameras"));
                List<String[]> list = new ArrayList<>();
                for (int k = 0; k < arr.length(); k++) {
                    JSONObject jo = arr.getJSONObject(k);
                    if (jo.optBoolean("active")) {
                        selectedCameraId = jo.optString("id");
                    }
                    list.add(new String[]{jo.optString("id"), jo.optString("label")});
                }
                ui.post(() -> {
                    camerasCache.clear();
                    camerasCache.addAll(list);
                    refreshCamButtons();
                });
            } catch (Exception e) {
                Log.w(TAG, "loadCameras failed", e);
            }
        }).start();
    }

    /** 按摄像头列表重建切换按钮, 当前选中项带 ✓。 */
    private void refreshCamButtons() {
        if (camBtns == null) return;
        camBtns.removeAllViews();
        for (String[] c : camerasCache) {
            final String id = c[0];
            String label = c[1];
            boolean active = id.equals(selectedCameraId);
            Button b = makeButton((active ? "✓ " : "") + label, v -> {
                if (!id.equals(selectedCameraId)) {
                    selectedCameraId = id;
                    refreshCamButtons();
                    doGet("/setcam?camera=" + id);
                    Toast.makeText(MainActivity.this,
                            "切换摄像头，相机重启中...", Toast.LENGTH_SHORT).show();
                }
            });
            b.setEnabled(!active);
            camBtns.addView(b);
        }
    }

    private void loadSizes() {
        new Thread(() -> {
            try {
                JSONArray arr = new JSONArray(httpGet(BASE + "/sizes"));
                List<String> list = new ArrayList<>();
                for (int k = 0; k < arr.length(); k++) list.add(arr.getString(k));
                ui.post(() -> {
                    sizes.clear();
                    sizes.addAll(list);
                    sizeAdapter.notifyDataSetChanged();
                });
            } catch (Exception e) {
                Log.w(TAG, "loadSizes failed", e);
            }
        }).start();
    }

    /** 每 1.5s 刷新一次状态显示 */
    private void startStatusRefresh() {
        new Thread(() -> {
            while (uiRunning) {
                try {
                    JSONObject o = new JSONObject(httpGet(BASE + "/info"));
                    applyRanges(o.optJSONObject("cam"));
                    JSONObject cam = o.optJSONObject("cam");
                    float fd = cam == null ? -1 : (float) cam.optDouble("focusDist", -1);
                    String focusInfo = fd > 0 ? String.format("%.1fD (%.0fcm)", fd, 100f / fd) : "自动";
                    String txt = "状态: " + o.optString("status")
                            + "\n后端: " + o.optString("backend")
                            + "\n分辨率: " + o.optString("width") + " x " + o.optString("height")
                            + "\n帧率: " + String.format("%.1f", o.optDouble("fps")) + " fps"
                            + "\n画质: " + o.optString("jpegQuality")
                            + "\n对焦: " + focusInfo
                            + "  " + afStateName(cam == null ? -1 : cam.optInt("afState", -1));
                    ui.post(() -> {
                        if (statusText != null) statusText.setText(txt);
                    });
                } catch (Exception e) {
                    ui.post(() -> {
                        if (statusText != null) statusText.setText("服务未响应 (可能已停止)");
                    });
                }
                try {
                    Thread.sleep(1500);
                } catch (InterruptedException e) {
                    break;
                }
            }
        }).start();
    }

    private static String afStateName(int s) {
        switch (s) {
            case 0: return "未激活";
            case 1: return "扫描中";
            case 2: return "已对焦";
            case 3: return "对焦扫描中";
            case 4: return "已锁定";
            case 5: return "未对焦(被动)";
            case 6: return "未对焦";
            default: return "未知";
        }
    }

    private void doGet(String path) {
        new Thread(() -> {
            try {
                String r = httpGet(BASE + path);
                ui.post(() -> Toast.makeText(MainActivity.this, r, Toast.LENGTH_SHORT).show());
            } catch (Exception e) {
                ui.post(() -> Toast.makeText(MainActivity.this,
                        "请求失败: " + e.getMessage(), Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    /**
     * 用原始 Socket 直连本机 HTTP 服务, 绕过 Android 明文流量(cleartext)策略检查。
     */
    private String httpGet(String urlStr) throws Exception {
        return new String(httpGetBytes(urlStr), "UTF-8");
    }

    /** 二进制版本: /frame 返回 JPEG 字节流, 不能按字符串读。 */
    private byte[] httpGetBytes(String urlStr) throws Exception {
        URL u = new URL(urlStr);
        String host = u.getHost();
        int port = u.getPort() < 0 ? 80 : u.getPort();
        String path = u.getPath();
        if (u.getQuery() != null) path += "?" + u.getQuery();
        if (path == null || path.isEmpty()) path = "/";

        Socket sock = new Socket();
        try {
            sock.connect(new InetSocketAddress(host, port), 3000);
            sock.setSoTimeout(8000);
            OutputStream os = sock.getOutputStream();
            String req = "GET " + path + " HTTP/1.1\r\n"
                    + "Host: " + host + "\r\n"
                    + "Connection: close\r\n"
                    + "Accept: */*\r\n\r\n";
            os.write(req.getBytes("UTF-8"));
            os.flush();

            InputStream in = sock.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            byte[] all = bos.toByteArray();

            // 跳过 HTTP 响应头, 取 body (服务器不用 chunked)
            int split = -1;
            for (int i = 3; i < all.length; i++) {
                if (all[i - 3] == '\r' && all[i - 2] == '\n'
                        && all[i - 1] == '\r' && all[i] == '\n') {
                    split = i + 1;
                    break;
                }
            }
            if (split < 0) split = 0;
            byte[] body = new byte[all.length - split];
            System.arraycopy(all, split, body, 0, body.length);
            return body;
        } finally {
            try { sock.close(); } catch (Exception ignore) {}
        }
    }

    @Override
    protected void onDestroy() {
        uiRunning = false;
        super.onDestroy();
    }
}
