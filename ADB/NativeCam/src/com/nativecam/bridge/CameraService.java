package com.nativecam.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CameraMetadata;
import android.hardware.camera2.CaptureFailure;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.MeteringRectangle;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.util.Size;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

/**
 * NativeCam Bridge
 * 前台服务: 用 Camera2 的静态 JPEG 输出通道持续抓取摄像头最高分辨率画面
 * (视频流通道最高只能到 3264x2448, 静态 JPEG 通道才能拿到 4032x3024),
 * 并通过本地 HTTP 服务 (经 adb forward 走 USB) 暴露给 PC。
 *
 * PC 接入:
 *   adb forward tcp:8888 tcp:8888
 *   http://127.0.0.1:8888/frame   单帧原生分辨率 JPEG
 *   http://127.0.0.1:8888/stream  MJPEG 流
 *   http://127.0.0.1:8888/info    状态 JSON
 *   http://127.0.0.1:8888/sizes   摄像头支持的 JPEG 尺寸列表
 */
public class CameraService extends Service {
    private static final String TAG = "NativeCamBridge";
    private static final int PORT = 8888;
    private static final String CHANNEL_ID = "nativecam";

    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;
    private ImageReader jpegReader;
    private HandlerThread cameraThread;
    private Handler cameraHandler;

    private volatile byte[] latestJpeg = null;
    private volatile int jpegWidth = 0;
    private volatile int jpegHeight = 0;
    private volatile int frameCount = 0;
    private volatile double fps = 0;
    private volatile String status = "init";
    private volatile String lastError = "";
    private Size[] jpegSizes = new Size[0];
    private int sizeIndex = 0;

    // Camera1 (已废弃但直接映射到 HAL) 备用通道: 在 LIMITED 级别的 Samsung HAL 上,
    // 有时能访问 Camera2 看不到的更高拍照分辨率 (本设备的 4032x3024)。
    private android.hardware.Camera camera1 = null;
    private android.graphics.SurfaceTexture dummySurface = null;
    private String backend = "camera2";

    // 可调参数 (通过 HTTP 接口 /setres /setquality 修改)
    private volatile int jpegQuality = 95;
    private Size forcedSize = null;   // 用户强制指定的分辨率, 优先于自动选出的最大尺寸

    // 相机 3A 控制 (通过 HTTP 接口 /setcam 修改, 下一帧生效)
    // 默认即「跟原生相机一致」的自动模式: 连续自动对焦 + 自动曝光 + 自动白平衡
    private volatile int afMode = CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE;
    private volatile int aeMode = CameraMetadata.CONTROL_AE_MODE_ON;
    private volatile int awbMode = CameraMetadata.CONTROL_AWB_MODE_AUTO;
    private volatile Integer evIndex = null;        // 曝光补偿索引 (AE auto 时生效)
    private volatile Long exposureNs = null;        // 手动曝光时间 ns (AE off 时生效)
    private volatile Integer iso = null;            // 手动感光度 (AE off 时生效)
    private volatile Float focusDist = null;        // 手动对焦距离 (屈光度, AF off 时生效)

    // 点选测光/对焦区域 (归一化 x,y,w,h ∈ 0..1, 相对传感器画面; null = 全画面自动)
    // 同时作用于 AF 与 AE, 等价于原生相机 App 的 "点按对焦"
    private volatile float[] meteringRegion = null;
    private Rect activeArray = null;                // 传感器有效区域 (Camera2 坐标换算用)

    // ---- 曝光微调支持 (App 内 ISO/EV 滑动条) ----
    // AE 收敛时的实际曝光参数 (来自 CaptureResult 回调), 作为 ISO 手动模式下的快门基准
    private volatile long lastAeExposureNs = 0;
    private volatile int lastAeIso = 0;
    // 设备能力范围 (openCamera 时读取, -1 = 不支持)
    private volatile int evMin = 0, evMax = 0;
    // EV 每档对应的实际曝光值 (CONTROL_AE_COMPENSATION_STEP, 常见 1/3 或 1/2 EV)。
    // /info 上报给 PC 端, 用于把索引值换算成用户易懂的 EV (如 -18 档 × 1/3 = -6.0 EV)。
    private volatile double evStep = 1.0 / 3.0;
    private volatile int isoMin = -1, isoMax = -1;
    private volatile float focusMin = 0f, focusMax = 0f;  // 对焦距离范围 (屈光度)

    // 点击对焦: 单帧 capture 模式需要显式 AF_TRIGGER 才会驱动镜头扫描
    private volatile boolean afTriggerPending = false;
    private volatile long afTriggerUntilMs = 0;
    // 对焦诊断 (最近一帧 CaptureResult): /info 暴露, 供 PC 端闭环验证
    private volatile int lastAfState = -1;
    private volatile int lastLensState = -1;

    // 摄像头选择: null = 默认后置第一个; /setcam?camera=<id> 切换并重启相机
    private volatile String cameraIdPref = null;
    private volatile String activeCameraId = null;

    // ---- 自愈看门狗: 相机链路死亡(断连/卡死/回调丢失)后自动恢复, 无需手动重启 ----
    // 背景: 相机被系统回收触发 onDisconnected 时旧逻辑只 close 不重启, 流永久死亡;
    // 个别 HAL 异常时 capture 回调可能永远不触发, 画面同样卡死。
    private static final long WATCHDOG_INTERVAL_MS = 3000;  // 检查间隔
    private static final long STALL_TIMEOUT_MS = 8000;      // 超此时长无新帧判定卡死
    private volatile long lastFrameAtMs = 0;  // 最近收到图像帧的时刻 (elapsedRealtime)
    private volatile long sessionStartedAtMs = 0; // 会话建立时刻 (streaming 尚无帧时的卡死判定基准)
    private volatile int autoRecoverCount = 0; // 累计自动恢复次数 (/info 诊断可见)
    private volatile long nextRecoverAtMs = 0; // 下次允许自动恢复的时刻 (指数退避)
    private volatile int recoverFailStreak = 0; // 连续恢复失败次数 (streaming 恢复后清零)

    private ServerSocket serverSocket;
    private Thread httpThread;
    private volatile boolean running = true;
    private long fpsT0 = 0;
    private int fpsN = 0;

    // ------------------------------------------------------------------
    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "onCreate");
        startForegroundNotification();
        cameraThread = new HandlerThread("NativeCamThread");
        cameraThread.start();
        cameraHandler = new Handler(cameraThread.getLooper());
        startWatchdog();
        startHttpServer();
        openCamera();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // 若服务已在运行但相机未打开(如权限刚授予), 重试一次
        if (cameraDevice == null && !"error".equals(status)) {
            openCamera();
        }
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        running = false;
        closeCamera();
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (Exception ignored) {
        }
        if (cameraThread != null) cameraThread.quitSafely();
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    private void startForegroundNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "NativeCam", NotificationManager.IMPORTANCE_LOW);
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.createNotificationChannel(ch);
        }
        Notification.Builder nb = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        nb.setContentTitle("NativeCam Bridge")
                .setContentText("Camera service running on :" + PORT)
                .setSmallIcon(android.R.drawable.ic_menu_camera);
        startForeground(1, nb.build());
    }

    // ------------------------------------------------------------------
    private void openCamera() {
        try {
            CameraManager manager = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
            if (manager == null) {
                status = "error";
                lastError = "CameraManager null";
                return;
            }
            String[] ids = manager.getCameraIdList();
            // 用户已选择指定摄像头时优先使用; 否则默认后置第一个
            String camSel = null;
            if (cameraIdPref != null && Arrays.asList(ids).contains(cameraIdPref)) {
                camSel = cameraIdPref;
            } else {
                for (String id : ids) {
                    CameraCharacteristics cc = manager.getCameraCharacteristics(id);
                    Integer f = cc.get(CameraCharacteristics.LENS_FACING);
                    int facing = effectiveFacing(id, f == null ? -1 : f);
                    if (facing == 0) {   // 后置 (以 Camera1 元数据修正后)
                        camSel = id;
                        break;
                    }
                }
                if (camSel == null && ids.length > 0) camSel = ids[0];
            }
            if (camSel == null) {
                status = "error";
                lastError = "no camera";
                return;
            }

            final String camId = camSel;
            activeCameraId = camId;
            CameraCharacteristics cc = manager.getCameraCharacteristics(camId);
            // 保存传感器有效区域, 用于把归一化测光/对焦区域换算成 Camera2 像素坐标
            try {
                activeArray = cc.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE);
            } catch (Exception ignored) {
                activeArray = null;
            }
            // EV 补偿与 ISO 可调范围 (App 滑动条用)
            try {
                android.util.Range<Integer> evR = cc.get(
                        CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE);
                if (evR != null) {
                    evMin = evR.getLower();
                    evMax = evR.getUpper();
                }
            } catch (Exception ignored) {
                evMin = evMax = 0;
            }
            try {
                android.util.Rational st = cc.get(
                        CameraCharacteristics.CONTROL_AE_COMPENSATION_STEP);
                if (st != null && st.getDenominator() != 0) {
                    double d = st.doubleValue();
                    if (d > 0) evStep = d;
                }
            } catch (Exception ignored) {
                evStep = 1.0 / 3.0;   // 兜底: 多数设备为 1/3 EV 一档
            }
            try {
                android.util.Range<Integer> isoR = cc.get(
                        CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE);
                if (isoR != null) {
                    isoMin = isoR.getLower();
                    isoMax = isoR.getUpper();
                }
            } catch (Exception ignored) {
                isoMin = isoMax = -1;
            }
            // 对焦距离范围 (屈光度): 0=无穷远, max=最近
            try {
                Float minDist = cc.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE);
                if (minDist != null && minDist > 0) {
                    focusMin = 0f;
                    focusMax = minDist;
                }
            } catch (Exception ignored) {
                focusMin = focusMax = 0f;
            }
            StreamConfigurationMap map = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            ArrayList<Size> all = new ArrayList<>();
            if (map != null) {
                Size[] normal = map.getOutputSizes(ImageFormat.JPEG);
                if (normal != null) all.addAll(Arrays.asList(normal));
                // Android 11+: 高分辨率拍照通道。本设备的 4032x3024 (12MP) 只在该通道暴露,
                // 常规 getOutputSizes(JPEG) 最大仅到 3264x2448。
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    Size[] high = map.getHighResolutionOutputSizes(ImageFormat.JPEG);
                    if (high != null && high.length > 0) {
                        Log.i(TAG, "high-res channel sizes=" + high.length);
                        all.addAll(Arrays.asList(high));
                    }
                }
            }
            if (all.isEmpty()) {
                all.add(new Size(4032, 3024));
                all.add(new Size(1920, 1080));
                all.add(new Size(1280, 720));
                all.add(new Size(640, 480));
                Log.w(TAG, "no JPEG sizes reported, using fallback list");
            }
            // 去重后转数组
            jpegSizes = new LinkedHashSet<>(all).toArray(new Size[0]);

            // 候选尺寸按面积从大到小排序
            Arrays.sort(jpegSizes, (a, b) -> Long.compare(
                    (long) b.getWidth() * b.getHeight(),
                    (long) a.getWidth() * a.getHeight()));
            sizeIndex = 0;
            Log.i(TAG, "JPEG candidates=" + jpegSizes.length + ", largest="
                    + jpegSizes[0].getWidth() + "x" + jpegSizes[0].getHeight());

            // 探测 Camera1 通道: 在 LIMITED 级别的 Samsung HAL 上, Camera1 常能访问
            // Camera2 看不到的更高拍照分辨率。若更大则优先使用。
            // 注意: 用户显式选择了摄像头 (/setcam?camera=<id>) 时必须尊重选择,
            // 否则切换摄像头会被该策略强制拉回后置 Camera1。
            Size c1 = probeCamera1MaxSize();
            if (cameraIdPref == null && c1 != null && (long) c1.getWidth() * c1.getHeight()
                    > (long) jpegSizes[0].getWidth() * jpegSizes[0].getHeight()) {
                Log.i(TAG, "Camera1 offers larger size " + c1.getWidth() + "x" + c1.getHeight()
                        + " > Camera2 " + jpegSizes[0].getWidth() + "x"
                        + jpegSizes[0].getHeight() + " -> using Camera1");
                backend = "camera1";
                startCamera1(c1);
                return;
            }
            backend = "camera2";

            manager.openCamera(camId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice cd) {
                    cameraDevice = cd;
                    status = "opened";
                    trySession(0);
                }

                @Override
                public void onDisconnected(CameraDevice cd) {
                    // 只标记状态, 恢复统一交给看门狗(带退避)。
                    // 若在此立即重开, 会与 closeCamera/正在销毁的资源竞态,
                    // 且持续性错误下 onError<->openCamera 快速循环把相机打挂。
                    status = "disconnected";
                    closeCamera();
                }

                @Override
                public void onError(CameraDevice cd, int error) {
                    status = "error";
                    lastError = "camera error " + error;
                    // 用户选择的摄像头打开失败 → 回退默认后置 (一次性, 不循环)
                    if (cameraIdPref != null) {
                        Log.w(TAG, "camera " + cameraIdPref + " onError " + error + ", 回退默认后置");
                        cameraIdPref = null;
                        status = "restarting";
                        cameraHandler.postDelayed(CameraService.this::openCamera, 600);
                    }
                    // 其余错误不在此重开: 看门狗按退避策略统一恢复
                }
            }, cameraHandler);
        } catch (SecurityException e) {
            status = "error";
            lastError = "CAMERA permission denied: " + e.getMessage();
            Log.e(TAG, "permission", e);
        } catch (Exception e) {
            // 用户选择的物理摄像头可能不可直接打开 (部分设备限制) → 回退默认后置
            lastError = e.toString();
            Log.e(TAG, "openCamera", e);
            if (cameraIdPref != null) {
                Log.w(TAG, "camera " + cameraIdPref + " 无法打开, 回退默认后置");
                cameraIdPref = null;
                status = "restarting";
                cameraHandler.postDelayed(this::openCamera, 600);
                return;
            }
            status = "error";
        }
    }

    /**
     * 用指定下标的候选尺寸建立捕获会话。部分设备(Samsung HAL)会报告实际无法建流的尺寸,
     * 因此失败时自动降级到下一个更小的尺寸, 直到成功 —— 保证最终拿到该设备真实可用的
     * 最高分辨率, 而不是停留在"报告支持但建流失败"的尺寸上。
     */
    private void trySession(int index) {
        if (cameraDevice == null) return;
        if (jpegSizes == null || index >= jpegSizes.length) {
            status = "error";
            lastError = "no usable JPEG size (tried all candidates)";
            return;
        }
        if (jpegReader != null) {
            try {
                jpegReader.close();
            } catch (Exception ignored) {
            }
            jpegReader = null;
        }
        // 用户强制指定的分辨率优先 (index 0 时), 否则按候选列表降级
        Size sz = (index == 0 && forcedSize != null) ? forcedSize : jpegSizes[index];
        sizeIndex = index;
        jpegWidth = sz.getWidth();
        jpegHeight = sz.getHeight();
        try {
            jpegReader = ImageReader.newInstance(jpegWidth, jpegHeight, ImageFormat.JPEG, 2);
            jpegReader.setOnImageAvailableListener(reader -> {
                Image img = reader.acquireLatestImage();
                if (img == null) return;
                try {
                    ByteBuffer buf = img.getPlanes()[0].getBuffer();
                    byte[] bytes = new byte[buf.remaining()];
                    buf.get(bytes);
                    latestJpeg = bytes;
                    frameCount++;
                    fpsN++;
                    long now = System.currentTimeMillis();
                    if (fpsT0 == 0) {
                        fpsT0 = now;
                    } else if (now - fpsT0 >= 1000) {
                        fps = fpsN * 1000.0 / (now - fpsT0);
                        fpsT0 = now;
                        fpsN = 0;
                    }
                    status = "streaming";
                    // 帧心跳: 看门狗据此判断采集链路是否卡死
                    lastFrameAtMs = android.os.SystemClock.elapsedRealtime();
                    recoverFailStreak = 0;   // 已恢复流式输出, 重置退避计数
                } finally {
                    img.close();
                }
            }, cameraHandler);

            Log.i(TAG, "trying size #" + index + " " + jpegWidth + "x" + jpegHeight);
            cameraDevice.createCaptureSession(Arrays.asList(jpegReader.getSurface()),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(CameraCaptureSession s) {
                            captureSession = s;
                            status = "streaming";
                            sessionStartedAtMs = android.os.SystemClock.elapsedRealtime();
                            lastError = "";
                            Log.i(TAG, "session OK at " + jpegWidth + "x" + jpegHeight);
                            captureLoop();
                        }

                        @Override
                        public void onConfigureFailed(CameraCaptureSession s) {
                            Log.w(TAG, "configure failed at " + jpegWidth + "x"
                                    + jpegHeight + " -> downgrade");
                            trySession(index + 1);
                        }
                    }, cameraHandler);
        } catch (Exception e) {
            Log.e(TAG, "session error at " + jpegWidth + "x" + jpegHeight, e);
            lastError = "session: " + e.toString();
            trySession(index + 1);
        }
    }

    /**
     * 用循环 capture (单次拍照) 而非 setRepeatingRequest:
     * 高分辨率 JPEG 属于静态捕获通道, 部分设备对 JPEG 的 repeating request 支持不佳。
     */
    private void captureLoop() {
        if (cameraDevice == null || captureSession == null || !running) return;
        try {
            // 用录像模板(RECORD)而非拍照模板(STILL_CAPTURE):
            // 拍照模板为静态照片优化, 曝光更充分/画面更亮(保留暗部、快门偏慢),
            // 与原生相机 App 的预览观感明显不一致(同 EV/ISO 下本 App 偏亮)。
            // 录像模板的曝光策略更接近实时预览且持续采集时稳定。
            CaptureRequest.Builder b =
                    cameraDevice.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
            b.addTarget(jpegReader.getSurface());
            b.set(CaptureRequest.JPEG_QUALITY, (byte) jpegQuality);
            b.set(CaptureRequest.JPEG_ORIENTATION, 0);
            // 高质量降噪/锐化提示: 投影场景光弱, 压噪点 (HAL 支持时生效)
            b.set(CaptureRequest.NOISE_REDUCTION_MODE,
                    CameraMetadata.NOISE_REDUCTION_MODE_HIGH_QUALITY);
            b.set(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_HIGH_QUALITY);
            // ---- 3A 控制: 与原生相机一致的自动模式, 手动模式可通过 /setcam 切换 ----
            b.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO);
            b.set(CaptureRequest.CONTROL_AE_MODE, aeMode);
            b.set(CaptureRequest.CONTROL_AWB_MODE, awbMode);
            if (evIndex != null) {
                b.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, evIndex);
            }
            if (aeMode == CameraMetadata.CONTROL_AE_MODE_OFF) {
                // 手动曝光: 不支持的设备会静默忽略
                if (exposureNs != null) {
                    b.set(CaptureRequest.SENSOR_EXPOSURE_TIME, exposureNs);
                }
                if (iso != null) {
                    b.set(CaptureRequest.SENSOR_SENSITIVITY, iso);
                }
            }
            // ---- 对焦: 单帧 capture 模式下 CONTINUOUS_PICTURE 不会主动扫描镜头,
            // 点击对焦必须显式发 AF_TRIGGER_START (拍照类 App 的标准做法)。 ----
            long nowMs = System.currentTimeMillis();
            boolean triggerStart = afTriggerPending;
            boolean triggerActive = nowMs < afTriggerUntilMs;
            if (triggerStart) {
                afTriggerPending = false;
                afTriggerUntilMs = nowMs + 3000;   // 触发窗口 3s, 期间 AF_MODE_AUTO 收敛
            }
            if (triggerStart || triggerActive) {
                b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_AUTO);
                b.set(CaptureRequest.CONTROL_AF_TRIGGER, triggerStart
                        ? CameraMetadata.CONTROL_AF_TRIGGER_START
                        : CameraMetadata.CONTROL_AF_TRIGGER_IDLE);
            } else {
                b.set(CaptureRequest.CONTROL_AF_MODE, afMode);
            }
            if (afMode == CameraMetadata.CONTROL_AF_MODE_OFF && focusDist != null) {
                b.set(CaptureRequest.LENS_FOCUS_DISTANCE, focusDist);
            }
            // ---- 点选测光/对焦区域 (tap-to-focus): AF 与 AE 同时指向该区域 ----
            MeteringRectangle mr = buildMeteringRect();
            if (mr != null) {
                b.set(CaptureRequest.CONTROL_AF_REGIONS, new MeteringRectangle[]{mr});
                b.set(CaptureRequest.CONTROL_AE_REGIONS, new MeteringRectangle[]{mr});
            }
            captureSession.capture(b.build(), new CameraCaptureSession.CaptureCallback() {
                @Override
                public void onCaptureCompleted(CameraCaptureSession session,
                                               CaptureRequest request, TotalCaptureResult result) {
                    // 记录 AE 收敛后的实际曝光参数, 作为 ISO/快门手动微调的基准
                    try {
                        Long expNs = result.get(CaptureResult.SENSOR_EXPOSURE_TIME);
                        Integer sens = result.get(CaptureResult.SENSOR_SENSITIVITY);
                        if (expNs != null && expNs > 0) lastAeExposureNs = expNs;
                        if (sens != null && sens > 0) lastAeIso = sens;
                        Integer afs = result.get(CaptureResult.CONTROL_AF_STATE);
                        Integer lens = result.get(CaptureResult.LENS_STATE);
                        if (afs != null) lastAfState = afs;
                        if (lens != null) lastLensState = lens;
                    } catch (Exception ignored) {
                    }
                    cameraHandler.post(CameraService.this::captureLoop);
                }

                @Override
                public void onCaptureFailed(CameraCaptureSession session,
                                            CaptureRequest request, CaptureFailure failure) {
                    lastError = "capture failed " + failure.getReason();
                    cameraHandler.postDelayed(CameraService.this::captureLoop, 500);
                }
            }, cameraHandler);
        } catch (Exception e) {
            lastError = "capture: " + e.toString();
            Log.e(TAG, "captureLoop", e);
            cameraHandler.postDelayed(CameraService.this::captureLoop, 1000);
        }
    }

    // ------------------------------------------------------------------
    // Camera1 (已废弃 API) 备用通道:
    // Camera1 直接映射到 HAL, 在 LIMITED 级别的设备上常能访问 Camera2 看不到的
    // 更高拍照分辨率 (本设备的 4032x3024 / 12MP)。
    // ------------------------------------------------------------------
    private Size probeCamera1MaxSize() {
        android.hardware.Camera c = null;
        try {
            int num = android.hardware.Camera.getNumberOfCameras();
            int backId = 0;
            android.hardware.Camera.CameraInfo ci = new android.hardware.Camera.CameraInfo();
            for (int i = 0; i < num; i++) {
                android.hardware.Camera.getCameraInfo(i, ci);
                if (ci.facing == android.hardware.Camera.CameraInfo.CAMERA_FACING_BACK) {
                    backId = i;
                    break;
                }
            }
            c = android.hardware.Camera.open(backId);
            android.hardware.Camera.Parameters p = c.getParameters();
            List<android.hardware.Camera.Size> sizes = p.getSupportedPictureSizes();
            int maxPx = 0;
            android.hardware.Camera.Size best = null;
            for (android.hardware.Camera.Size s : sizes) {
                if (s.width * s.height > maxPx) {
                    maxPx = s.width * s.height;
                    best = s;
                }
            }
            if (best != null) {
                Log.i(TAG, "Camera1 picture sizes=" + (sizes == null ? 0 : sizes.size())
                        + ", max=" + best.width + "x" + best.height);
                return new Size(best.width, best.height);
            }
        } catch (Exception e) {
            Log.w(TAG, "Camera1 probe failed: " + e);
        } finally {
            if (c != null) {
                try {
                    c.release();
                } catch (Exception ignored) {
                }
            }
        }
        return null;
    }

    /** Camera1 通道使用的后置摄像头索引 (open 前 probe 同款选择逻辑)。 */
    private static int backIdOfCamera1() {
        int num = android.hardware.Camera.getNumberOfCameras();
        android.hardware.Camera.CameraInfo ci = new android.hardware.Camera.CameraInfo();
        for (int i = 0; i < num; i++) {
            android.hardware.Camera.getCameraInfo(i, ci);
            if (ci.facing == android.hardware.Camera.CameraInfo.CAMERA_FACING_BACK) {
                return i;
            }
        }
        return 0;
    }

    private void startCamera1(Size target) {
        // Camera1 通道固定使用后置摄像头, 同步 id 供 /info 显示
        activeCameraId = String.valueOf(backIdOfCamera1());
        try {
            int num = android.hardware.Camera.getNumberOfCameras();
            int backId = 0;
            android.hardware.Camera.CameraInfo ci = new android.hardware.Camera.CameraInfo();
            for (int i = 0; i < num; i++) {
                android.hardware.Camera.getCameraInfo(i, ci);
                if (ci.facing == android.hardware.Camera.CameraInfo.CAMERA_FACING_BACK) {
                    backId = i;
                    break;
                }
            }
            camera1 = android.hardware.Camera.open(backId);
            android.hardware.Camera.Parameters p = camera1.getParameters();
            p.setPictureSize(target.getWidth(), target.getHeight());
            p.setJpegQuality(jpegQuality);
            p.setPictureFormat(ImageFormat.JPEG);
            // 3A: 连续自动对焦 + 自动白平衡 (与原生相机一致)
            try {
                p.setFocusMode(android.hardware.Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
            } catch (Exception ignored) {
            }
            try {
                p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_AUTO);
            } catch (Exception ignored) {
            }
            try {
                p.setAutoExposureLock(false);
            } catch (Exception ignored) {
            }
            camera1.setParameters(p);
            // 虚拟预览目标: 不需要真实显示, 因此屏幕可以关闭
            dummySurface = new android.graphics.SurfaceTexture(0);
            camera1.setPreviewTexture(dummySurface);
            camera1.startPreview();
            jpegWidth = target.getWidth();
            jpegHeight = target.getHeight();
            Log.i(TAG, "Camera1 started at " + jpegWidth + "x" + jpegHeight);
            status = "streaming";
            lastError = "";
            camera1TakePicture();
        } catch (Exception e) {
            Log.e(TAG, "Camera1 start failed", e);
            lastError = "camera1 start: " + e;
            status = "error";
        }
    }

    private void camera1TakePicture() {
        if (camera1 == null || !running) return;
        try {
            camera1.takePicture(null, null, new android.hardware.Camera.PictureCallback() {
                @Override
                public void onPictureTaken(byte[] data, android.hardware.Camera cam) {
                    if (data != null && data.length > 0) {
                        latestJpeg = data;
                        frameCount++;
                        fpsN++;
                        long now = System.currentTimeMillis();
                        if (fpsT0 == 0) {
                            fpsT0 = now;
                        } else if (now - fpsT0 >= 1000) {
                            fps = fpsN * 1000.0 / (now - fpsT0);
                            fpsT0 = now;
                            fpsN = 0;
                        }
                        status = "streaming";
                        lastError = "";
                    }
                    // takePicture 后必须重新 startPreview 才能拍下一张
                    cameraHandler.post(() -> {
                        try {
                            cam.startPreview();
                            camera1TakePicture();
                        } catch (Exception e) {
                            Log.e(TAG, "camera1 next shot", e);
                        }
                    });
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "camera1 takePicture", e);
            cameraHandler.postDelayed(() -> camera1TakePicture(), 500);
        }
    }

    private void closeCamera() {
        try {
            if (captureSession != null) {
                captureSession.close();
                captureSession = null;
            }
        } catch (Exception ignored) {
        }
        try {
            if (cameraDevice != null) {
                cameraDevice.close();
                cameraDevice = null;
            }
        } catch (Exception ignored) {
        }
        try {
            if (jpegReader != null) {
                jpegReader.close();
                jpegReader = null;
            }
        } catch (Exception ignored) {
        }
        try {
            if (camera1 != null) {
                camera1.stopPreview();
                camera1.release();
                camera1 = null;
            }
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------------
    private void startHttpServer() {
        httpThread = new Thread(() -> {
            try {
                serverSocket = new ServerSocket(PORT);
                serverSocket.setReuseAddress(true);
                Log.i(TAG, "HTTP server listening on " + PORT);
                while (running) {
                    Socket sock = serverSocket.accept();
                    Thread t = new Thread(() -> handleClient(sock));
                    t.setDaemon(true);
                    t.start();
                }
            } catch (Exception e) {
                Log.e(TAG, "http server", e);
            }
        });
        httpThread.setDaemon(true);
        httpThread.start();
    }

    private void handleClient(Socket sock) {
        try {
            BufferedReader in = new BufferedReader(
                    new InputStreamReader(sock.getInputStream()));
            String line = in.readLine();
            if (line == null) {
                sock.close();
                return;
            }
            String[] parts = line.split(" ");
            String path = (parts.length >= 2) ? parts[1] : "/";
            String base = path.split("\\?")[0];
            String query = "";
            int qi = path.indexOf('?');
            if (qi >= 0 && qi + 1 < path.length()) query = path.substring(qi + 1);
            OutputStream out = sock.getOutputStream();

            if ("/frame".equals(base) || "/snapshot".equals(base)) {
                byte[] jpg = latestJpeg;
                if (jpg == null) {
                    writeResponse(out, 503, "text/plain", "no frame yet".getBytes("UTF-8"));
                } else {
                    out.write(("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                            + "Content-Length: " + jpg.length + "\r\n"
                            + "Cache-Control: no-cache\r\n\r\n").getBytes("UTF-8"));
                    out.write(jpg);
                }
            } else if ("/stream".equals(base)) {
                out.write(("HTTP/1.1 200 OK\r\n"
                        + "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                        + "Cache-Control: no-cache\r\n"
                        + "Pragma: no-cache\r\n"
                        + "Connection: close\r\n\r\n").getBytes("UTF-8"));
                out.flush();
                byte[] last = null;
                while (running) {
                    byte[] jpg = latestJpeg;
                    if (jpg != null && jpg != last) {
                        out.write(("--frame\r\nContent-Type: image/jpeg\r\n"
                                + "Content-Length: " + jpg.length + "\r\n\r\n").getBytes("UTF-8"));
                        out.write(jpg);
                        out.write("\r\n".getBytes("UTF-8"));
                        out.flush();
                        last = jpg;
                    } else {
                        Thread.sleep(20);
                    }
                }
            } else if ("/info".equals(base)) {
                JSONObject o = new JSONObject();
                o.put("status", status);
                o.put("backend", backend);
                o.put("width", jpegWidth);
                o.put("height", jpegHeight);
                o.put("fps", fps);
                o.put("frames", frameCount);
                o.put("jpegQuality", jpegQuality);
                o.put("lastError", lastError);
                o.put("cameraId", activeCameraId == null ? "" : activeCameraId);
                // 当前 3A 状态
                JSONObject cam = new JSONObject();
                cam.put("af", afName(afMode));
                cam.put("ae", aeMode == CameraMetadata.CONTROL_AE_MODE_OFF ? "off" : "auto");
                cam.put("awb", wbName(awbMode));
                cam.put("ev", evIndex == null ? 0 : evIndex);
                cam.put("exposureNs", exposureNs == null ? -1 : exposureNs);
                cam.put("iso", iso == null ? -1 : iso);
                cam.put("focusDist", focusDist == null ? -1 : focusDist);
                cam.put("focusMin", focusMin);
                cam.put("focusMax", focusMax);
                cam.put("evMin", evMin);
                cam.put("evMax", evMax);
                cam.put("evStep", evStep);
                cam.put("isoMin", isoMin);
                cam.put("isoMax", isoMax);
                cam.put("lastAeExposureNs", lastAeExposureNs);
                cam.put("lastAeIso", lastAeIso);
                cam.put("afState", lastAfState);
                cam.put("lensState", lastLensState);
                cam.put("autoRecoverCount", autoRecoverCount);
                o.put("cam", cam);
                writeResponse(out, 200, "application/json", o.toString(2).getBytes("UTF-8"));
            } else if ("/cameras".equals(base)) {
                // 枚举全部可用摄像头 (含逻辑多摄背后的物理子摄, 如超广角/长焦):
                // [{id, facing, label, active}], 0=后置 1=前置 -1=外部/未知
                JSONArray arr = new JSONArray();
                try {
                    CameraManager manager =
                            (CameraManager) getSystemService(Context.CAMERA_SERVICE);
                    if (manager != null) {
                        List<String> seen = new ArrayList<>();
                        for (String id : manager.getCameraIdList()) {
                            CameraCharacteristics cc = manager.getCameraCharacteristics(id);
                            Integer f = cc.get(CameraCharacteristics.LENS_FACING);
                            int facing = effectiveFacing(id, f == null ? -1 : f);
                            addCameraEntry(arr, id, facing,
                                    id.equals(activeCameraId), false);
                            seen.add(id);
                            // 逻辑多摄: 展开背后的物理摄像头 (部分设备可直接打开,
                            // 打不开时服务端会自动回退默认后置)
                            try {
                                java.util.Set<String> phys = cc.getPhysicalCameraIds();
                                if (phys != null && phys.size() > 1) {
                                    for (String pid : phys) {
                                        if (!seen.contains(pid)) {
                                            seen.add(pid);
                                            addCameraEntry(arr, pid,
                                                    effectiveFacing(pid, facing),
                                                    pid.equals(activeCameraId), true);
                                        }
                                    }
                                }
                            } catch (Exception ignored) {
                            }
                        }
                        // 隐藏摄像头探测: 部分三星等设备的超广角/长焦不在
                        // getCameraIdList() 中, 但 getCameraCharacteristics(hid)
                        // 能查到且可直接打开。暴力扫描常见 id 区间。
                        for (int i = 0; i <= 255; i++) {
                            String hid = String.valueOf(i);
                            if (seen.contains(hid)) continue;
                            try {
                                CameraCharacteristics cc =
                                        manager.getCameraCharacteristics(hid);
                                Integer f = cc.get(CameraCharacteristics.LENS_FACING);
                                int facing = effectiveFacing(hid, f == null ? -1 : f);
                                addCameraEntry(arr, hid, facing,
                                        hid.equals(activeCameraId), true);
                                seen.add(hid);
                            } catch (Exception ignored) {
                            }
                        }
                    }
                } catch (Exception ignored) {
                }
                writeResponse(out, 200, "application/json",
                        arr.toString().getBytes("UTF-8"));
            } else if ("/sizes".equals(base)) {
                JSONArray arr = new JSONArray();
                for (Size s : jpegSizes) {
                    arr.put(s.getWidth() + "x" + s.getHeight());
                }
                writeResponse(out, 200, "application/json", arr.toString(2).getBytes("UTF-8"));
            } else if ("/stop".equals(base) || "/quit".equals(base)) {
                // 先回响应再停服务, 否则客户端收不到回复
                writeResponse(out, 200, "application/json",
                        "{\"result\":\"stopping\"}".getBytes("UTF-8"));
                out.flush();
                cameraHandler.postDelayed(this::stopServiceNow, 300);
            } else if ("/restart".equals(base)) {
                writeResponse(out, 200, "application/json",
                        "{\"result\":\"restarting\"}".getBytes("UTF-8"));
                out.flush();
                cameraHandler.post(this::restartCamera);
            } else if ("/setres".equals(base)) {
                writeResponse(out, 200, "application/json",
                        setResolution(query).getBytes("UTF-8"));
            } else if ("/setquality".equals(base)) {
                writeResponse(out, 200, "application/json",
                        setQuality(query).getBytes("UTF-8"));
            } else if ("/setcam".equals(base)) {
                writeResponse(out, 200, "application/json",
                        setCam(query).getBytes("UTF-8"));
            } else {
                String html = "<html><head><meta charset=\"utf-8\"><title>NativeCam Bridge</title>"
                        + "<style>body{background:#1e1e1e;color:#ddd;font-family:sans-serif;padding:24px}"
                        + "code{background:#000;padding:2px 6px;color:#dcdcaa;display:inline-block;margin:2px 0}"
                        + "</style></head><body><h1>NativeCam Bridge</h1>"
                        + "<p>status: <b>" + status + "</b> &nbsp; " + jpegWidth + "x" + jpegHeight
                        + " &nbsp; " + String.format("%.1f", fps) + " fps &nbsp; frames=" + frameCount + "</p>"
                        + "<div><code>GET /frame</code> single native-resolution JPEG</div>"
                        + "<div><code>GET /stream</code> MJPEG stream</div>"
                        + "<div><code>GET /info</code> status JSON</div>"
                        + "<div><code>GET /sizes</code> supported JPEG sizes</div>"
                        + "<div><code>GET /setcam?af=auto&amp;ae=auto&amp;wb=auto</code> camera controls</div>"
                        + "<div style=\"font-size:0.9em;color:#8a8\">&nbsp;&nbsp;af: auto|off|macro &nbsp; focus: diopter &nbsp; "
                        + "ae: auto|off &nbsp; exposure: 1/60|ns &nbsp; iso: N &nbsp; ev: N &nbsp; wb: auto|daylight|... &nbsp; reset: 1</div>"
                        + "</body></html>";
                writeResponse(out, 200, "text/html; charset=utf-8", html.getBytes("UTF-8"));
            }
            out.flush();
            sock.close();
        } catch (Exception e) {
            try {
                sock.close();
            } catch (Exception ignored) {
            }
        }
    }

    // ------------------------------------------------------------------
    // 远程控制: 停止 / 重启 / 改分辨率 / 改画质
    // ------------------------------------------------------------------
    private Map<String, String> parseQuery(String query) {
        Map<String, String> m = new HashMap<>();
        if (query == null || query.isEmpty()) return m;
        for (String kv : query.split("&")) {
            int i = kv.indexOf('=');
            if (i > 0) {
                m.put(kv.substring(0, i).trim(), kv.substring(i + 1).trim());
            }
        }
        return m;
    }

    private void stopServiceNow() {
        Log.i(TAG, "stop requested via HTTP");
        running = false;
        closeCamera();
        try {
            stopForeground(true);
        } catch (Exception ignored) {
        }
        stopSelf();
        // 通过主线程延迟终止整个进程 (含可能存活的 MainActivity), 保证 /stop 真正关闭 App。
        // 延迟 400ms 让 stopSelf 的 onDestroy 先完成, 避免被系统当作异常死亡而重启。
        new Handler(Looper.getMainLooper()).postDelayed(() -> {
            Log.i(TAG, "exiting process");
            android.os.Process.killProcess(android.os.Process.myPid());
            System.exit(0);
        }, 400);
    }

    /** 朝向校正: 该设备 Camera2 的 LENS_FACING 元数据与实际相反,
     *  Camera1 的 CameraInfo 与实际一致 —— 优先采用 Camera1 的报告。 */
    private int effectiveFacing(String id, int camera2Facing) {
        try {
            int idi = Integer.parseInt(id);
            int n1 = android.hardware.Camera.getNumberOfCameras();
            if (idi >= 0 && idi < n1) {
                android.hardware.Camera.CameraInfo ci =
                        new android.hardware.Camera.CameraInfo();
                android.hardware.Camera.getCameraInfo(idi, ci);
                return ci.facing;
            }
        } catch (Exception ignored) {
        }
        return camera2Facing;
    }

    /** 生成一条摄像头条目: 朝向 + 焦距(区分超广角/主摄/长焦) + 是否物理子摄。 */
    private void addCameraEntry(JSONArray arr, String id, int facing, boolean active,
                                boolean physical) {
        try {
            JSONObject jo = new JSONObject();
            jo.put("id", id);
            jo.put("facing", facing);
            String name;
            if (facing == 1) {
                name = "前置";
            } else if (facing == 0) {
                name = "后置";
            } else {
                name = "外接";
            }
            if (physical) name += "(物理)";
            // 焦距标注: 超广角 <3mm, 主摄 4-8mm, 长焦 >10mm (近似)
            try {
                CameraManager manager = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
                CameraCharacteristics cc = manager.getCameraCharacteristics(id);
                float[] lens = cc.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
                if (lens != null && lens.length > 0) {
                    float mm = lens[0];
                    name += String.format(java.util.Locale.US, " %.1fmm", mm);
                    if (facing == 0 && lens[0] < 3f) name = "后置超广角";
                    else if (facing == 0 && lens[0] > 9f) name = "后置长焦";
                }
            } catch (Exception ignored) {
            }
            jo.put("label", name + " (#" + id + ")");
            jo.put("active", active);
            arr.put(jo);
        } catch (Exception ignored) {
        }
    }

    /** 启动自愈看门狗: 定期检查是否有新帧, 卡死/断连时自动恢复相机。 */
    private void startWatchdog() {
        cameraHandler.postDelayed(this::watchdogTick, WATCHDOG_INTERVAL_MS);
    }

    /**
     * 看门狗心跳: 覆盖三类"画面卡住"场景 —
     * 1) capture 回调丢失/HAL 假死: streaming 状态下长时间无新帧 → restartCamera
     * 2) 相机被系统回收: status 停在 disconnected → openCamera
     * 3) 相机 HAL 出错未自动恢复: status 停在 error/restarting → openCamera
     * 恢复失败不会死循环 — 只有状态匹配时才触发, 且每次触发间隔 ≥ 一个检查周期。
     */
    private void watchdogTick() {
        try {
            if (running) {
                long now = android.os.SystemClock.elapsedRealtime();
                boolean streaming = "streaming".equals(status);
                // 卡死判定基准: 优先用最近帧心跳; 会话建立后从未出帧则用会话建立时刻
                long baseline = lastFrameAtMs > 0 ? lastFrameAtMs : sessionStartedAtMs;
                boolean stalled = streaming && baseline > 0
                        && (now - baseline) > STALL_TIMEOUT_MS;
                boolean dead = "disconnected".equals(status)
                        || "error".equals(status)
                        || "restarting".equals(status);
                if (stalled || (dead && now >= nextRecoverAtMs)) {
                    // 指数退避: 3s, 6s, 12s, 24s, 48s, 60s(封顶)。
                    // 持续性错误(相机被系统禁用等)下不会快速循环打挂相机。
                    long backoff = Math.min(
                            3000L << Math.min(recoverFailStreak, 5), 60000L);
                    nextRecoverAtMs = now + backoff;
                    recoverFailStreak++;
                    autoRecoverCount++;
                    if (stalled) {
                        Log.w(TAG, "watchdog: no frame for " + (now - baseline)
                                + "ms -> restartCamera (backoff " + backoff + "ms)");
                        restartCamera();
                    } else {
                        Log.w(TAG, "watchdog: status=" + status
                                + " -> openCamera (backoff " + backoff + "ms)");
                        openCamera();
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "watchdog error", e);
        } finally {
            cameraHandler.postDelayed(this::watchdogTick, WATCHDOG_INTERVAL_MS);
        }
    }

    private void restartCamera() {
        Log.i(TAG, "restart requested");
        closeCamera();
        cameraDevice = null;
        status = "restarting";
        if ("camera1".equals(backend)) {
            Size target = (forcedSize != null) ? forcedSize : probeCamera1MaxSize();
            if (target == null) {
                target = jpegSizes.length > 0 ? jpegSizes[0] : new Size(3264, 2448);
            }
            startCamera1(target);
        } else {
            // 给 HAL 留出释放时间, 否则紧接着 openCamera + createCaptureSession
            // 容易偶发失败而静默降级到 Camera1 备用通道
            cameraHandler.postDelayed(this::openCamera, 600);
        }
    }

    /** 应用新分辨率: 重建捕获会话 (forcedSize 优先于自动选出的最大尺寸) */
    private void restartSessionWithForced() {
        if ("camera1".equals(backend)) {
            closeCamera();
            Size target = (forcedSize != null) ? forcedSize
                    : (jpegSizes.length > 0 ? jpegSizes[0] : new Size(3264, 2448));
            startCamera1(target);
            return;
        }
        if (captureSession != null) {
            try {
                captureSession.close();
            } catch (Exception ignored) {
            }
            captureSession = null;
        }
        trySession(0);
    }

    private String setResolution(String query) {
        try {
            Map<String, String> q = parseQuery(query);
            if ("1".equals(q.get("auto")) || "true".equals(q.get("auto"))) {
                forcedSize = null;
                Log.i(TAG, "setres -> auto (highest)");
                cameraHandler.post(this::restartSessionWithForced);
                return "{\"result\":\"ok\",\"size\":\"auto\"}";
            }
            int w;
            int h;
            String size = q.get("size");
            if (size != null && size.contains("x")) {
                String[] wh = size.toLowerCase().split("x");
                w = Integer.parseInt(wh[0].trim());
                h = Integer.parseInt(wh[1].trim());
            } else {
                String sw = q.get("w");
                String sh = q.get("h");
                if (sw == null || sh == null) {
                    return "{\"result\":\"error\",\"error\":\"use ?size=WxH or ?w=W&h=H\"}";
                }
                w = Integer.parseInt(sw);
                h = Integer.parseInt(sh);
            }
            boolean found = false;
            for (Size s : jpegSizes) {
                if (s.getWidth() == w && s.getHeight() == h) {
                    found = true;
                    break;
                }
            }
            if (!found) {
                return "{\"result\":\"error\",\"error\":\"unsupported " + w + "x" + h
                        + "\",\"hint\":\"GET /sizes\"}";
            }
            forcedSize = new Size(w, h);
            Log.i(TAG, "setres -> " + w + "x" + h);
            cameraHandler.post(this::restartSessionWithForced);
            return "{\"result\":\"ok\",\"size\":\"" + w + "x" + h + "\"}";
        } catch (Exception e) {
            return "{\"result\":\"error\",\"error\":"
                    + JSONObject.quote(String.valueOf(e)) + "}";
        }
    }

    private String setQuality(String query) {
        try {
            Map<String, String> q = parseQuery(query);
            String qs = q.get("q");
            if (qs == null) qs = q.get("quality");
            if (qs == null) {
                return "{\"result\":\"error\",\"error\":\"use ?q=1..100\"}";
            }
            int v = Integer.parseInt(qs.trim());
            if (v < 1 || v > 100) {
                return "{\"result\":\"error\",\"error\":\"q must be 1..100\"}";
            }
            jpegQuality = v;
            Log.i(TAG, "setquality -> " + v);
            cameraHandler.post(this::applyQuality);
            return "{\"result\":\"ok\",\"quality\":" + v + "}";
        } catch (Exception e) {
            return "{\"result\":\"error\",\"error\":"
                    + JSONObject.quote(String.valueOf(e)) + "}";
        }
    }

    private void applyQuality() {
        if ("camera1".equals(backend) && camera1 != null) {
            try {
                android.hardware.Camera.Parameters p = camera1.getParameters();
                p.setJpegQuality(jpegQuality);
                camera1.setParameters(p);
                Log.i(TAG, "Camera1 quality applied: " + jpegQuality);
            } catch (Exception e) {
                Log.w(TAG, "apply quality failed", e);
            }
        }
        // Camera2: 质量写在 CaptureRequest 中, 下一帧自动生效
    }

    // ------------------------------------------------------------------
    // /setcam: 相机 3A 自动/手动调节
    //   af=auto|off|macro|infinity | focus=屈光度(浮点)
    //   ae=auto|off | exposure=ns 或 "1/60" | iso=100..6400 | ev=-N..N
    //   wb=auto|incandescent|fluorescent|daylight|cloudy-daylight|shade|twilight
    //   reset=1 恢复全部默认(自动)
    // ------------------------------------------------------------------
    private String afName(int m) {
        switch (m) {
            case CameraMetadata.CONTROL_AF_MODE_OFF: return "off";
            case CameraMetadata.CONTROL_AF_MODE_MACRO: return "macro";
            case CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE: return "auto";
            default: return String.valueOf(m);
        }
    }

    private String wbName(int m) {
        switch (m) {
            case CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT: return "incandescent";
            case CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT: return "fluorescent";
            case CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT: return "daylight";
            case CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT: return "cloudy-daylight";
            case CameraMetadata.CONTROL_AWB_MODE_TWILIGHT: return "twilight";
            case CameraMetadata.CONTROL_AWB_MODE_SHADE: return "shade";
            default: return "auto";
        }
    }

    private int afFromName(String s) {
        if ("off".equals(s)) return CameraMetadata.CONTROL_AF_MODE_OFF;
        if ("macro".equals(s)) return CameraMetadata.CONTROL_AF_MODE_MACRO;
        return CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE;
    }

    private int wbFromName(String s) {
        if ("incandescent".equals(s)) return CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT;
        if ("fluorescent".equals(s)) return CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT;
        if ("daylight".equals(s)) return CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT;
        if ("cloudy".equals(s) || "cloudy-daylight".equals(s))
            return CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT;
        if ("twilight".equals(s)) return CameraMetadata.CONTROL_AWB_MODE_TWILIGHT;
        if ("shade".equals(s)) return CameraMetadata.CONTROL_AWB_MODE_SHADE;
        return CameraMetadata.CONTROL_AWB_MODE_AUTO;
    }

    /** 解析曝光值: 支持 "1/60"、"5000000"、纯数字 ns */
    private Long parseExposure(String s) {
        try {
            s = s.trim();
            if (s.contains("/")) {
                String[] p = s.split("/");
                double sec = Double.parseDouble(p[0].trim())
                        / Double.parseDouble(p[1].trim());
                return (long) (sec * 1_000_000_000.0);
            }
            return (long) Double.parseDouble(s);
        } catch (Exception e) {
            return null;
        }
    }

    /** 归一化区域 -> Camera2 MeteringRectangle (基于传感器 activeArray 坐标)。 */
    private MeteringRectangle buildMeteringRect() {
        float[] r = meteringRegion;
        if (r == null) return null;
        Rect a = activeArray;
        if (a == null || a.width() <= 0 || a.height() <= 0) return null;
        int cx = a.left + Math.round(r[0] * a.width());
        int cy = a.top + Math.round(r[1] * a.height());
        int cw = Math.max(1, Math.round(r[2] * a.width()));
        int ch = Math.max(1, Math.round(r[3] * a.height()));
        cx = Math.max(a.left, Math.min(a.left + a.width() - cw, cx));
        cy = Math.max(a.top, Math.min(a.top + a.height() - ch, cy));
        return new MeteringRectangle(cx, cy, cw, ch,
                MeteringRectangle.METERING_WEIGHT_MAX - 1);
    }

    /** 归一化区域 -> Camera1 Area (坐标系 -1000..1000)。 */
    private android.hardware.Camera.Area buildCamera1Area() {
        float[] r = meteringRegion;
        if (r == null) return null;
        int left = Math.round(r[0] * 2000f) - 1000;
        int top = Math.round(r[1] * 2000f) - 1000;
        int right = Math.round((r[0] + r[2]) * 2000f) - 1000;
        int bottom = Math.round((r[1] + r[3]) * 2000f) - 1000;
        left = Math.max(-1000, Math.min(999, left));
        top = Math.max(-1000, Math.min(999, top));
        right = Math.max(left + 1, Math.min(1000, right));
        bottom = Math.max(top + 1, Math.min(1000, bottom));
        return new android.hardware.Camera.Area(new android.graphics.Rect(left, top, right, bottom),
                900);
    }

    private String setCam(String query) {
        try {
            Map<String, String> q = parseQuery(query);
            if ("1".equals(q.get("reset"))) {
                afMode = CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE;
                aeMode = CameraMetadata.CONTROL_AE_MODE_ON;
                awbMode = CameraMetadata.CONTROL_AWB_MODE_AUTO;
                evIndex = null;
                exposureNs = null;
                iso = null;
                focusDist = null;
                meteringRegion = null;
                Log.i(TAG, "setcam -> reset all auto");
                applyCamCamera1();
                return "{\"result\":\"ok\",\"reset\":true}";
            }
            StringBuilder applied = new StringBuilder();
            String v;
            if ((v = q.get("af")) != null) {
                afMode = afFromName(v.toLowerCase().trim());
                if (!"off".equals(v.toLowerCase().trim())) focusDist = null;
                applied.append("af=").append(afName(afMode)).append(" ");
            }
            if ((v = q.get("focus")) != null) {
                // 手动对焦: 需要 AF off + 屈光度距离
                afMode = CameraMetadata.CONTROL_AF_MODE_OFF;
                focusDist = Float.parseFloat(v.trim());
                applied.append("focusDist=").append(focusDist).append(" ");
            }
            if ((v = q.get("ae")) != null) {
                aeMode = "off".equals(v.toLowerCase().trim())
                        ? CameraMetadata.CONTROL_AE_MODE_OFF
                        : CameraMetadata.CONTROL_AE_MODE_ON;
                if ("off".equals(v.toLowerCase().trim())) {
                    // 切手动曝光: 快门/ISO 沿用 AE 收敛值 (画面不变), 用户再微调
                    if (exposureNs == null) {
                        exposureNs = lastAeExposureNs > 0 ? lastAeExposureNs : 10_000_000L;
                    }
                    if (iso == null) {
                        iso = lastAeIso > 0 ? lastAeIso : 400;
                    }
                }
                applied.append("ae=").append(aeMode == CameraMetadata.CONTROL_AE_MODE_OFF
                        ? "off" : "auto").append(" ");
            }
            if ((v = q.get("exposure")) != null) {
                exposureNs = parseExposure(v);
                if (exposureNs == null) {
                    return "{\"result\":\"error\",\"error\":\"bad exposure, use ns or 1/60\"}";
                }
                applied.append("exposureNs=").append(exposureNs).append(" ");
            }
            if ((v = q.get("iso")) != null) {
                if ("auto".equalsIgnoreCase(v.trim())) {
                    // 回到自动: 清手动 ISO 与快门, AE 重新接管
                    iso = null;
                    exposureNs = null;
                    if (aeMode == CameraMetadata.CONTROL_AE_MODE_OFF) {
                        aeMode = CameraMetadata.CONTROL_AE_MODE_ON;
                    }
                    applied.append("iso=auto ");
                } else {
                    // 手动 ISO: AE off + 快门沿用 AE 收敛值, 用户在 App 上观察画面微调
                    if (aeMode == CameraMetadata.CONTROL_AE_MODE_ON) {
                        if (exposureNs == null) {
                            exposureNs = lastAeExposureNs > 0 ? lastAeExposureNs : 10_000_000L;
                        }
                        if (iso == null) {
                            iso = lastAeIso > 0 ? lastAeIso : 400;
                        }
                        aeMode = CameraMetadata.CONTROL_AE_MODE_OFF;
                    }
                    iso = Integer.parseInt(v.trim());
                    applied.append("iso=").append(iso).append(" ae=off ");
                }
            }
            if ((v = q.get("ev")) != null) {
                if ("auto".equalsIgnoreCase(v.trim())) {
                    evIndex = null;
                    applied.append("ev=auto ");
                } else {
                    int idx = Integer.parseInt(v.trim());
                    // 钳制到设备支持范围（CONTROL_AE_COMPENSATION_RANGE），
                    // 超出范围的值会被 HAL 静默忽略 → 表现为"调了没反应"。
                    if (evMin != 0 || evMax != 0) {
                        idx = Math.max(evMin, Math.min(evMax, idx));
                    }
                    evIndex = idx;
                    // EV 仅在 AE 自动模式下生效。若当前处于手动曝光
                    // (ae=off，例如先前设过手动 ISO)，必须切回自动，否则 EV 无效。
                    if (aeMode == CameraMetadata.CONTROL_AE_MODE_OFF) {
                        aeMode = CameraMetadata.CONTROL_AE_MODE_ON;
                        exposureNs = null;
                        iso = null;
                        applied.append("ae=auto(iso=auto, EV requires AE) ");
                    }
                    applied.append("ev=").append(evIndex).append(" ");
                }
            }
            if ((v = q.get("wb")) != null) {
                awbMode = wbFromName(v.toLowerCase().trim());
                applied.append("wb=").append(wbName(awbMode)).append(" ");
            }
            if ((v = q.get("region")) != null) {
                v = v.toLowerCase().trim();
                if ("clear".equals(v)) {
                    meteringRegion = null;
                    applied.append("region=clear ");
                } else {
                    // 归一化区域 x,y[,w,h] ∈ 0..1, 相对传感器画面; 默认 20% 方形
                    String[] p = v.split(",");
                    float nx = Float.parseFloat(p[0].trim());
                    float ny = Float.parseFloat(p[1].trim());
                    float nw = p.length > 2 ? Float.parseFloat(p[2].trim()) : 0.20f;
                    float nh = p.length > 3 ? Float.parseFloat(p[3].trim()) : 0.20f;
                    nx = Math.max(0f, Math.min(0.98f, nx));
                    ny = Math.max(0f, Math.min(0.98f, ny));
                    nw = Math.max(0.03f, Math.min(1f, nw));
                    nh = Math.max(0.03f, Math.min(1f, nh));
                    // 保证区域不超出画面
                    nx = Math.min(nx, 1f - nw);
                    ny = Math.min(ny, 1f - nh);
                    meteringRegion = new float[]{nx, ny, nw, nh};
                    // 区域点选后恢复连续自动对焦, 使 AF 立即以该区域收敛
                    if (afMode == CameraMetadata.CONTROL_AF_MODE_OFF && focusDist == null) {
                        afMode = CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE;
                    }
                    // 单帧 capture 模式必须显式触发 AF 扫描, 否则 region 不生效
                    if ("camera2".equals(backend)) {
                        afTriggerPending = true;
                    }
                    applied.append(String.format("region=%.3f,%.3f,%.3f,%.3f ",
                            meteringRegion[0], meteringRegion[1], meteringRegion[2], meteringRegion[3]));
                }
            }
            if ((v = q.get("camera")) != null) {
                v = v.trim();
                if (v.isEmpty()) {
                    cameraIdPref = null;    // 清除选择, 回到默认后置
                    applied.append("camera=default ");
                } else if (!v.equals(cameraIdPref)) {
                    cameraIdPref = v;
                    applied.append("camera=").append(v).append(" ");
                }
            }
            if (applied.length() == 0) {
                return "{\"result\":\"error\",\"error\":\"nothing to set\","
                        + "\"usage\":\"?af=auto|off|macro ?focus=diopter ?ae=auto|off "
                        + "?exposure=1/60|ns ?iso=N ?ev=N ?wb=auto|daylight|... "
                        + "?region=x,y,w,h|clear ?camera=<id> ?reset=1\"}";
            }
            Log.i(TAG, "setcam: " + applied);
            applyCamCamera1();
            // 切换摄像头需要重启相机以应用
            if (applied.toString().contains("camera=")) {
                cameraHandler.post(this::restartCamera);
            }
            return "{\"result\":\"ok\"," + "\"applied\":\"" + applied.toString().trim() + "\"}";
        } catch (Exception e) {
            return "{\"result\":\"error\",\"error\":"
                    + JSONObject.quote(String.valueOf(e)) + "}";
        }
    }

    /** Camera1 通道: 把 3A 设置映射到旧 API (能力有限: 对焦模式/白平衡/曝光补偿) */
    private void applyCamCamera1() {
        if (!"camera1".equals(backend) || camera1 == null) return;
        try {
            android.hardware.Camera.Parameters p = camera1.getParameters();
            if (afMode == CameraMetadata.CONTROL_AF_MODE_OFF && focusDist != null) {
                p.setFocusMode(android.hardware.Camera.Parameters.FOCUS_MODE_FIXED);
            } else if (afMode == CameraMetadata.CONTROL_AF_MODE_MACRO) {
                p.setFocusMode(android.hardware.Camera.Parameters.FOCUS_MODE_MACRO);
            } else {
                p.setFocusMode(android.hardware.Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
            }
            switch (awbMode) {
                case CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_INCANDESCENT);
                    break;
                case CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_FLUORESCENT);
                    break;
                case CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_DAYLIGHT);
                    break;
                case CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_CLOUDY_DAYLIGHT);
                    break;
                case CameraMetadata.CONTROL_AWB_MODE_TWILIGHT:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_TWILIGHT);
                    break;
                case CameraMetadata.CONTROL_AWB_MODE_SHADE:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_SHADE);
                    break;
                default:
                    p.setWhiteBalance(android.hardware.Camera.Parameters.WHITE_BALANCE_AUTO);
            }
            if (evIndex != null) {
                int v = evIndex;
                v = Math.max(p.getMinExposureCompensation(),
                        Math.min(p.getMaxExposureCompensation(), v));
                p.setExposureCompensation(v);
            }
            // 点选测光/对焦区域 (设备支持时; 不支持则自动忽略)
            try {
                android.hardware.Camera.Area area = buildCamera1Area();
                if (area != null && p.getMaxNumFocusAreas() > 0) {
                    java.util.List<android.hardware.Camera.Area> one =
                            java.util.Collections.singletonList(area);
                    p.setFocusAreas(one);
                    if (p.getMaxNumMeteringAreas() > 0) p.setMeteringAreas(one);
                } else {
                    p.setFocusAreas(null);
                    if (p.getMaxNumMeteringAreas() > 0) p.setMeteringAreas(null);
                }
            } catch (Exception ignored) {
            }
            camera1.setParameters(p);
        } catch (Exception e) {
            Log.w(TAG, "applyCamCamera1 failed", e);
        }
    }

    private void writeResponse(OutputStream out, int code, String contentType, byte[] body)
            throws Exception {
        String reason = (code == 200) ? "OK" : "Service Unavailable";
        out.write(("HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Cache-Control: no-cache\r\n\r\n").getBytes("UTF-8"));
        out.write(body);
    }
}
