# Hikvision / EZVIZ Bridge Camera for Scrypted

这是一个独立的 Scrypted 插件，用于把海康、萤石以及使用海康 HCNetSDK 私有协议的摄像头接入 Scrypted，并暴露为 HomeKit 摄像头。

它不是另一个 RTSP 代理。视频和摄像头麦克风走 RTSP，手机对讲音频通过 Hikbridge 的 `/talk` WebSocket 转成 PCM16，再由 `NET_DVR_VoiceComSendData` 推送到摄像头喇叭。

## 能做什么

- 多摄像头，每台单独配置
- RTSP 视频接入 Scrypted
- 摄像头麦克风随 RTSP 音频一起回传
- HomeKit 按住说话，音频下发到摄像头喇叭
- Hikbridge SDK 抓图
- 每台摄像头独立开关音频轨和 HomeKit 对讲
- 支持 TCP / UDP RTSP 传输
- 支持 Hikvision / EZVIZ 中与 207 能力相同的设备

## 前提条件

插件不直接连接摄像头的 8000 端口。它依赖已经安装并运行的 Hikbridge：

- Hikbridge add-on 或独立 Docker 正常启动
- `/health` 和 `/api/cameras` 能看到摄像头
- Hikbridge 能通过 `NET_DVR_VoiceComSendData` 向该摄像头喊话
- 摄像头 RTSP 主流包含麦克风音轨，才能把摄像头声音回传到 HomeKit

因此，“类似 207”的设备可以复用同一套配置；不同型号是否支持，取决于：

1. HCNetSDK 能否登录该摄像头
2. 摄像头固件是否开放语音对讲
3. RTSP 是否有音频轨
4. Hikbridge 是否为该摄像头配置了独立 id

插件不硬编码 207 或 225，任何满足上述条件的海康 / 萤石设备都可以添加。

## 实测设备

| 摄像头 | 视频 | 摄像头麦克风 | 手机喊话 | 说明 |
| --- | --- | --- | --- | --- |
| cam207 | 支持 | 支持 | 支持 | RTSP 有 `mpeg4-generic/16000/1` 音轨，Hikbridge `/talk` 可用 |
| cam225 | 支持 | 不支持 | 不支持 | RTSP 无音频轨，语音对讲能力为 0 |

cam225 这类设备建议关闭 `Camera microphone in stream` 和 `Enable HomeKit talkback`，只作为普通摄像头使用。

## 安装

### 方式一：从 npm 安装

如果这个包已经发布到 npm：

1. 打开 Scrypted 管理界面
2. 进入 `Plugins`
3. 点击 `Install Plugin`
4. 安装 `@wangling75/scrypted-hikbridge`
5. 后续有新版本时，在 Scrypted 的插件更新里更新

### 方式二：从本仓库直接部署

适合还没有发布 npm 包，或者需要立即测试的情况。编译发生在你的电脑或 CI，不需要在 Home Assistant 里编译。

```sh
cd scrypted-hikbridge
pnpm install
pnpm exec tsc --noEmit
pnpm run build
npx scrypted login 192.168.2.203:10443
pnpm run scrypted-deploy 192.168.2.203:10443
```

登录信息保存在当前用户的 `~/.scrypted/login.json`。Scrypted 默认 HTTPS 端口通常是 `10443`，HTTP 端口通常是 `11080`。如果部署脚本默认使用 `10443`，也可以显式写成 `192.168.2.203:10443`。

部署成功后，Scrypted 里会出现 `Hikvision / EZVIZ Bridge Camera` 插件设备。

## 添加摄像头

在 Scrypted 里打开插件设备，点击 `Add Device`，按下面填写。

### cam207 示例

| 配置项 | 值 |
| --- | --- |
| Camera name | Camera 207 |
| RTSP URL | `rtsp://admin:你的密码@192.168.2.207:554/h264/ch1/main/av_stream` |
| Hikbridge camera id | `cam207` |
| Hikbridge URL | `http://192.168.2.203:8654` |
| Hikbridge token | 你的 `auth_token` |
| Camera microphone in stream | 开 |
| Enable HomeKit talkback | 开 |
| Talkback sample rate | `16000` |
| RTSP transport | `tcp` |

### cam225 示例

| 配置项 | 值 |
| --- | --- |
| Camera name | Camera 225 |
| RTSP URL | `rtsp://admin:你的密码@192.168.2.225:554/h264/ch1/main/av_stream` |
| Hikbridge camera id | `cam225` |
| Hikbridge URL | `http://192.168.2.203:8654` |
| Hikbridge token | 你的 `auth_token` |
| Camera microphone in stream | 关 |
| Enable HomeKit talkback | 关 |

每台设备创建后，都可以在设备设置里单独修改这些项目，不需要重新创建。

## 接入 HomeKit

1. 在 Scrypted 中启用 HomeKit 插件
2. 把该摄像头设备加入 HomeKit
3. 在 iPhone 的家庭 App 里打开摄像头
4. 按住麦克风按钮说话

对讲链路是：

```text
iPhone 麦克风
  -> Scrypted Intercom
  -> 插件转成 PCM16 mono
  -> Hikbridge /talk
  -> HCNetSDK NET_DVR_VoiceComSendData
  -> 摄像头喇叭

摄像头麦克风
  -> RTSP 音频轨
  -> Scrypted
  -> HomeKit
```

从 `0.1.4` 开始，对讲音频按 20 ms PCM 小块发送，WebSocket 和
ffmpeg 并行启动，并且不再把 ffmpeg 的大块输出一次性推给网桥。
这样可以避免摄像头喇叭侧积累几秒钟音频，同时保留网桥忙时丢旧帧
的保护。HomeKit 本身仍会有一层系统音频缓冲，无法由插件完全消除。

## 配置说明

| 设置 | 作用 |
| --- | --- |
| `RTSP URL` | Scrypted 的视频和摄像头麦克风来源 |
| `Hikbridge camera id` | Hikbridge 中的摄像头 id，例如 `cam207` |
| `Hikbridge URL` | Hikbridge 的 HTTP 地址，默认 `http://127.0.0.1:8654` |
| `Hikbridge token` | Hikbridge 的 `auth_token` |
| `Camera microphone in stream` | RTSP 是否有音轨，没有就关闭 |
| `Enable HomeKit talkback` | 是否暴露 Scrypted Intercom 接口 |
| `Talkback sample rate` | 发送给摄像头的 PCM 采样率，默认 16000 |
| `RTSP transport` | 建议先用 `tcp` |

如果 Scrypted 和 Hikbridge 不在同一个网络命名空间，`127.0.0.1` 会连不上。你的 HA 地址是 `192.168.2.203`，所以优先使用：

```text
http://192.168.2.203:8654
```

## 常见问题

### HomeKit 没有麦克风按钮

检查该摄像头是否启用了 `Enable HomeKit talkback`。关闭后插件不会暴露 `Intercom` 接口，HomeKit 就不会显示对讲按钮。

### 按了说话没有声音

依次检查：

1. Hikbridge 的 `/health` 是否能访问
2. `Hikbridge camera id` 是否写对
3. `Hikbridge token` 是否和 add-on 配置一致
4. Scrypted 能否访问 `192.168.2.203:8654`
5. 摄像头是否支持 HCNetSDK 语音对讲

### 能听到摄像头声音，但手机说话没声音

说明 RTSP 音轨正常，问题在喊话链路。查看 Scrypted 插件日志里是否有：

- `Timed out connecting to Hikbridge /talk`
- `Hikbridge talk rejected`
- `talkback ffmpeg`

### 提示 busy

Hikbridge 的 `/talk` 对同一台摄像头只允许一个活动会话。HA Web UI、Node-RED 或另一个 HomeKit 会话可能正在占用该摄像头。

### 日志时间和实际时间不一致

这是 Hikbridge 容器时区问题，不是本插件问题。给 Hikbridge 设置 `TZ=Asia/Shanghai`，或在 add-on 配置里设置对应时区。

## 安全提醒

- RTSP URL 里包含摄像头账号密码
- Hikbridge token 是访问密码
- 不要把真实密码和 token 提交到 GitHub
- 建议给 Hikbridge 设置非空 `auth_token`

## 开发

```sh
pnpm install
pnpm exec tsc --noEmit
pnpm run build
```

构建产物：

```text
dist/plugin.zip
dist/main.nodejs.js
dist/main.nodejs.js.map
```

`dist/` 和 `node_modules/` 不提交到 Git，由 GitHub Actions 或本地构建生成。
