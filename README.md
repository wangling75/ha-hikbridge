# Hikvision / EZVIZ 云台接入 Home Assistant

让只开放 **TCP 8000 私有协议**（没有 HTTP / ONVIF / ISAPI）的 Hikvision /
EZVIZ OEM 摄像头在 Home Assistant 里实现云台控制、快照和喇叭语音播放。

不需要改固件。摄像头本身已经在 8000 端口上跑着完整的 HCNetSDK 服务，
这套方案只是用官方 SDK 去调它。

支持两种部署方式：

- **Home Assistant add-on**：适合 Home Assistant OS / Supervised，带侧边栏、ingress、开机自启和商店更新。
- **纯 Docker**：适合 HA Container、群晖、威联通、普通 Linux 或 ESXi 里的独立虚拟机；只运行同一个镜像，HA 集成通过 HTTP 连接它。

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

> 不想用 HA add-on 也可以直接看下面的「纯 Docker 部署」。两种方式运行的是同一个镜像，API、Web UI、云台和喊话功能一致。

## 目录

| 路径 | 作用 |
| --- | --- |
| `hikbridge/` | HAOS 插件（Debian + HCNetSDK + HTTP 引擎） |
| `custom_components/hikbridge/` | HA 自定义集成（摄像头、按钮、服务） |
| `scrypted-hikbridge/` | Scrypted 插件（多摄像头、HomeKit 双向对讲） |
| `repository.json` | 声明这是一个 HA 加载项仓库 |
| `hacs.json` | 声明 HACS 集成仓库元数据 |
| `options.example.json` | 纯 Docker 部署的配置示例 |
| `docker-compose.example.yml` | 纯 Docker 部署的 Compose 示例 |

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

3. 仓库通过 GitHub Actions 构建并发布 amd64 镜像到 GHCR；HAOS 添加仓库后会拉取
   `ghcr.io/wangling75/amd64-hikbridge:<版本号>`。也可以完全不用 HAOS，直接按
   「纯 Docker 部署」运行同一个镜像。

4. 回到 HA：

   **设置 → 加载项 → 加载项商店 → 右上角三个点 → 仓库 → 粘贴
   `https://github.com/<你的用户名>/ha-hikbridge` → 添加**

   如果提示 `StoreManager.add_repository blocked ... supervisor needs to be
   updated first`，先到 **设置 → 系统 → 更新** 把 Supervisor 更新到最新，
   必要时也更新 Home Assistant OS，然后重试。

   「Hikvision PTZ Bridge」会出现在商店里，直接安装、填参数、启动。
   首次安装会拉取 GHCR 镜像，需要几分钟。

### 许可提醒

`hikbridge/hcnetsdk/` 里是 **Hikvision 官方的闭源 SDK 二进制**（约 29 MB）。
公开分发前请自行确认这符合 Hikvision 的许可条款；拿不准就用**私有仓库**，
或者把 SDK 从仓库里排除、改成本地构建时再放进去。

## 二、纯 Docker 部署（不使用 HA add-on）

这一节适合 HA Container、群晖、威联通、普通 Linux 或 ESXi 里的独立 Docker 主机。
插件镜像本身不依赖 Supervisor，HA add-on 只是多了一层商店、ingress 和侧边栏。

### 1. 获取镜像

直接用 GHCR 镜像：

```bash
docker pull ghcr.io/wangling75/amd64-hikbridge:0.2.8
```

或者本地构建：

```bash
git clone https://github.com/wangling75/ha-hikbridge.git
cd ha-hikbridge
docker build -t hikbridge:0.2.8 ./hikbridge
```

镜像只提供 `linux/amd64`。在 ARM 主机上需要模拟运行，不推荐长期这样用。

### 2. 准备 options.json

仓库根目录的 `options.example.json` 可以直接复制成 `options.json`：

```bash
cp options.example.json options.json
```

然后修改摄像头 IP、账号密码和 `auth_token`。示例：

```json
{
  "cameras": [
    {
      "id": "cam207",
      "name": "Camera 207",
      "camera_ip": "192.168.2.207",
      "camera_port": 8000,
      "username": "admin",
      "password": "你的摄像头密码",
      "channel": 1,
      "rtsp_url": "rtsp://admin:你的摄像头密码@192.168.2.207:554/h264/ch1/main/av_stream"
    },
    {
      "id": "cam225",
      "name": "Camera 225",
      "camera_ip": "192.168.2.225",
      "camera_port": 8000,
      "username": "admin",
      "password": "你的摄像头密码",
      "channel": 1
    }
  ],
  "speak_sample_rate": "16000",
  "auth_token": "CHANGE_ME_TOKEN",
  "ssl": false,
  "ssl_port": 8655
}
```

`auth_token` 是 Web UI 和 API 的密码，不是摄像头密码。`speak_sample_rate` 是后备输入采样率；插件登录后会优先使用摄像头实际声明的编码和采样率。
`ssl: true` 时插件会在 `ssl_port`（默认 8655）额外开一个 HTTPS 监听，用自签证书，浏览器只需确认一次就能用网页「点击开始喊话」；`8654` 的明文 HTTP 保持不变，HA 侧边栏和 Scrypted 都不受影响。网页实时画面通过 `/live.mp4` 把 RTSP 视频和音频转给浏览器，点击「开启声音」即可听到摄像头麦克风。
如果 RTSP 密码包含 `@`、`:`、`/` 等字符，需要在 RTSP URL 里做 URL 编码。

### 3. 启动容器

```bash
docker run -d \
  --name hikbridge \
  --restart unless-stopped \
  -p 8654:8654 \
  -e HIK_TIMEZONE=Asia/Shanghai \
  -v "$PWD/options.json:/data/options.json:ro" \
  ghcr.io/wangling75/amd64-hikbridge:0.2.8
```

查看日志：

```bash
docker logs -f hikbridge
```

验证：

```bash
curl -s -H 'X-Auth-Token: CHANGE_ME_TOKEN' http://127.0.0.1:8654/health
```

Web UI：

```text
http://<Docker主机IP>:8654/
```

如果设置了 `auth_token`，浏览器会先要求输入这个密码。

### 4. docker-compose 示例

仓库根目录提供了 `docker-compose.example.yml`，复制成 `docker-compose.yml` 后可直接使用：

```bash
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

更新镜像：

```bash
docker compose pull
docker compose up -d
```

### 5. 接入 HA 集成

纯 Docker 只是替代 add-on，HA 里仍然要安装 `custom_components/hikbridge` 集成。
添加集成时：

| 字段 | 值 |
| --- | --- |
| 主机 | Docker 主机 IP |
| 端口 | `8654` |
| 鉴权令牌 | 和 `options.json` 的 `auth_token` 一致 |
| RTSP 地址 | 对应摄像头 RTSP 地址 |

如果 HA Core 和 Docker 在同一台机器上，主机可以填 `127.0.0.1`；如果不在同一台机器上，填 Docker 主机的局域网 IP，例如 `192.168.2.153`。端口要确保防火墙放行。

### 6. HA add-on 和纯 Docker 的区别

| 方式 | 优点 | 限制 |
| --- | --- | --- |
| HA add-on | 商店安装、自动启动、ingress 侧边栏、Supervisor 管理 | 只能在 HAOS / Supervised 使用 |
| 纯 Docker | 任何支持 Docker 的 Linux 主机都能跑 | 没有 HA 侧边栏，需要自己维护 compose/更新 |

## 三、安装 HA 集成

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
| RTSP 地址 | `rtsp://admin:YOUR_PASSWORD@192.168.2.207:554/h264/ch1/main/av_stream` |
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
`NET_DVR_VoiceComSendData` 推给摄像头喇叭。插件登录后会查询摄像头声明的
对讲编码；207 实测为 AAC/16 kHz，插件会自动用内置 ffmpeg 编成 ADTS 单帧，
不再把 PCM 直接塞进只接受 AAC 的通道。

> TTS 引擎如果不是输出 WAV（比如 Google Translate 输出 MP3），集成会尝试
> 调用 `ffmpeg` 转码；HAOS 的 Core 容器默认没有 ffmpeg，所以建议用
> **Piper**（本地 TTS，输出 WAV），或者用 `media_url` 传现成的 WAV。

## Scrypted / HomeKit 双向对讲

仓库里的 `scrypted-hikbridge/` 是一个独立的 Scrypted 插件，用于把同一批
海康 / 萤石摄像头接入 HomeKit，并实现按住说话。

- 视频和摄像头麦克风走 RTSP
- 手机对讲音频通过 Hikbridge 的 `/talk` 下发到摄像头喇叭
- 多摄像头独立配置，可分别控制对讲开关
- 207 这类 RTSP 有音轨且支持语音对讲的设备可用
- 225 这类无 RTSP 音轨、对讲能力为 0 的设备只能做视频

完整安装、部署和排错说明见：
[`scrypted-hikbridge/README.md`](scrypted-hikbridge/README.md)。

## 插件 HTTP API

```
GET  /health
GET  /snapshot.jpg
POST /ptz                    {"direction": "right", "duration": 0.6}
POST /ptz/stop
POST /speak                  body = WAV file
POST /speak_raw?rate=8000    body = raw PCM16, encoded for the camera
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
