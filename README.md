# R18_video —— 本地 AI 漫剧生产框架

创意 → **LLM 剧本/分镜** → **资产** → **关键帧** → **运镜** → **本地 H3 视频** → **合成** 的全流水线。
每个环节一个"段"（stage），段内按"镜"（shot）为单位记 manifest，**断点续跑**：完成即跳过、失败可重入、输入变化自动 stale 重跑。

设计文档：`docs/FRAMEWORK_DESIGN.md`（段语义、退出码、seed 派生规则等以它为准）。

> 所有命令默认在**仓库根**执行（`python -m pipeline` 的包发现要求 cwd=仓库根，或 PYTHONPATH 含仓库根；配置里的相对路径解析不受 cwd 影响）。
> Git Bash 示例：`cd /e/projects/推理/R18_video`

---

## 0. 当前状态（2026-10-27 冒烟记录）

如实说明：**干跑/模拟链路全绿，真实 GPU 渲染尚未发生过**。评估可用性时以此为前提。

| 冒烟项 | 结果 |
| --- | --- |
| 主链路（`projects/samples/demo/`，2 镜双通道） | **到达 final，全链路通过，产物 11 个**（成片 `final/samples_demo.mp4`、SRT、contact sheet、各段产物与 manifest） |
| 编剧阶段（`projects/demo_screenplay/`） | **到达 llm-full**：创意→剧本.md 与剧本→shots.yaml 两步均为真实 LLM 生成（本地 llama.cpp），schema 校验实跑通过（非校验器降级模式）。跳过项：deepseek-api 端点（备选②）未启用——本地 llama.cpp 首选已接通，无需降级 |
| `python selftest.py`（主链路，dry） | 退出码 **0** |
| `python selftest.py`（扩展编剧后，dry） | 退出码 **0** |
| `python selftest.py --with-server`（README 校验时复跑，2026-10-27） | 退出码 **0**（PASS=116 FAIL=0；此时 8188 已在线，含 H3 三节点在列断言） |

环境实测（2026-10-27 `python -m pipeline doctor`）：8188 在线（ComfyUI 0.37.0），`MiniMaxH3ImageToVideo` / `TextEncodeQwenImage21` / `UnetLoaderGGUF` 三节点已装，十件权重全部在盘且过大小门，ffmpeg 8.1.1 在 PATH，Blender 在 `E:\tools\blender\blender.exe`。交接时的"服务器停机"状态已不复现——但服务器由人拉起（`run_nvidia_gpu.bat`），**随时可能停机**，跑渲染段前先用 doctor 确认。

备注：冒烟所用 llama-server（Bonsai2-27B，CPU 模式，127.0.0.1:8010）可能仍在后台运行——空闲只占内存不占 GPU/CPU，可供后续冒烟复用；不用可自行结束该进程。

**尚未跑过的真实路径**（首跑标定点，先小后大）：
- **H3 视频整段 B 级**：本机从未成功渲染过一次 H3。节点已装齐、六件权重在盘，但 124 帧网格、双 VAE 音频轨、时长口径全部待首跑确认——**先 `--only S1` 试产**。
- **camera 模式A 真渲**：Blender EEVEE 渲染 + ffmpeg 合 mp4 未跑（首次触发约 380MB Blender 便携包下载且占 GPU）。
- **assets / keyframes 渲染路径**：未经真实 ComfyUI 验证（用 FakeComfy/干跑验证到提交参数层）；首跑标定点=1024×1024 方图。

---

## 1. 框架总览

```
创意（premise 一句话）
  │  python -m pipeline run <项目> --stage screenplay …（需配置 LLM）
  ▼
剧本.md ──breakdown──▶ characters.yaml / shots.yaml / subtitles.yaml（人工也可直接手填这三份）
  │
  ▼  assets      角色三视图参考图（ComfyUI，双通道；人工挑候选或 --auto-pick）
  ▼  keyframes   每镜首/末帧（photo 通道 turbo 9 步；末帧 edit 保身份 / fresh 两模式）
  ▼  camera      运镜：模式A（Blender，端点图或整段视频）/ 模式B（词库 camdesc 直通 H3，默认）
  ▼  video       本地 MiniMax H3 首→末帧生视频（124..362 帧网格，5.167–15.083s）
  ▼  final       ffmpeg 拼接、字幕烧录、标题卡、ambience 床轨、contact sheet → 成片 mp4
```

- 段依赖链：`assets → keyframes → camera → video → final`（`--stage all` 按序执行，**不含段0 编剧**）。
- 段0 编剧三命令：`--stage screenplay-write`（创意→剧本.md）、`--stage breakdown`（剧本→三份 yaml；未配置 LLM 时=三份人工输入的校验器）、`--stage screenplay`（= write → breakdown 串联）。
- 产物布局：`projects/<名>/{剧本.md,剧本模板.md,project.yaml,characters.yaml,shots.yaml,subtitles.yaml,assets/,keyframes/,camera/,video/,final/}`，各段 manifest（`*_manifest.json`）同目录记进度。
- 退出码：`0` 成功；`1` 失败/输入错误；`2` ComfyUI 不可达（assets/keyframes/video 预检即退，不让任务空转）；`3` 所选段执行器未落地。
- camera 与 final 段不依赖 ComfyUI（Blender+ffmpeg / 纯 ffmpeg），服务器停机也能跑。

---

## 2. 新项目接入（五步）

### 第 1 步 · 脚手架

```bash
python -m pipeline new 我的剧 --style photo     # --style photo|anime = 项目默认通道
```

生成 `projects/我的剧/`：`project.yaml`（按通道预填画风锚点）、`剧本模板.md`、`characters.yaml`、`shots.yaml`、`subtitles.yaml` 与五个空产物目录。
**注意**：脚手架 `characters.yaml` 里的角色锚点是遗留示例内容，接入新项目时务必整体替换为**成年（18+）**角色设定再使用（见 §8 内容边界）。

### 第 2 步 · 编剧（二选一）

**A. LLM 自动编剧**（先按 §3 配置 `llm:` 段）：

```bash
python -m pipeline run 我的剧 --stage screenplay \
    --premise "雨夜侦探追寻真相的悬疑短剧" --style-note "悬疑·雨夜·治愈" \
    --shots 8 --total-s 45
```

覆盖前自动备份 `*.bak`。已配置 LLM 但服务不可达时**按失败退出（exit 1），不静默降级**——生成内容即产物，静默换模板会误导。

**B. 手填模板**：照 `剧本模板.md` 的说明写人物/分镜，把字段誊进 `characters.yaml` / `shots.yaml` / `subtitles.yaml` 三份文件，然后跑校验器：

```bash
python -m pipeline run 我的剧 --stage breakdown --shots 8 --total-s 45
```

（未配置 LLM 时 breakdown 就是三份人工 yaml 的校验器；`--shots/--total-s` 可省，省了则只约束单镜 5–15s。）

### 第 3 步 · 预检 + 逐段执行

```bash
python -m pipeline doctor                 # 环境详查：路径/十件权重/ffmpeg/Blender/服务器与 H3 节点
python -m pipeline run 我的剧 --stage assets --dry-run      # 计划预览（不渲染不触网不写盘）
python -m pipeline run 我的剧 --stage keyframes --dry-run
python -m pipeline run 我的剧 --stage camera --dry-run      # camera 段不需要 ComfyUI
python -m pipeline run 我的剧 --stage video --dry-run
python -m pipeline run 我的剧 --stage final --dry-run
```

正式跑（assets/keyframes/video 需要 ComfyUI 已拉起，见 §6）：

```bash
python -m pipeline run 我的剧 --stage assets          # 三视图候选；默认人工挑：
python -m pipeline.stages.assets 我的剧 --pick 主角:c1 # 挑选关口；或 --auto-pick 取第一候选
python -m pipeline run 我的剧 --stage keyframes
python -m pipeline run 我的剧 --stage camera
python -m pipeline run 我的剧 --stage video --only S1 # H3 首跑务必单镜试产
python -m pipeline run 我的剧 --stage final           # 成片落 projects/我的剧/final/
```

也可以一条到底（段0 不在内）：`python -m pipeline run 我的剧 --stage all`。任一段失败即停（下游依赖其产物），修好后重跑同一命令即可。

常用旗标（`run` 全量见 `python -m pipeline run --help`）：

| 旗标 | 作用 |
| --- | --- |
| `--dry-run` | 只打印计划与决策 |
| `--only S1,S3` | 只处理指定镜 |
| `--force` | done 也重跑 |
| `--keep-stale` | stale 保留旧产物不重跑（默认 stale 即重跑） |
| `--auto-pick` | assets 三视图自动取第一候选（无人值守） |
| `--abandon-running` | 把遗留 running unit 置回 pending 重提（**须确认服务器侧已无该 job**，防双大模型 OOM） |
| `--trust-llm` | camera 模式A：LLM 生成的 bpy 代码免人工审阅直接执行 |
| `--wait-weights` | video 权重未就绪时轮询（默认立即失败） |
| `--set a.b=c` | 配置覆盖，可重复（优先级坑见 §7） |

### 第 4 步 · 断点续跑与恢复

- **续跑=重跑同一命令**：done 跳过、failed 可重入、stale（输入/上游/配置哈希变化）自动重跑；中断（Ctrl-C）后 manifest 已记进行中状态，重跑时孤儿收敛接管。
- 随时看进度：`python -m pipeline status 我的剧`（各段 done/failed/stale 计数、done 但产物缺失的告警）。
- 服务器重启/提交后断线，按 prompt_id 从 ComfyUI history 回捞补下载：

```bash
python -m pipeline recover 我的剧           # 可选 --pid <prompt_id> 只处理一条
```

已知缺陷：recover 对多文件 unit（如 assets 三视图）用单一 prompt_id 会重复下载同一 history 条目；assets manifest 已逐槽记录 c1/c2 各自 pid，正确做法是按槽级映射回捞（待核心侧修正）。

### 第 5 步 · 成片

`python -m pipeline run 我的剧 --stage final` 产出：成片 `final/<项目名>.mp4`、`<项目名>.srt`、`contact_sheet.png`。
直连入口与更多选项（标题卡/片尾卡/跳过烧录/自定义输出目录等）：

```bash
python -m pipeline.stages.final --project projects/我的剧 --dry-run
python -m pipeline.stages.final --project projects/我的剧 --title "标题" --end-card "_fin" --set final.crf=20
```

音频规格为运行时自适应：全片基准=首个有声镜的 ffprobe 实测采样率/声道（全无声回退 44.1k/stereo），无声镜补轨、异规格镜与 ambience 床轨都先规整到基准再 concat；视频流全程直拷不二压。

---

## 3. LLM 接入配置（OpenAI 兼容三字段）

编辑 `pipeline/config.yaml` 顶层 `llm:` 段（段0 编剧用；运镜模式A 之后可复用）。核心三字段：

```yaml
llm:
  enabled: true
  base_url: "http://127.0.0.1:8010/v1"   # OpenAI 兼容服务根地址，填到 /v1 为止
  api_key: "no-key"                       # 本地服务任意非空串；真实密钥建议改走 api_key_env
  model: "Bonsai2-27B"                    # 模型名（llama.cpp 按权重名报告，填权重名最稳）
  # api_key_env: PROXY_API_KEY            # 非空则优先于 api_key 从该环境变量取密钥（密钥不落盘）
```

两个本机已核实示例（即 config.yaml 内注释原文，二选一取消注释改值即可）：

```yaml
# 示例① 本地 llama.cpp：先运行 E:/projects/推理/llama/start-llama-bonsai.bat
#   拉起 llama-server（Bonsai2-27B，OpenAI 兼容，127.0.0.1:8010，无鉴权）
# enabled: true
# base_url: "http://127.0.0.1:8010/v1"
# model: "Bonsai2-27B"
# api_key: "no-key"

# 示例② DeepSeek：E:/projects/推理/deepseek-api 项目（网页版逆向代理，OpenAI 兼容，
#   默认 127.0.0.1:8317，PROXY_API_KEY 鉴权；模型 deepseek-chat / deepseek-reasoner）
# enabled: true
# base_url: "http://127.0.0.1:8317/v1"
# model: "deepseek-chat"
# api_key_env: PROXY_API_KEY
```

行为要点（`pipeline/screenplay/llm_client.py`）：
- 自动补 `/chat/completions`；失败线性退避重试 `retries` 次；自动剥除 `<think>…</think>` 残留。
- **camera 模式A 的 LLM 是另一套独立配置**（`pipeline/camera/llm_bpy.py`：`PIPELINE_LLM_CONFIG` 环境变量 JSON / `pipeline/camera/llm_config.json` + 环境变量取密钥），尚未迁移到公共 `llm:` 段，两处需分别配。
- 首次接真实服务（llama.cpp / DeepSeek）建议先拿单项目试跑 `--stage screenplay`。

---

## 4. 双通道（assets 与 keyframes 贯穿）

| | photo（默认） | anime |
| --- | --- | --- |
| 权重 | Sakura-Image-2.1-Turbo（Qwen 系）：unet GGUF + Qwen3VL 文本编码器 + VAE，共 3 件 | waiIllustrious SDXL checkpoint，1 件 |
| 采样 | euler，9 步 turbo sigmas，cfg 1.0 | euler_ancestral，steps 28，cfg 5.0，img2img denoise 0.55 |
| 状态 | 冒烟所用通道 | **C 级待标定**（参数为设计值，工作流未经真机验证） |

- 切换：项目级 `project.yaml` 的 `style:`，单镜可用 `shots.yaml` 的 `style:` 覆盖（冒烟项目 S2 即 anime 覆盖示例）。
- 画风锚点按通道各备一套：`project.yaml` 的 `style_anchors` / `negative_anchors`，拼进所有图像提示词。
- 权重路径与参数在 `pipeline/config.yaml` 的 `channels:` 段，改动后相关 unit 会按配置哈希变化自动 stale。

---

## 5. 运镜两模式（camera 段）

每镜可单独指定，shots.yaml 写法：

```yaml
camera: {mode: B, move: push_in, intensity: slow, extra: ""}   # 词库直通
camera: {mode: A, move: push_in}                                # Blender 运镜
```

优先级：单镜 `camera.mode` > `defaults.camera.mode` > `camera.mode_default`（config 默认 **B**；全局切 A：`--set camera.mode_default=A`）。

**模式B · 词库直通 H3（默认，零 Blender 依赖）**：产物 `camera/S#_camdesc.txt` 运镜提示词，video 段与首末帧一起喂给 H3。

**模式A · Blender 运镜**：默认产物运镜起/止两帧 `S#_camfirst/camlast.png`（替代 keyframes 帧作 H3 首末帧）；`--output animation` 则渲 PNG 帧 + ffmpeg 合无音轨 `S#.mp4`（该镜 video 段跳过 H3，等同 `camera.output=full_video`）。可选 `--llm` 让 LLM 生成 bpy 脚本（未配置/失败自动回退预设模板；**默认只落盘待人工审阅**，加 `--trust-llm` 才自动执行）。

直连调试入口（含 `vocab` 打印运镜词库、`install` 手动装 Blender）：

```bash
python -m pipeline.camera.run_camera modeA --shot S1 --image-first S1_first.png \
    --image-last S1_last.png --move push_in --outdir projects/我的剧/camera --verify-only
python -m pipeline.camera.run_camera modeB --shot S1 --move push_in --intensity slow \
    --outdir projects/我的剧/camera
python -m pipeline.camera.run_camera vocab
python -m pipeline.camera.run_camera install -h
```

Blender 便携包缺失时首次使用自动下载解压（约 383MB，落 `camera.blender.install_dir`，默认 `E:/tools/blender`）；已装即复用不校验版本线。模式A 真渲与 LLM 真调用均未在本机跑过（§0）。

---

## 6. 环境要求

| 依赖 | 说明 | 核查命令 |
| --- | --- | --- |
| Python 3 | 仅标准库，无第三方包。本机实测 3.14.3 | `python --version` |
| ComfyUI | assets/keyframes/video 段需要。地址与权重根目录在 `config.yaml` `comfy:` 段（默认 `http://127.0.0.1:8188`，portable 根 `E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable`）。拉起：`run_nvidia_gpu.bat` | `python -m pipeline doctor` |
| H3 节点 | custom_nodes 需有 `MiniMaxH3ImageToVideo`、`TextEncodeQwenImage21`、`UnetLoaderGGUF` 三节点（doctor 逐一列出；冒烟时点已核实装齐） | 同上 |
| 三族权重共 10 件 | photo 3 件 + anime 1 件 + H3 6 件（unet/text_encoder 各 >5GB、两支 LoRA 各 >1.5GB 大小门防残件），路径见 `config.yaml`，doctor 逐件查大小 | 同上 |
| ffmpeg / ffprobe | ≥4 即可，PATH 优先（本机 8.1.1）；final/camera 段用。路径可配 `final.ffmpeg` | 同上 |
| 便携 Blender | 仅 camera 模式A 需要；缺失自动下载（约 383MB） | 同上 |
| LLM 服务（可选） | 段0 编剧需要；本地 llama.cpp 或任意 OpenAI 兼容端点（§3） | 跑一次 `--stage screenplay` |

`python -m pipeline doctor` 为信息性输出（环境缺项不改退出码）；`python selftest.py` 全量干跑回归（`--with-server` 加测服务器与节点清单，`--strict` 把 WARN 升级为 FAIL）。

---

## 7. 故障排查

| 症状 / 退出码 | 原因与处置 |
| --- | --- |
| `No module named pipeline` | cwd 不在仓库根——`cd /e/projects/推理/R18_video` 后再跑，或设 PYTHONPATH 含仓库根 |
| **exit 2**（assets/keyframes/video 开跑即退） | ComfyUI 不可达（8188 停机）：先运行 `run_nvidia_gpu.bat` 再重跑。camera/final 段不受影响 |
| **exit 1** 且提示"依赖未就绪，不自动越级" | 段依赖链有缺口：先补齐上游段（`status` 看哪段未完成） |
| **exit 3** | 所选段执行器未落地（并行构建期），换直连入口（§8）或等该段收口 |
| unit 长期 `running` 卡住 | 中断遗留的孤儿：确认服务器侧已无该 job 后加 `--abandon-running` 重跑（防双大模型 OOM，有意不自动重提） |
| 提交后断线/服务器重启 | `python -m pipeline recover <项目>` 按 prompt_id 回捞 history；history 查无会自动置回 pending 重提。多文件 unit 回捞有已知缺陷（§2 第 4 步） |
| LoRA/大权重报"大小低于下限（残件？）" | 下载被截断（先例：4-step LoRA `.truncated.bak` 残件）：删残件重下，doctor 复查 |
| video 段报权重未就绪 | 默认立即失败；确认在下载中可加 `--wait-weights` 轮询 |
| `--set` 传参好像没生效 | 已知优先级坑：`project.yaml` 的 `overrides:` 与 CLI `--set` 同键时**项目覆盖 CLI**（与设计 §2.2 字面不一致）。规避：别在 overrides 里写与 `--set` 同键的配置 |
| screenplay 报服务不可达直接失败 | 设计如此（不静默降级）：起 LLM 服务（§3）或改走手填三份 yaml 的校验器路线 |
| status 显示某段 `manifest 损坏` | 按提示人工检查或删除该段 manifest 重建（该段会整体重跑） |
| 中文项目名在 ComfyUI 输出路径出问题 | 属 H3 首跑标定确认项：video 段产物经 /view 下载落项目目录，服务器端子目录组织用项目名。稳妥起见首跑用 ASCII 项目名验证后再试中文 |
| 单镜时长被拒（"超容差，最近网格点…"） | H3 帧数网格 124..362 帧（≈5.167–15.083s，容差 ±0.354s）：把 `duration_s` 调到报错里列出的最近网格点 |
| 段内异常只显示一行、想要完整栈 | CLI 段级兜底吞掉 traceback（有意为之）：直调 `python -m pipeline.stages.<段> …` 复现拿完整栈 |

---

## 8. 模块命令速查与已知限制

### 命令速查（统一入口之外的直连入口）

```bash
# 核心与脚手架
python selftest.py [--with-server] [--strict]          # 全量干跑回归
python -m pipeline run <项目> --stage camera --dry-run # 复审原始场景

# assets（三视图）
python -m pipeline.stages.assets <项目> --dry-run       # 渲染去掉 --dry-run
python -m pipeline.stages.assets <项目> --pick 主角:c1  # 人工挑候选（:c1/:c2）
python -m pipeline.stages.assets --selftest             # 模块自检（干跑）

# keyframes（首末帧）
python -m pipeline.selftest_keyframes                   # 模块自检
python -m pipeline.stages.keyframes <项目> [--only S1,S3] [--force] [--dry-run]

# camera（运镜）
python -m pipeline.camera.run_camera modeA|modeB|vocab|install -h

# video（本地 H3）
python -m pipeline.stages.video <项目> --dry-run
python -m pipeline.stages.video --selftest

# final（合成）
python -m pipeline.stages.final --project projects/我的剧 --dry-run
#   可选：--only S1,S3 --force --no-burn --no-contact-sheet --no-ambience
#         --title 文本 --end-card 文本 --out-dir output/final --set final.crf=20
```

### 已知限制（如实清单，勿默认"都能跑"）

- **H3（video 段）B 级**：本机零成功渲染记录；节点结构与权重在盘已核实，124 帧网格、双 VAE 音频、时长口径全部待首跑标定。首跑 `--only S1`。
- **camera 模式A**：真渲（EEVEE + ffmpeg 合成）与 LLM 真调用未跑过，验证止于场景构建 dry-run 层；Blender 首次使用触发约 383MB 下载。
- **anime 通道 C 级**：steps/cfg/sampler/denoise 均为设计值，待标定；photo 通道 turbo 9 步 sigmas 同属首跑标定项。
- **assets/keyframes 渲染路径**：未经真实 ComfyUI 验证（FakeComfy 端到端 + 干跑）；assets 首跑标定点=1024×1024 方图。keyframes 的 `use_char_ref`（角色参考图注入）为 B 级特性，**默认关**。
- **网格参数变更不反向失效历史模式A产物**：改网格只对新渲染生效，历史 done 的模式A unit 不会自动 stale（后续增强项）。
- **screenplay 段**：不进 `--stage all`、不写段 manifest（剧本与三份 yaml 即状态），`status` 不显示它；breakdown 产出的 `seed: 0` 是"由 seed_base 派生"的占位语义（派生公式在 keyframes/video 段统一执行）。
- **recover 多文件 unit** 单 pid 重复下载缺陷（§2 第 4 步）。
- **`--set` 与项目 overrides 同键优先级**（§7）。
- **selftest.py 为多模块共写文件**，各段自检都有独立直连入口（见速查），互为兜底。

### 内容边界

- 框架提示词模板与 LLM 提示词**默认全成年（18+）且默认中性内容（全身着装）**；screenplay 段无论 `project.yaml` 的 `content_rating` 声明为何，成年底线不放宽。
- 该约束是**提示词层约定**（模板常量 + 不可关闭的负面锚点 + 模块文档），无法阻止使用者手工改锚点文本绕过——生成内容的合法性与合规责任由使用者承担，请遵守当地法律与分发平台政策。
- 遗留问题：脚手架 `characters.yaml` 示例锚点沿袭旧内容（含未成年角色描述的示例），尚未清理——新建项目第一步就应整体替换（见 §2 第 1 步）。
