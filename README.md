# Hikvision / EZVIZ 云台接入 Home Assistant

让只开放 **TCP 8000 私有协议**（没有 HTTP / ONVIF / ISAPI）的 Hikvision /
EZVIZ OEM 摄像头在 Home Assistant 里实现云台控制、快照和喇叭语音播放。

不需要改固件。摄像头本身已经在 8000 端口上跑着完整的 HCNetSDK 服务，
这套方案只是用官方 SDK 去调它。

## 为什么需要一个 add-on

Home Assistant OS（你 ESXi 里的那台）把 Home Assistant Core 跑在 **Alpine
(musl)** 容器里，而 Hikvision 的 SDK 是 **glibc** 动态库，直接放进
`custom_components` 是加载不起来的。

所以拆成两层：

```
┌──────────────────────────── HAOS 虚拟机 ────────────────────────────┐
│                                                                     │
│  ┌──────────────────┐   HTTP 127.0.0.1:8654   ┌──────────────────┐  │
│  │ HA Core (Alpine) │ ◄─────────────────────► │ add-on (Debian)  │  │
│  │ custom_component │                         │ HCNetSDK + ctypes│  │
│  └──────────────────┘                         └────────┬─────────┘  │
└─────────────────────────────────────────────────────────┼───────────┘
                                                          │ TCP 8000
                                                   ┌──────▼───────┐
                                                   │  摄像头       │
                                                   │ 192.168.2.207│
                                                   └──────────────┘
```

add-on 用 Debian 基础镜像，SDK 在里面正常工作；它和 HA Core 都用 host
网络，所以 HA 通过 `127.0.0.1:8654` 就能访问到。整个过程 HA 自己管理容器，
你不需要碰 Docker。

## 目录

| 路径 | 作用 |
| --- | --- |
| `hikbridge/` | HAOS 插件（Debian + HCNetSDK + HTTP 引擎） |
| `custom_components/hikbridge/` | HA 自定义集成（摄像头、按钮、服务） |
| `repository.json` | 声明这是一个 HA 加载项仓库 |

## 一、托管到 GitHub

这个目录已经是一个可直接使用的 **HA 加载项仓库**。步骤：

1. 在 GitHub 新建一个仓库，比如 `ha-hikbridge`（**建议设为私有**，见下面的
   许可提醒），把本目录内容推上去：

   ```sh
   cd ha
   git init -b main
   git add .
   git commit -m "Hikvision PTZ bridge add-on + integration"
   git remote add origin git@github.com:<你的用户名>/ha-hikbridge.git
   git push -u origin main
   ```

2. 把 `YOUR_GITHUB_USER` 全部替换成你的用户名，共 3 处：

   - `repository.json` 里的 `url`
   - `hikbridge/config.yaml` 里的 `url` 和 `image`
   - `custom_components/hikbridge/manifest.json` 里的 `documentation` / `issue_tracker`

   ```sh
   grep -rl YOUR_GITHUB_USER . | xargs sed -i '' 's/YOUR_GITHUB_USER/你的用户名/g'
   ```

   改完再 push 一次。

3. 这个仓库不使用预构建镜像。HAOS 添加仓库后会在本机按 `hikbridge/Dockerfile`
   自动构建 amd64 插件，不依赖 GHCR，也不需要在电脑上装 Docker。

4. 回到 HA：

   **设置 → 加载项 → 加载项商店 → 右上角三个点 → 仓库 → 粘贴
   `https://github.com/<你的用户名>/ha-hikbridge` → 添加**

   如果提示 `StoreManager.add_repository blocked ... supervisor needs to be
   updated first`，先到 **设置 → 系统 → 更新** 把 Supervisor 更新到最新，
   必要时也更新 Home Assistant OS，然后重试。

   「Hikvision PTZ Bridge」会出现在商店里，直接安装、填参数、启动。
   首次安装会在 HAOS 里构建镜像，需要几分钟。

### 许可提醒

`hikbridge/hcnetsdk/` 里是 **Hikvision 官方的闭源 SDK 二进制**（约 29 MB）。
公开分发前请自行确认这符合 Hikvision 的许可条款；拿不准就用**私有仓库**，
或者把 SDK 从仓库里排除、改成本地构建时再放进去。

## 二、安装 HA 集成

把 `custom_components/hikbridge` 整个目录放到 HA 的 `/config/custom_components/hikbridge`
（Samba / SSH 加载项），重启 HA。

> 也可以用 **HACS**：HACS → 集成 → 右上角三个点 → 自定义存储库 →
> 填仓库地址、类别选「集成」。

然后 **设置 → 设备与服务 → 添加集成 → Hikvision PTZ Bridge**：

**设置 → 设备与服务 → 添加集成 → Hikvision PTZ Bridge**

| 字段 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `8654` |
| 鉴权令牌 | 留空（除非你在插件里设了） |
| RTSP 地址 | `rtsp://admin:CAHGIM@192.168.2.207:554/h264/ch1/main/av_stream` |
| TTS 实体 | 例如 `tts.piper`，用于语音播放 |
| 按钮点动时长 | `0.6` 秒 |

## 会生成什么

设备 **Hikvision PTZ**，包含：

- `camera.hikvision_ptz` —— 实时画面走 RTSP，快照走 SDK（1280×720 JPEG）
- `button.*_tilt_up` / `tilt_down` / `pan_left` / `pan_right` —— 点动
- `button.*_stop` —— 立即停止

## 服务

```yaml
# 云台点动
service: hikbridge.move
target:
  entity_id: button.hikvision_ptz_pan_left
data:
  direction: left
  duration: 0.8

# 停止
service: hikbridge.stop

# 文字转语音，从摄像头喇叭放出来
service: hikbridge.speak
data:
  text: "有人在家吗？"
  tts_entity: tts.piper

# 或者直接放一个 WAV
service: hikbridge.speak
data:
  media_url: "http://192.168.2.153:8123/local/ding.wav"
```

`hikbridge.speak` 的实现路径：HA 的 TTS 实体生成音频 → 集成把 WAV 解码成
16-bit 单声道 PCM 并按摄像头要求的采样率重采样 → POST 给插件 → 插件用
`NET_DVR_VoiceComSendData` 推给摄像头喇叭。

> TTS 引擎如果不是输出 WAV（比如 Google Translate 输出 MP3），集成会尝试
> 调用 `ffmpeg` 转码；HAOS 的 Core 容器默认没有 ffmpeg，所以建议用
> **Piper**（本地 TTS，输出 WAV），或者用 `media_url` 传现成的 WAV。

## 插件 HTTP API

```
GET  /health
GET  /snapshot.jpg
POST /ptz                    {"direction": "right", "duration": 0.6}
POST /ptz/stop
POST /speak                  body = WAV file
POST /speak_raw?rate=8000    body = raw PCM, forwarded verbatim
```

## 这台摄像头实测能力

| 功能 | 结果 |
| --- | --- |
| 上 / 下 / 左 / 右 | ✅ 可用（SDK 命令 21–24） |
| 对角线 | ❌ 返回 err 11 |
| 预置位（preset） | ❌ 返回 err 11 |
| 变焦 / 对焦 / 光圈 | ❌ 返回 err 11（无光学变焦） |
| SDK 快照 | ✅ 1280×720 JPEG |
| 喇叭播放（TTS） | ✅ 接口调用成功，实际出声待确认 |
| 摄像头麦克风回传 | ✅ 对讲链路建立后持续收到音频帧 |
| RTSP 反向通道 | ❌ 没有 backchannel，所以 go2rtc / 原生 RTSP 对讲走不通 |

## 关于「语音对讲」

- **HA 本身**没有对讲（双向语音）能力，HA 的 HomeKit Bridge 也只做单向音频。
- 这套方案实现的是 **HA → 摄像头喇叭**（TTS、提示音），以及
  **摄像头麦克风 → 插件**（音频帧已经收得到，但 HA 侧没有现成的双向通道）。
- 要做真正的 **双向对讲**（手机说话 → 摄像头喇叭），可行路线是：
  插件把收到的麦克风音频转成 RTSP/WebRTC 流，再由 **Scrypted** 暴露成
  HomeKit 摄像头——Scrypted 的 HomeKit 插件支持双向语音。
  这一步需要额外写音频转发，属于后续工作。

## 已知限制

- 插件只在 `amd64` 架构构建（SDK 只有 x86_64 版本）。
- 摄像头同时只容忍一个控制会话，插件内部用长连接 + 锁串行化所有操作。
- `NET_DVR_VoiceComSendData` 单次最多约 1600 字节，引擎已自动分片并按实时
  速率推送。
- 语音链路需要 ALSA 有一个可用的播放设备；插件用 `ALSA_CONFIG_PATH`
  指向自带的 null 设备配置，所以容器里没有声卡也能工作。
