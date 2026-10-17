# 听序 · Tingxu

**把声音整理成有序的记录。**

听序是一款面向 QS668 开放协议录音卡的本地语音工作台，将音频采集、转写、纪要、人物与词库管理集中在同一个 Web 界面中。也可以直接导入已有音频使用，无须先连接录音卡。

GitHub 仓库：`happynailinzz/tingxu` · 推荐英文仓库名：**`tingxu`**

> 当前为早期版本，主要面向 macOS。声纹匹配提供人工确认的建议，不用于身份认证。手机、平板和电脑采用响应式布局；默认服务仅允许本机访问。

## 功能

| 能力 | 说明 |
| --- | --- |
| 录音卡采集 | Type-C 挂载文件列表、选择采集、下载、安全弹出；蓝牙扫描、读取文件、选择导入和全部导入 |
| 本地音频档案 | 导入、搜索、筛选、批量处理、播放、下载、编辑显示标题、回收和恢复 |
| 本地 / 云端转写 | Paraformer、SenseVoice、自定义 FunASR 模型，或兼容 `/audio/transcriptions` 的服务 |
| 智能纪要 | 自定义兼容 `/chat/completions` 的模型服务及提示词，支持 Markdown 阅读和导出 |
| 任务进度 | 按采集、转写、总结阶段展示完成文件数、当前对象和执行日志；失败中止后续阶段 |
| 人物与声纹 | 人物档案、3–30 秒参考样本登记、本地 CAMPPlus 提取、候选匹配、人工确认与会话角色命名 |
| 热词与纠错 | 分组、权重、开关、批量添加、JSON 导入导出，以及可关闭的字面纠错规则 |
| 三端布局 | 手机底部导航与列表 / 详情切换；平板按宽度调整；电脑列表 / 阅读双栏 |
| macOS 插卡监听 | 自动打开管理台、单实例保护；可选登录常驻与退出重启配置 |

```text
录音卡（Type-C / BLE）或本地文件
                 ↓
           本地音频档案
                 ↓
      转写模型 + 热词 / 纠错
                 ↓
     完整转写 ← 人物关联 / 声纹建议
                 ↓
      自定义总结模型 + 提示词
                 ↓
        Markdown 纪要与会话档案
```

## 快速开始

需要 Python 3.11–3.13，推荐 3.11 或 3.12。音频转换需要 FFmpeg；Node.js 仅用于前端和协议测试，没有前端构建步骤。

```bash
git clone https://github.com/happynailinzz/tingxu.git
cd tingxu

# macOS 已安装 Homebrew 时
brew install python@3.12 ffmpeg
python3.12 -m venv funasr-env
source funasr-env/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python qs668-console.py
```

浏览器打开终端输出的地址，默认 `http://127.0.0.1:8765`。端口占用时会尝试后续端口。首次调用本地模型可能下载权重，下载完成后可离线转写。不同系统上的 PyTorch、torchaudio 和模型依赖兼容性需自行确认。

只使用文件管理和云端服务时，可以先安装基础依赖：

```bash
python -m pip install -r requirements-core.txt
python qs668-console.py
```

运行器优先使用项目下的 `funasr-env/bin/python`；不存在时使用当前 Python。设备蓝牙操作仍需安装 `bleak` 并授予系统蓝牙权限。

## 录音卡导入

**Type-C**：插入录音卡，在“录音卡”页选择文件并采集。当前按挂载卷中的 `record/` 目录识别设备。复制结束后使用安全弹出，再拔线。

**蓝牙**：打开设备蓝牙，在“录音卡 → 蓝牙导入”中依次扫描、读取文件列表、选择导入；也可导入全部。传输由运行工作台的电脑执行，不依赖浏览器 Web Bluetooth。多设备时需通过底层工具显式指定设备地址。BLE 文件传输比有线慢；导入和实时音频不可同时运行。

**已有文件**：在“录音档案”点击“导入音频”。支持 WAV、裸 Opus、OGG、MP3、M4A、FLAC、AAC、WebM、MP4，每个上传文件最多 1 GB。非 WAV 转写前转换成 16 kHz 单声道 WAV。裸 Opus 可能需要转换后才能试听。

## 配置模型与提示词

进入“模型与提示词”，保存设置后对新任务生效。

- **本地转写**：选择 Paraformer、SenseVoice，或填写兼容 FunASR AutoModel 的模型 ID / 本地路径。自定义模型不启用远程代码。
- **云端转写**：填写基础地址（通常到 `/v1`）、模型名、API Key。单文件在工作台中限 100 MB，服务商可能更低。普通 JSON 可能只有全文，逐段时间戳需接口支持 `verbose_json`。
- **总结模型**：推荐选择“自定义”，填写兼容 Chat Completions 的服务。留空提示词使用默认纪要模板，也可完全替换。默认配置模式兼容已有的 Hermes provider 配置；全新安装应先配置服务。

配置示例只使用占位值：

```text
基础地址：https://api.example.com/v1
转写模型：your-transcription-model
总结模型：your-chat-model
API Key：在页面中填写，不写入 README 或源码
```

模型是否可用以服务实际支持为准；保存设置不发送模型请求。纯本地转写不上传音频，云端转写上传所选音频，总结服务接收转写文本。

## 人物、声纹与热词

1. 在“声纹与人物”新建人物，选择音频相对时间中的 3–30 秒清晰单人片段，登记声纹。每人最多 5 个样本，可以试听和移除。
2. 在录音详情打开“人物与声纹”，手动关联说话人，或生成声纹匹配建议。**只有人工确认后的关联才用于阅读和后续总结**。
3. 说话人编号只属于当前录音。旧转写没有精确分段时，须用新版支持分离的模型重新转写，才能生成声纹建议。重新转写会重置当前人物关联，保留旧关联备份。
4. “热词与纠错”支持维护术语和字面规范词。热词偏置需要模型支持，SenseVoice 和当前云端转写路径不传热词偏置。字面纠错独立于模型，可用于本地和云端结果，但需自行审查规则，避免误改。

调整相似度阈值与候选差距可改变建议的保守程度；默认值未经通用数据集标定。多说话人重叠、噪声、短音频和跨设备录音会影响匹配效果。

## 数据与隐私

```text
webui/                       响应式页面，无 CDN 依赖
qs668-console.py             管理台、插卡监听与任务日志
qs668_web*.py                文件、设置及 API
qs668_personalization.py     人物、角色、词库与声纹元数据
qs668-web-task.py            转写 / 总结任务
qs668-voice-task.py          本地声纹登记与匹配
nextproto/tools/             上游协议与设备工具
nextproto/sdk/               上游 JavaScript SDK
nextproto/archive/           本机音频、转写、纪要（不提交）
sessions/                    本机会话、精确分段、人工关联（不提交）
```

密钥配置采用本机 `0600` 权限文件，页面接口只返回是否已保存密钥。声纹向量和参考音频均保存在本地。仓库忽略录音、模型、人物档案、词库、配置、日志、会话和临时任务；公开仓库不包含真实样本。

默认仅绑定 `127.0.0.1`，不能从另一台手机或平板直接访问，也不提供公网发布配置。三端适配指不同屏幕下的界面布局。任务列表在服务内存中，重启后清空；任务产物保存在磁盘。不要在执行模型或传输任务时重启。

## 登录常驻与开发验证

登录自启动与维护步骤见 [CONSOLE-SERVICE.md](CONSOLE-SERVICE.md)，详细使用说明见 [WEBUI-GUIDE.md](WEBUI-GUIDE.md)。

```bash
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/webui.test.cjs
node --check webui/app.js
node --check webui/personalization.js
cd nextproto && npm test
```

测试使用临时文件和模拟模型响应，不会上传真实音频或调用收费模型。声纹准确率、真实硬件传输与各屏幕视觉效果仍需要实际环境验收。

## 来源与许可证

协议工具和 SDK 基于 **NextProto 的 AI recorder card open protocol**，保留其原始 MIT 许可证及协议文档。上游项目：`nextproto1024/ai-recorder-card-open-protocol`；原产品 / 测试平台：`https://nextproto.top/qs668/`。

听序的本地工作台功能采用 MIT 许可证。模型权重、第三方依赖和外部服务遵循各自条款。
