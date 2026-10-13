import sdk, {
    Camera,
    DeviceCreator,
    DeviceCreatorSettings,
    DeviceProvider,
    FFmpegInput,
    Intercom,
    MediaObject,
    RequestMediaStreamOptions,
    RequestPictureOptions,
    ResponseMediaStreamOptions,
    ResponsePictureOptions,
    ScryptedDeviceBase,
    ScryptedDeviceType,
    ScryptedInterface,
    ScryptedMimeTypes,
    Setting,
    Settings,
    SettingValue,
    VideoCamera,
} from "@scrypted/sdk";
import { randomBytes } from "node:crypto";
import { ChildProcess, spawn } from "node:child_process";
import WebSocket, { RawData } from "ws";

const { deviceManager, mediaManager } = sdk;

const DEFAULT_BRIDGE_URL = "http://127.0.0.1:8654";
const DEFAULT_TALK_RATE = 16000;
const DEFAULT_RTSP_TRANSPORT = "tcp";
const DEFAULT_VIDEO_CODEC = "h264";
const TALK_CONNECT_TIMEOUT_MS = 5000;

type IntercomState = {
    ffmpeg: ChildProcess;
    ws: WebSocket;
};

function asString(value: SettingValue | undefined): string | undefined {
    if (value === undefined || value === null)
        return undefined;
    return String(value);
}

function boolSetting(value: string | null | undefined, fallback = true): boolean {
    if (value === undefined || value === null || value === "")
        return fallback;
    return value !== "false";
}

function buildTalkFfmpegArgs(input: FFmpegInput, sampleRate: number): string[] {
    const args = ["-nostdin", "-loglevel", "error"];
    const inputArguments = input.inputArguments?.slice() || [];

    if (inputArguments.length) {
        args.push(...inputArguments);
        if (!inputArguments.includes("-i") && input.url)
            args.push("-i", input.url);
    }
    else if (input.url) {
        args.push("-i", input.url);
    }
    else if (input.urls?.length) {
        args.push("-i", input.urls[0]);
    }
    else {
        throw new Error("HomeKit intercom media does not contain an FFmpeg input");
    }

    // HomeKit sends AAC-ELD or Opus. Hikbridge expects raw PCM16 mono.
    args.push(
        "-vn",
        "-ac", "1",
        "-ar", String(sampleRate),
        "-f", "s16le",
        "-",
    );
    return args;
}

class HikBridgeCamera extends ScryptedDeviceBase implements Camera, VideoCamera, Intercom, Settings {
    private intercomState?: IntercomState;

    constructor(nativeId: string, public provider: HikBridgeProvider) {
        super(nativeId);
    }

    private get storedBridgeUrl(): string {
        return this.storage.getItem("bridgeUrl")
            || this.provider.storage.getItem("bridgeUrl")
            || DEFAULT_BRIDGE_URL;
    }

    private get storedBridgeToken(): string {
        return this.storage.getItem("bridgeToken")
            ?? this.provider.storage.getItem("bridgeToken")
            ?? "";
    }

    private get bridgeCameraId(): string {
        return this.storage.getItem("bridgeCameraId") || "";
    }

    private get rtspUrl(): string {
        return this.storage.getItem("rtspUrl") || "";
    }

    private get rtspTransport(): string {
        return this.storage.getItem("rtspTransport") || DEFAULT_RTSP_TRANSPORT;
    }

    private get audioEnabled(): boolean {
        return boolSetting(this.storage.getItem("audioEnabled"));
    }

    private get intercomEnabled(): boolean {
        return boolSetting(this.storage.getItem("intercomEnabled"));
    }

    private get talkSampleRate(): number {
        const value = Number(this.storage.getItem("talkSampleRate") || DEFAULT_TALK_RATE);
        if (!Number.isFinite(value) || value < 8000 || value > 48000)
            return DEFAULT_TALK_RATE;
        return Math.round(value);
    }

    private get videoCodec(): string {
        const value = (this.storage.getItem("videoCodec") || DEFAULT_VIDEO_CODEC).toLowerCase();
        return value === "h265" || value === "hevc" ? "h265" : "h264";
    }

    private get streamOptions(): ResponseMediaStreamOptions {
        const stream: ResponseMediaStreamOptions = {
            id: "main",
            name: "Main",
            container: "rtsp",
            // Scrypted uses this hint instead of probing, and it must match
            // what the camera really sends or the HomeKit plugin can pick the
            // wrong transcode path.
            video: { codec: this.videoCodec },
        };
        if (this.audioEnabled)
            stream.audio = {};
        return stream;
    }

    private bridgeHttpUrl(path: string): string {
        const base = new URL(this.storedBridgeUrl);
        const prefix = base.pathname.replace(/\/+$/, "");
        base.pathname = `${prefix}${path}`;
        base.search = "";
        return base.toString();
    }

    private bridgeWebSocketUrl(): string {
        const base = new URL(this.storedBridgeUrl);
        base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
        const prefix = base.pathname.replace(/\/+$/, "");
        base.pathname = `${prefix}/api/cameras/${encodeURIComponent(this.bridgeCameraId)}/talk`;
        base.search = "";
        if (this.storedBridgeToken)
            base.searchParams.set("token", this.storedBridgeToken);
        return base.toString();
    }

    private bridgeHeaders(): Record<string, string> | undefined {
        if (!this.storedBridgeToken)
            return undefined;
        return { "X-Auth-Token": this.storedBridgeToken };
    }

    private requireRtspUrl(): string {
        if (!this.rtspUrl)
            throw new Error("RTSP URL is not configured");
        return this.rtspUrl;
    }

    private requireBridgeCameraId(): string {
        if (!this.bridgeCameraId)
            throw new Error("Hikbridge camera id is not configured (for example cam207)");
        return this.bridgeCameraId;
    }

    getProvidedInterfaces(): string[] {
        const interfaces: string[] = [
            ScryptedInterface.Camera,
            ScryptedInterface.VideoCamera,
            ScryptedInterface.Settings,
        ];
        if (this.intercomEnabled)
            interfaces.push(ScryptedInterface.Intercom);
        return interfaces;
    }

    async getSettings(): Promise<Setting[]> {
        return [
            {
                key: "rtspUrl",
                title: "RTSP URL",
                type: "password",
                placeholder: "rtsp://admin:password@192.168.2.207:554/h264/ch1/main/av_stream",
                value: this.rtspUrl,
                description: "RTSP main stream. Keep the audio track enabled for camera-to-HomeKit audio.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Camera",
            },
            {
                key: "bridgeCameraId",
                title: "Hikbridge camera id",
                type: "string",
                placeholder: "cam207",
                value: this.bridgeCameraId,
                description: "The id shown by the Hikbridge /health or /api/cameras endpoint.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Camera",
            },
            {
                key: "bridgeUrl",
                title: "Hikbridge URL",
                type: "string",
                placeholder: DEFAULT_BRIDGE_URL,
                value: this.storedBridgeUrl,
                description: "Use the HA host address if Scrypted is not running with host networking.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Bridge",
            },
            {
                key: "bridgeToken",
                title: "Hikbridge token",
                type: "password",
                value: this.storedBridgeToken,
                description: "X-Auth-Token used by the Hikbridge add-on.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Bridge",
            },
            {
                key: "audioEnabled",
                title: "Camera microphone in stream",
                type: "boolean",
                value: String(this.audioEnabled),
                description: "Disable only when the camera RTSP stream has no audio track.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Audio",
            },
            {
                key: "intercomEnabled",
                title: "Enable HomeKit talkback",
                type: "boolean",
                value: String(this.intercomEnabled),
                description: "Adds the Scrypted Intercom interface. The bridge must support /talk for this camera.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Audio",
            },
            {
                key: "talkSampleRate",
                title: "Talkback sample rate",
                type: "number",
                range: [8000, 48000],
                value: String(this.talkSampleRate),
                description: "PCM sample rate sent to the Hikbridge /talk endpoint. 16000 is the default.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Audio",
            },
            {
                key: "rtspTransport",
                title: "RTSP transport",
                type: "string",
                choices: ["tcp", "udp"],
                value: this.rtspTransport,
                description: "TCP is more reliable for most Hikvision and EZVIZ cameras.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Camera",
            },
            {
                key: "videoCodec",
                title: "Video codec hint",
                type: "string",
                choices: ["h264", "h265"],
                value: this.videoCodec,
                description: "Codec of the RTSP stream above. Must match the camera: h264 for the 207 main stream, h265 for its sub stream.",
                group: "Hikvision / EZVIZ Bridge",
                subgroup: "Camera",
            },
        ];
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
        this.storage.setItem(key, value === undefined || value === null ? "" : String(value));

        if (key === "intercomEnabled" || key === "audioEnabled")
            await this.provider.updateDevice(this.nativeId!);

        if (key === "videoCodec" || key === "rtspUrl" || key === "rtspTransport")
            await this.onDeviceEvent(ScryptedInterface.VideoCamera, undefined);

        await this.onDeviceEvent(ScryptedInterface.Settings, undefined);
    }

    async getPictureOptions(): Promise<ResponsePictureOptions[]> {
        return [];
    }

    async takePicture(options?: RequestPictureOptions): Promise<MediaObject> {
        void options;
        const cameraId = this.requireBridgeCameraId();
        const response = await fetch(
            this.bridgeHttpUrl(`/api/cameras/${encodeURIComponent(cameraId)}/snapshot.jpg`),
            { headers: this.bridgeHeaders() },
        );
        if (!response.ok)
            throw new Error(`Hikbridge snapshot failed: HTTP ${response.status}`);

        const buffer = Buffer.from(await response.arrayBuffer());
        const contentType = response.headers.get("content-type") || "image/jpeg";
        return mediaManager.createMediaObject(buffer, contentType, { sourceId: this.id });
    }

    async getVideoStreamOptions(): Promise<ResponseMediaStreamOptions[]> {
        return [this.streamOptions];
    }

    async getVideoStream(options?: RequestMediaStreamOptions): Promise<MediaObject> {
        const rtspUrl = this.requireRtspUrl();
        const selected = options?.id && options.id !== "main" ? this.streamOptions : this.streamOptions;
        const ffmpegInput: FFmpegInput = {
            // `url` is what Scrypted's prebuffer resolves. Without it the
            // prebuffer session dies with "Invalid URL" and HomeKit playback
            // stutters. Scrypted appends `-i <url>` itself, so the input
            // arguments here must not contain `-i`.
            url: rtspUrl,
            inputArguments: ["-rtsp_transport", this.rtspTransport],
            mediaStreamOptions: selected,
        };
        return mediaManager.createFFmpegMediaObject(ffmpegInput, { sourceId: this.id });
    }

    private async openTalkSocket(): Promise<WebSocket> {
        const url = this.bridgeWebSocketUrl();
        const headers = this.bridgeHeaders();

        return new Promise<WebSocket>((resolve, reject) => {
            let settled = false;
            const ws = new WebSocket(url, headers ? { headers } : undefined);

            const finish = (callback: () => void) => {
                if (settled)
                    return;
                settled = true;
                clearTimeout(timer);
                callback();
            };

            const timer = setTimeout(() => {
                finish(() => {
                    ws.terminate();
                    reject(new Error("Timed out connecting to Hikbridge /talk"));
                });
            }, TALK_CONNECT_TIMEOUT_MS);

            ws.once("open", () => {
                finish(() => {
                    ws.send(JSON.stringify({ rate: this.talkSampleRate }));
                    resolve(ws);
                });
            });
            ws.once("error", (error) => {
                finish(() => reject(error));
            });
            ws.once("close", () => {
                finish(() => reject(new Error("Hikbridge /talk closed before it was ready")));
            });
        });
    }

    async startIntercom(media: MediaObject): Promise<void> {
        if (!this.intercomEnabled)
            throw new Error("HomeKit talkback is disabled for this camera");
        this.requireBridgeCameraId();

        await this.stopIntercom();

        const input = await mediaManager.convertMediaObjectToJSON<FFmpegInput>(
            media,
            ScryptedMimeTypes.FFmpegInput,
        );
        const ws = await this.openTalkSocket();

        let ffmpeg: ChildProcess;
        try {
            const args = buildTalkFfmpegArgs(input, this.talkSampleRate);
            this.console.log("starting Hikbridge talkback", args.join(" "));
            ffmpeg = spawn(await mediaManager.getFFmpegPath(), args, {
                stdio: ["ignore", "pipe", "pipe"],
            });
        }
        catch (error) {
            ws.terminate();
            throw error;
        }

        const state: IntercomState = { ffmpeg, ws };
        this.intercomState = state;

        ffmpeg.stdout?.on("data", (chunk: Buffer) => {
            if (this.intercomState !== state || ws.readyState !== WebSocket.OPEN)
                return;
            try {
                ws.send(chunk);
            }
            catch (error) {
                this.console.error("Hikbridge talk send failed", error);
                void this.stopIntercom();
            }
        });

        ffmpeg.stderr?.on("data", (chunk: Buffer) => {
            const text = chunk.toString().trim();
            if (text)
                this.console.log(`talkback ffmpeg: ${text}`);
        });

        ffmpeg.on("error", (error) => {
            this.console.error("talkback ffmpeg failed", error);
            void this.stopIntercom();
        });

        ffmpeg.on("close", () => {
            if (this.intercomState === state)
                void this.stopIntercom();
        });

        ws.on("message", (data: RawData) => {
            try {
                const message = JSON.parse(data.toString());
                if (message?.ok === false) {
                    this.console.error(`Hikbridge talk rejected: ${message.error || "unknown error"}`);
                    void this.stopIntercom();
                }
            }
            catch {
                // Binary/unknown frames are not expected from the current bridge.
            }
        });

        ws.on("error", (error) => {
            this.console.error("Hikbridge talk socket failed", error);
            if (this.intercomState === state)
                void this.stopIntercom();
        });

        ws.on("close", () => {
            if (this.intercomState === state)
                void this.stopIntercom();
        });
    }

    async stopIntercom(): Promise<void> {
        const state = this.intercomState;
        if (!state)
            return;
        this.intercomState = undefined;

        try {
            state.ffmpeg.kill("SIGKILL");
        }
        catch {
            // The child may have exited already.
        }

        try {
            if (state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify({ cmd: "stop" }));
                state.ws.close();
            }
            else {
                state.ws.terminate();
            }
        }
        catch {
            // The socket may have closed between the state check and this call.
        }
    }
}

class HikBridgeProvider extends ScryptedDeviceBase implements DeviceProvider, DeviceCreator {
    private devices = new Map<string, HikBridgeCamera>();
    private names = new Map<string, string>();

    constructor() {
        super();
        this.systemDevice = {
            deviceCreator: "Hikvision / EZVIZ Bridge Camera",
        };
    }

    getScryptedDeviceCreator(): string {
        return "Hikvision / EZVIZ Bridge Camera";
    }

    async getCreateDeviceSettings(): Promise<Setting[]> {
        return [
            {
                key: "newCamera",
                title: "Camera name",
                placeholder: "Front gate camera",
            },
            {
                key: "rtspUrl",
                title: "RTSP URL",
                type: "password",
                placeholder: "rtsp://admin:password@192.168.2.207:554/h264/ch1/main/av_stream",
            },
            {
                key: "bridgeCameraId",
                title: "Hikbridge camera id",
                type: "string",
                placeholder: "cam207",
            },
            {
                key: "bridgeUrl",
                title: "Hikbridge URL",
                type: "string",
                placeholder: DEFAULT_BRIDGE_URL,
            },
            {
                key: "bridgeToken",
                title: "Hikbridge token",
                type: "password",
                placeholder: "hikbridge2026",
            },
            {
                key: "audioEnabled",
                title: "Camera microphone in stream",
                type: "boolean",
                value: "true",
                description: "Enable only when the camera RTSP stream contains an audio track.",
            },
            {
                key: "intercomEnabled",
                title: "Enable HomeKit talkback",
                type: "boolean",
                value: "true",
                description: "Requires the Hikbridge /talk endpoint and a camera that supports voice talk.",
            },
            {
                key: "talkSampleRate",
                title: "Talkback sample rate",
                type: "number",
                range: [8000, 48000],
                value: "16000",
                description: "PCM sample rate sent to the camera speaker. 16000 is the default.",
            },
            {
                key: "rtspTransport",
                title: "RTSP transport",
                type: "string",
                choices: ["tcp", "udp"],
                value: DEFAULT_RTSP_TRANSPORT,
            },
            {
                key: "videoCodec",
                title: "Video codec hint",
                type: "string",
                choices: ["h264", "h265"],
                value: DEFAULT_VIDEO_CODEC,
                description: "Codec of the RTSP stream above. The 207 main stream is h264; its sub stream is h265.",
            },
        ];
    }

    async createDevice(settings: DeviceCreatorSettings): Promise<string> {
        const nativeId = randomBytes(4).toString("hex");
        const camera = this.getOrCreate(nativeId);
        const name = asString(settings.newCamera) || "Hikvision Camera";

        // Scrypted does not allocate device storage until the provider has announced
        // the native id. Register the base camera interfaces before writing settings.
        await deviceManager.onDeviceDiscovered({
            nativeId,
            name,
            type: ScryptedDeviceType.Camera,
            interfaces: [
                ScryptedInterface.Camera,
                ScryptedInterface.VideoCamera,
                ScryptedInterface.Settings,
            ],
        });

        const values: Array<[string, SettingValue | undefined]> = [
            ["rtspUrl", settings.rtspUrl],
            ["bridgeCameraId", settings.bridgeCameraId],
            ["bridgeUrl", settings.bridgeUrl],
            ["bridgeToken", settings.bridgeToken],
            ["audioEnabled", settings.audioEnabled],
            ["intercomEnabled", settings.intercomEnabled],
            ["talkSampleRate", settings.talkSampleRate],
            ["rtspTransport", settings.rtspTransport],
            ["videoCodec", settings.videoCodec],
        ];
        for (const [key, value] of values) {
            const text = asString(value);
            if (text)
                camera.storage.setItem(key, text);
        }

        this.names.set(nativeId, name);
        await this.updateDevice(nativeId, name);
        return nativeId;
    }

    async getDevice(nativeId: string): Promise<HikBridgeCamera> {
        return this.getOrCreate(nativeId);
    }

    async releaseDevice(id: string, nativeId: string): Promise<void> {
        void id;
        const camera = this.devices.get(nativeId);
        if (camera)
            await camera.stopIntercom();
        this.devices.delete(nativeId);
        this.names.delete(nativeId);
    }

    getDeviceName(nativeId: string): string {
        return this.names.get(nativeId)
            || this.devices.get(nativeId)?.name
            || "Hikvision Camera";
    }

    async updateDevice(nativeId: string, name?: string): Promise<void> {
        const camera = this.getOrCreate(nativeId);
        if (name)
            this.names.set(nativeId, name);

        await deviceManager.onDeviceDiscovered({
            nativeId,
            name: name || this.names.get(nativeId) || camera.name || "Hikvision Camera",
            type: ScryptedDeviceType.Camera,
            interfaces: camera.getProvidedInterfaces(),
        });
    }

    private getOrCreate(nativeId: string): HikBridgeCamera {
        let camera = this.devices.get(nativeId);
        if (!camera) {
            camera = new HikBridgeCamera(nativeId, this);
            this.devices.set(nativeId, camera);
        }
        return camera;
    }
}

export default new HikBridgeProvider();
