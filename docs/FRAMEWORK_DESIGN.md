# AI 漫剧生产框架 · 通用化设计（FRAMEWORK_DESIGN v2）

> 目标环境（2026-10-26 已逐项核实）：Windows 10/11 + Git Bash + 单卡 RTX 3090(24G)；
> ComfyUI 便携包 `E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable`（127.0.0.1:8188，内嵌 Python 3.13.14；**当前 8188 未监听**，`netstat` 核实）；
> ffmpeg 8.1.1 已在 PATH（`which ffmpeg` 核实）；系统 Python 3.14.3 可用；Blender 未安装（由运镜模块负责便携安装）。
> 设计原则：**保守可运行优先**。本文所有技术参数按三级证据标注，防止"代码存在"被误当"已跑通"：
>
> - **A 级（本机已跑通）**：photo 通道关键帧全批次（keyframes.log 尾部 `S9/S10 … OK`、`KEYFRAME BATCH FINISHED`，`output/keyframes/` 20 张 PNG 在盘）、ComfyUI HTTP 提交/轮询/下载回路（comfy_client.py，经 test_s1.py 与关键帧批次实际使用）、PIL contact sheet（`output/keyframes_contact_sheet.png` 在盘）。
> - **B 级（仅代码/文案存在，零运行证据）**：H3 视频工作流（render_videos.py 的 video_wf 从未成功执行一次——`output/shots/` 为空，videos.log 止于 `20:35:09 S1 render attempt 1 ...` 无任何结果行，该次尝试无声死亡且当时 8188 已不可达）、final 合成（`output/` 下无 `_merged.mp4/subtitles.srt/ep01*.mp4`，build_final.py 从未跑完）、img_wf 的 ref_images 参考图分支（test_s1.py:21、render_keyframes.py:121,133 三处调用均未传参）。**B 级参数一律视为"设计值，待首跑标定"，落地时先试产 1 镜再批量。**
> - **C 级（外部依赖未装/未实测）**：MiniMax H3 节点（custom_nodes 仅 ComfyUI-GGUF 与 SakuraQwenImage21Tools，`ls` 核实）、Blender 及其 headless 渲染、LLM 生成 bpy。

---

## 0. 与既有代码的关系（只读映射表 + 证据分级）

`_tools/` 与 `output/`、`剧本_雨落无声.md`、`shots_video.json` 一律**只读、不修改、不删除**。框架吸收其结构做法，按证据级别区分对待：

| 一次性脚本（证据） | 框架去向 | 吸收内容 | 证据级 |
|---|---|---|---|
| `_tools/comfy_client.py:17-52` | `pipeline/comfy.py` | queue → 轮询 history → /view 下载；SERVER=8188（:5）；Sakura t2i API 图（:60-81，ManualSigmas 8 步 turbo、euler、cfg=1.0） | **A**（回路跑通）；其中 ref_images 分支（:65-67）为 **B** |
| `_tools/render_keyframes.py` | `pipeline/stages/keyframes.py` + `pipeline/workflows/photo_sakura.py` | 1344×768（:121）；末帧保身份 edit 图（:64-80）；`last_fresh` 分支（:42-43,:131-133）；存在即跳过（:119-125）；重试 2 次（:88-102）；双 input 目录（:10-11）；seed 派生 +5/+7（:133,:140） | **A**（批次全 OK） |
| `_tools/render_videos.py` | `pipeline/stages/video.py` + `pipeline/workflows/h3_fl2v.py` | H3 fl2v API 图结构（:37-58，含双 VAE+音频）；length=124@24fps（:48）；`res_multistep`/`simple`（:50-51）；4/8-step LoRA 按大小择优且**步数随档位**（:32-35）；权重 >5GB 门（:29,:85）；>200KB 跳过门（:109） | **B**（零运行证据；结构照抄、参数全部待首跑标定，含 124 帧本身） |
| `_tools/build_final.py` | `pipeline/stages/final.py` | concat 转码 x264 crf18（:42-43）；SRT 全局时轴偏移算法（:45-48）；drawtext 标题 + subtitles force_style 与 Windows 盘符转义 `C\:/`（:10,:67-72）；msyh.ttc 字体（:10） | **B**（零运行证据） |
| `_tools/contact_sheet.py` | `final` 段改用 **ffmpeg tile** 实现 | 原脚本 PIL 版已跑通但依赖非标准库；框架全程仅标准库故改道 ffmpeg（**改道后为新实现，待验证**） | 原 **A** / 框架版 **B** |
| `_tools/resume.py` | manifest 记录 prompt_id + `pipeline recover` | 按 pid 回捞 history 补下载 | 思路产品化 |
| `_tools/dl_h3.ps1`、`dl_h3.log` | 保留为一次性运维工具 | hf-mirror 断点续传模式；框架只在启动时做大小校验 | — |
| `_tools/sakura_t2i_ref.json` | **仅作 UI 参考** | 已核实为 UI 格式（顶层 `nodes`/`links` 数组），**不可**直接 POST /prompt；框架模板一律用 API 格式 dict 构造器 | — |

择档事实：videos.log 唯一一次实际运行记录为 `lora=minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors steps=4`（20:35:09）——**4-step/4 步是曾被选中的档位（也未渲染成功）**；剧本:102"turbo-4step-768p LoRA…8 steps"自相矛盾，不作为依据；8-step LoRA 仅在盘未用过。

历史产物 `output/` 原地保留；《雨落无声》如需纳入框架，后续用 `pipeline import` 手工登记 manifest（P2，不在首版范围）。

---

## 1. 总体目录结构

```
E:/projects/推理/R18_video/
├── pipeline/                      # 框架包：通用、跨项目、仅标准库（系统 Python ≥3.10）
│   ├── __init__.py
│   ├── __main__.py                # 必需：python -m pipeline 的入口（转发 cli.main）
│   │                              #   已实测：无此文件时报
│   │                              #   "No module named pipeline.__main__; pipeline is a package
│   │                              #    and cannot be directly executed"（exit=1）
│   ├── config.py                  # 全局+项目配置加载/深合并/校验/哈希；定位仓库根（见 §2.2）
│   ├── miniyaml.py                # 受限子集 YAML 解析器（见 §2.3）
│   ├── comfy.py                   # ComfyUI HTTP 客户端（升级自 comfy_client.py）
│   ├── manifest.py                # 各段 manifest 读写（原子写/完成判定/上游指纹）
│   ├── ffmpeg_tool.py             # ffmpeg/ffprobe 发现与封装（PATH 优先，可绝对路径覆盖）
│   ├── llm.py                     # OpenAI 兼容 LLM 客户端 + 预设模板回退
│   ├── blender.py                 # Blender 便携版 安装（zipfile 解压）/发现/headless 执行
│   ├── scaffold.py                # new-project 脚手架
│   ├── screenplay/                # 段0 编剧（LLM 或模板/校验器降级，不进 --stage all）
│   │   ├── __init__.py            #   段分发（screenplay-write|breakdown；run --stage screenplay 串联）
│   │   ├── llm_client.py          #   公共 OpenAI 兼容客户端（config llm: 段；模式A 之后可复用）
│   │   ├── common.py              #   时长网格边界/预算可行性/剧本预算行解析/内容边界默认
│   │   ├── prompts.py             #   提示词构造（write/breakdown；内容边界默认中性·全成年）
│   │   ├── write.py               #   创意/大纲 → 剧本.md（预算可行性 + 结构 lint + 有界退回）
│   │   └── breakdown.py           #   剧本.md → shots/characters/subtitles.yaml（schema 强校验+有界退回）
│   ├── cli.py                     # 命令实现：new / run / status / doctor / recover
│   ├── config.yaml                # ① 全局配置（唯一事实源，见 §2）
│   ├── workflows/                 # API 格式工作流构造器（dict，参数注入）
│   │   ├── __init__.py            #   build_t2i/build_edit/build_h3 统一入口（双通道分派）
│   │   ├── photo_sakura.py        #   Sakura t2i（=img_wf）+ 保身份 edit（=edit_wf）
│   │   ├── anime_sdxl.py          #   waiIllustrious SDXL t2i + img2img 【待标定，§4】
│   │   └── h3_fl2v.py             #   MiniMax H3 首尾帧图生视频（结构照抄 video_wf，参数待标定）
│   ├── camera/
│   │   ├── vocab.yaml             # 模式B 运镜提示词词库（move × intensity 短语库）
│   │   └── presets/               # 模式A 预设 bpy 模板（参数化）
│   │       ├── dolly_in.py        #   推近
│   │       ├── pan_lr.py          #   横移/摇
│   │       └── parallax_2p.py     #   双平面 2.5D 视差
│   └── stages/                    # 五段执行器（只做编排，不直接拼工作流）
│       ├── assets.py              # 段1 资产
│       ├── keyframes.py           # 段2 关键帧
│       ├── camera.py              # 段3 运镜（A/B 双模式）
│       ├── video.py               # 段4 视频
│       └── final.py               # 段5 成片
├── projects/                      # 项目工作区：一剧一目录
│   └── <名>/
│       ├── project.yaml           # ② 项目级配置覆盖（含 style 通道选择）
│       ├── 剧本.md                # 人写/LLM 辅助台本（脚手架给模板）
│       ├── characters.yaml        # 角色一致性锚点 + 资产需求清单
│       ├── shots.yaml             # 分镜表（驱动 keyframes/camera/video/final）
│       ├── subtitles.yaml         # 字幕/标题卡 cues（final 段消费）
│       ├── assets/                # 段1 产物：<char>_threeview.png / <char>_expr_<名>.png / <char>_prop_<名>.png
│       │   └── assets_manifest.json
│       ├── keyframes/             # 段2 产物：S#_first.png / S#_last.png
│       │   └── keyframes_manifest.json
│       ├── camera/                # 段3 产物：S#.bpy / S#_camfirst.png / S#_camlast.png [/ S#.mp4] / S#_camdesc.txt
│       │   └── camera_manifest.json
│       ├── video/                 # 段4 产物：S#.mp4（H3 含音轨）
│       │   └── video_manifest.json
│       └── final/                 # 段5 产物：<项目名>.mp4 / <项目名>.srt / contact_sheet.png
│           └── final_manifest.json
├── selftest.py                    # ⑦ 根目录自检（仅标准库干跑，见 §7）
├── docs/FRAMEWORK_DESIGN.md       # 本文档
├── _tools/                        # 既有一次性脚本（只读参考）
├── output/                        # 《雨落无声》历史产物（原地不动）
└── logs/                          # pipeline 运行日志（每段一份 append）
```

要点：
- **框架与项目分离**：`pipeline/` 不含任何具体剧目内容；`projects/<名>/` 含全部项目数据与产物，删目录即删项目。
- 所有文本文件 UTF-8；配置/代码内路径一律写正斜杠 `E:/...`（Git Bash 与 Python 均原生接受，规避反斜杠转义）。
- 产物命名全局约定 `S#_first/S#_last/S#.mp4`，沿用 `_tools` 既有约定（`render_keyframes.py:112-113`、`render_videos.py:108`），便于历史物料对照。

---

## 2. 配置：全局 `pipeline/config.yaml` + 项目覆盖

### 2.1 全局配置

```yaml
comfy:
  server: http://127.0.0.1:8188          # comfy_client.py:5；当前停机，run 各段前先做可达性预检（§4.0）
  client_id: pipeline
  portable_root: E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable
  models_dir: E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/models
  input_dirs:                             # LoadImage 需要两处都放（render_keyframes.py:10-11 实测）
    - E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/user/default/input
    - E:/SD-WEBUI-AKI-V5.0/ComfyUI_windows_portable/ComfyUI/input
  poll_interval_s: 3
  timeout_img_s: 900                      # render_keyframes.py:93（A 级）
  timeout_video_s: 3600                   # render_videos.py:123（B 级设计值，首跑后再定）

channels:                                 # ③ 双通道，贯穿 assets 与 keyframes
  photo:                                  # = Sakura-Image-2.1-Turbo（Qwen 系）
    unet_gguf: diffusion_models/Sakura-Image-2.1-Turbo-Q5_K_M-4.60GiB.gguf
    clip: text_encoders/Sakura-TextEncoder-Qwen3VL-8B-Uncensored-bf16.safetensors
    vae: vae/qwen_image_2.1_vae_bf16.safetensors
    clip_type: qwen_image
    resolution: 1024
    width: 1344
    height: 768
    sampler: euler
    sigmas: "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"
    cfg: 1.0
  anime:                                  # = waiIllustrious SDXL 【工作流待标定，§4.3】
    checkpoint: checkpoints/waiIllustriousSDXL_v170.safetensors
    width: 1344
    height: 768
    steps: 28
    cfg: 5.0
    sampler: euler_ancestral              # 服务器实测名（新版 ComfyUI 无 euler_a，冒烟 2026-10-27 修正）
    img2img_denoise: 0.55                 # 末帧保身份 img2img 初始值，需 3-5 张标定
    negative: "photo, 3d, realistic, text, watermark, bad anatomy, extra fingers, lowres"

assets:                                   # §4.1 资产段专用（参考图，非成片帧）
  width: 1024
  height: 1024
  candidates: 2                           # 三视图每候选数（seed、seed+1）

video:                                    # 本地 MiniMax H3 fl2v —— 全节 B 级设计值
  # 模型值一律存 models_dir 下的相对路径（含子目录：实测六件套分属 diffusion_models/
  # text_encoders/loras/vae 四个子目录，photo unet 与 H3 unet 同在 diffusion_models）。
  # 两种用途由 comfy.py 的 helper 统一拆分：workflow 节点入参取 basename（ComfyUI 按
  # 自身目录解析，先例 render_videos.py:39 裸名），在盘/就绪检查取 models_dir+相对路径
  # 拼全路径（先例 render_videos.py:18-21）——避免"拿裸文件名拼 models_dir 必 miss"。
  unet: diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors
  text_encoder: text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
  clip_type: minimax
  lora_4step: loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
  lora_8step: loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
  lora_min_bytes: 1500000000              # 实测 4-step LoRA 有 .truncated.bak 残件，必须防（render_videos.py:32-35）
  vae_video: vae/minimax_h3_video_vae_fp16.safetensors
  vae_audio: vae/minimax_h3_audio_vae_fp32.safetensors
  weight_min_bytes: 5000000000            # unet/te 各 >5GB 才算就绪（render_videos.py:85 的门，沿用）
  sampler: res_multistep
  scheduler: simple
  # steps 不写死：随所选 LoRA 档位（4-step→4 / 8-step→8，render_videos.py:32-35 择优逻辑；
  # 4-step/4 步是 videos.log 里唯一被实际选中过的档位，但未渲染成功）
  steps_follow_lora: true
  fps: 24
  width: 1344
  height: 768
  length_grid: {a: 17, b: 5}              # 设计值：出自剧本:102 文案与 render_videos.py:48 代码字面量，
                                          #   本机从未成功执行过任何 H3 渲染；首跑标定含 124 帧本身（§4.4）
  k_min: 7                                # 时长域下端：124 帧 ≈ 5.167s
  k_max: 21                               # 时长域上端：362 帧 ≈ 15.083s
  duration_tolerance: half_step           # 命中容差 = a/fps/2 ≈ 0.354s；夹取后仍超容差 → 显式报错列最近网格点
  default_duration_s: 5.17
  min_request_s: 5.0                      # 请求侧校验域。"5–15s 可配"按 ±半步内命中解释：
  max_request_s: 15.0                     #   实际可达 5.167–15.083s（k 域 [7,21]）；本会话已逐 0.5s
                                          #   全扫 21 点验证全部命中（端点 15.0→k=21→15.083s，误差 0.083s）
  min_bytes: 200000                       # 成片大小下限（render_videos.py:109 门，沿用为设计值）

final:                                    # 本节 B 级设计值（build_final.py 从未跑完）
  ffmpeg: ffmpeg                          # PATH 优先（已核实 8.1.1 在 PATH）；可写绝对路径
  ffprobe: ffprobe
  crf: 18
  preset: medium
  audio_codec: aac
  audio_bitrate: 192k
  font: C:/Windows/Fonts/msyh.ttc
  sub_style: "FontName=Microsoft YaHei&FontSize=16&PrimaryColour=&H00F5F5F5&OutlineColour=&H80000000&BorderStyle=1&Outline=1&Shadow=0&Alignment=2&MarginV=34"

camera:
  mode_default: B                         # B=词库直通 H3（零依赖，默认）；A=Blender 运镜
  blender:                                # 本节 C 级（Blender 未装，全部待首跑验证）
    version: "4.2"                        # LTS；首次使用由 pipeline/blender.py 下载便携 zip、
                                          #   用 Python zipfile 解压到 install_dir（不用 tar：已实测
                                          #   Git Bash tar=GNU tar 1.35 不解 zip，tarfile 抛 ReadError）
    install_dir: E:/tools/blender
    download_base: https://download.blender.org/release/
    # 引擎枚举存 API 名并按版本自适应：4.2–4.5 为 BLENDER_EEVEE_NEXT，≤4.1 与 5.0+ 为
    # BLENDER_EEVEE（依据：Blender 官方 5.0 Python API 发布说明及 sketchfab/blender-plugin
    # issue #153/#176；本机未装 Blender，未实测）。预设模板里用 try/except 双枚举写法。
    render_engine_enum: [BLENDER_EEVEE_NEXT, BLENDER_EEVEE]
    samples: 16
    overscan: 1.08                        # 预留运镜裁切余量防穿帮
    timeout_s: 600
  llm:                                    # 模式A 的 bpy 生成；不配置则一律走预设模板
    enabled: false
    base_url: ""                          # OpenAI 兼容 /v1/chat/completions
    api_key_env: LLM_API_KEY              # 只从环境变量取密钥，配置文件不落密钥
    model: ""
    timeout_s: 90
    retries: 2
    require_review: true                  # LLM 代码默认只落盘不执行，--trust-llm 才自动跑

llm:                                      # 公共 OpenAI 兼容客户端（pipeline/screenplay/llm_client.py；
  enabled: false                          #   段0 编剧用；运镜模式A 之后可复用）
  base_url: ""                            # 填到 /v1 为止，客户端自动补 /chat/completions
  api_key: ""                             # 本地服务可任意非空串；真实密钥走 api_key_env
  api_key_env: ""                         # 非空则优先于 api_key 从环境变量取（密钥不落文件）
  model: ""
  timeout_s: 120
  retries: 2
  temperature: 0.7
  # 两个已核实示例（注释形式，启用时解开并改 enabled: true）：
  # ① 本地 llama.cpp：运行 E:/projects/推理/llama/start-llama-bonsai.bat 拉起 llama-server
  #   （Bonsai2-27B，OpenAI 兼容，127.0.0.1:8010，无鉴权）
  #   base_url: "http://127.0.0.1:8010/v1"  model: "Bonsai2-27B"  api_key: "no-key"
  # ② DeepSeek：E:/projects/推理/deepseek-api 项目（网页版逆向代理，OpenAI 兼容，
  #   127.0.0.1:8317，PROXY_API_KEY 鉴权，deepseek-chat / deepseek-reasoner）
  #   base_url: "http://127.0.0.1:8317/v1"  model: "deepseek-chat"  api_key_env: PROXY_API_KEY
  # 与既有实现的关系：camera 模块继续用 camera/llm_bpy.py（自带安全扫描），不强行重构；
  # 统一迁移路径 = llm_bpy.load_config 增配本节候选 + chat 调用换 llm_client（见其头注）。

run:
  attempts: 2                             # 总尝试次数（含首次）= render_keyframes.py:90 /
                                          #   render_videos.py:118 的 tries=2，即"1 次 + 1 次重试"
                                          #   （不是"重试 2 次"——那会是 3 次尝试）
  retry_wait_s: 20
  quiet_first_s: 5
  # 服务器可达预检仅对需要 ComfyUI 的段执行（needs_comfy：assets/keyframes/video = true；
  # camera = false——模式A 用 Blender、模式B 产物是纯文本 camdesc；final = false——只用 ffmpeg）。
  # 不可达即一句提示退出（exit 2），不让 unit 空转尝试后被误标 failed（当前 8188 停机即此场景）
  server_precheck: true

paths:
  # 相对路径基准 = 仓库根（= pipeline 包的父目录，由 config.py 用 __file__ 定位后锚定），
  # 与命令行 cwd 无关。边界说明：本保证只覆盖"路径解析"——`python -m pipeline` 的包发现
  # 仍需 cwd=仓库根或 PYTHONPATH 含仓库根（标准 Python 行为；仓库外实测报
  # No module named pipeline）。设 PYTHONPATH 后从任意 cwd 调用，配置解析到同一批目录
  projects_root: projects
  logs_dir: logs
```

### 2.2 合并与覆盖规则

优先级：**全局 config.yaml < 项目 project.yaml 的 `overrides:` 节 < CLI `--set a.b.c=v`**。

- `overrides` 按路径深合并：映射递归、**标量覆盖、列表整体替换**（不做列表合并，避免歧义）；
- 项目 `project.yaml` 除 `overrides` 外的字段（name/style/seed_base/style_anchors…）是项目元数据，不进全局合并；
- `config.py` 启动时做**全量校验**：必填缺失、路径不存在、数值越界一次性全部列出后报错（不是遇到第一个才炸）；
- 合并结果计算 `sha256(config_hash)` 写进各段 manifest，用于 §5 的失效判定。

项目 `project.yaml` 示例：

```yaml
name: 雨落无声
style: photo                    # 项目默认通道（anime|photo），单镜可在 shots.yaml 覆盖
seed_base: 20261000
style_anchors:                  # 画风锚点按通道各备一套，拼进所有图像提示词
  photo: "cinematic anime film still, Makoto Shinkai style rain, cel-shaded with soft moody lighting, wet asphalt reflections, muted dusk palette with warm neon accents, film grain, 16:9 widescreen composition"
  anime: "masterpiece anime illustration, cinematic rain atmosphere, cel shading, film grain"
negative_anchors:
  photo: "photorealistic, 3d render, western cartoon, text, watermark, logo, signature, extra fingers, deformed hands, extra limbs, bad anatomy, blurry, lowres, cropped"
  anime: "photo, 3d render, text, watermark, bad anatomy, extra fingers, blurry, lowres"
overrides:
  video: {default_duration_s: 5.17}
```

### 2.3 仅标准库 ⇒ `pipeline/miniyaml.py`

框架全程不用第三方包（系统 Python 3.14.3 裸机可跑，与 `_tools` 同策略）。stdlib 无 YAML，故自带**受限子集解析器**，支持集**与本文档模板严格自洽**：

- 嵌套映射（块风格）、块序列（`- `）、**flow 序列 `[a, b, c]`**、**flow 映射 `{k: v}` 及其嵌套（≥2 层，如 `{camera: {mode: B}}`）**、`|` 与 `>` 多行块、引号/裸标量、`#` 注释、UTF-8；
- **不支持**：锚点与别名（`&`/`*`）、多文档（`---`）、flow 与块混排的边角组合（如 flow 序列里嵌块映射）。越界语法直接报错并给行号，提示改写。

§6 脚手架模板里出现的 `characters: [主角id]`、`defaults: {duration_s: 5.17, camera: {mode: B, …}}` 等均落在支持集内；**selftest 第 10 项会把脚手架模板文件本身过一遍 miniyaml 并断言解析成功**，规范与实现不允许漂移。**为何不用 JSON**：约定 yaml 文件名（shots.yaml/characters.yaml/config.yaml），且 yaml 支持中文注释，对人工维护分镜表更友好。

---

## 3. 双通道抽象（贯穿 assets 与 keyframes）

通道 = 图像生成引擎的静态选择，**一处声明、全链一致**：

| 通道 | 模型（已核实在盘） | t2i | 保身份改画 | 证据级 |
|---|---|---|---|---|
| `photo` | Sakura-Image-2.1-Turbo GGUF + qwen_image_2.1_vae + 专用 TE | `img_wf` 图（comfy_client.py:60-81） | `edit_wf` 图：首帧为 image_1 条件 + edit 提示词 + `different person/face/…` 负面（render_keyframes.py:22,64-80） | **A**（关键帧批次全 OK） |
| `anime` | waiIllustriousSDXL_v170（checkpoints/ 已核实） | 标准 SDXL 图（CheckpointLoaderSimple+CLIPTextEncode+KSampler）**待标定** | img2img：首帧 VAEEncode 为初始 latent，denoise≈0.55 + 一致性负面词 **待标定** | **C**（权重在盘，从未使用） |

规则：
1. `style` 在 project.yaml 声明，shots.yaml 单镜可用 `style:` 覆盖（混合风格允许，但框架会告警：该镜出场角色的同通道资产必须存在，否则该镜降级回项目通道）。
2. `pipeline/workflows/__init__.py` 提供 `build_t2i(channel, …)` / `build_edit(channel, …)` 统一入口；stages 层**不感知**具体节点图——加第三通道只扩 workflows 目录 + config channels 节。
3. 通道只决定"图像怎么生成"；**关键帧与视频帧统一 1344×768**（photo 与 H3 同尺寸沿自 render_keyframes.py:121、render_videos.py:47-48；SDXL 在 1344×768 属常规可用桶，具体画质在标定期确认）。**唯一例外：assets 段资产参考图 1024×1024**（config `assets:` 节；1024 是 Qwen 系官方原生分辨率，但本机已跑通批次全部为 1344×768，方图输出列为 assets 段首跑标定点，见 §4.1）。
4. **seed 派生规则（全段统一，杜绝实现者各自发明）**——一律从 project.yaml `seed_base` 出发派生，显式指定优先（yaml 里写了 `seed:` 的 unit 用显式值），派生值与实际值全部写入 manifest：
   | 段 | 派生公式 | 备注 |
   |---|---|---|
   | assets | `seed_base + 1_000_000 + 1000×角色序 + 10×资产序`；三视图第 i 候选再 `+ (i-1)` | 候选即 seed、seed+1（config assets.candidates=2） |
   | keyframes 首帧 | `seed_base + 10×镜序` | 镜序从 1 起 |
   | keyframes 末帧 | edit：首帧 seed `+5`；fresh：`+7` | 沿用 render_keyframes.py:133,140 的 +5/+7（A 级） |
   | video | `seed_base + 2_000_000 + 镜序` | shots_video.json 历史上用独立 71001+ 系（S1=71001…S10=71010，:4,:40），框架改为统一派生；迁移老项目时在 yaml 写显式 seed |
   | camera 模式A | 无 seed（Blender 渲染确定性）；LLM 调用 temperature=0 并在 manifest 记响应哈希 | — |
5. 提示词拼装次序固定，保证可复现：
   `镜头描述 + 角色锚点(characters.yaml anchor_en) + 出场道具锚点 + 画风锚点(project.yaml style_anchors[style])`，负面同理。

---

## 4. 五段契约

### 4.0 段公共前置（每段开跑前）

1. **服务器可达预检**（`run.server_precheck`，默认开，**仅 needs_comfy 段执行**：assets/keyframes/video；camera 段不做——模式A 用 Blender、模式B 产物是纯文本 camdesc，均不依赖 ComfyUI；final 段不做——只依赖 ffmpeg）：GET `/system_stats`，不可达 → 打印一句"ComfyUI 未启动（当前 8188 停机即此情形）：请先运行 run_nvidia_gpu.bat"并以 **exit 2** 退出——区别于渲染失败（exit 1），且不让 unit 空转尝试后被误标 failed。
2. **孤儿 job 收敛**：段启动先扫 manifest 中 `status==running` 的 unit（上次进程死于 ComfyUI 仍在执行时的遗留，videos.log 20:35:09 的 S1 attempt 即实例）：
   - GET `/history/<prompt_id>` 已有完成记录 → 直接收割下载，置 done（不重提）；
   - GET `/queue` 显示该 prompt_id 仍在队列/执行中 → **等待并收割，绝不重提新 job**（H3 int8 unet 20.8G 几乎吃满 24G，双 H3 job 同跑必然 OOM 双杀）；**等待有上限**：复用该段超时（图 `timeout_img_s`=900s / 视频 `timeout_video_s`=3600s，按 `poll_interval_s` 轮询），超时 → unit 置 failed 并提示人工处置（ComfyUI 端取消该 job 或重启服务）；确认服务器侧已无此 job 后可用 `--abandon-running` 把 running 强制置 pending 重提；
   - history 与 queue 都查无（如服务器重启过）→ 置回 pending 重跑。
3. 依赖段完成度检查：缺上游产物 → 列出缺口、非零退出，不自动越级。

通用约定：每段输入 = 项目 yaml + 上一段产物；每段输出 = 固定命名文件 + 本段 manifest；**单位（unit）**是最小续跑单元（资产=角色×资产类型；其余=镜）。所有段支持 `--only` 过滤与 `--force` 强制重跑。**shots.yaml 取值优先级：单镜字段 > `shots.defaults:` 节 > config 段默认值**（`defaults:` 是纯缺省值表，任何单镜字段的出现都整体覆盖对应键，不合并）。

### 4.1 assets — 角色设定资产

- **输入**：`characters.yaml`（角色锚点 + 资产需求）、project.yaml 通道、config `assets:` 节（分辨率 1024×1024、候选数 2）。
- **行为**：对每个角色 × {三视图, 每个表情差分, 每个道具} 生成 t2i：
  - 三视图：`character reference sheet, front view / side view / back view, standing full body, standard outfit, neutral flat background` + 角色/服装锚点；**标准全身着装**是硬约定（一致性锚点以着装态为准，避免资产与剧情衣着漂移）。每候选一个 seed（§3 规则 4）。
  - 表情差分：`expression sheet, single portrait bust, <expr> expression, same face same hair`。
  - 道具：`prop sheet, single object on plain background` + 道具锚点。
  - **1024×1024 方图为 assets 段首跑标定点**（§3 规则 3 的唯一例外；Qwen 系官方原生分辨率为 1024，但本机已跑通批次全部 1344×768，方图零运行证据）。
- **输出**：`assets/<char_id>_threeview.png`（候选 `<char_id>_threeview_c1/c2.png`，pick 后定名）、`<char_id>_expr_<名>.png`、`<char_id>_prop_<名>.png` + `assets_manifest.json`（unit 键 = `<char_id>:threeview|expr:<名>|prop:<名>`）。
- **人工关口**：三视图是全剧身份锚，渲染 `assets.candidates` 个候选供人挑，选中后 manifest 标 `picked: true`；未 pick 时 keyframes 段对该角色**拒绝开跑**（宁可停，不糊）。`--auto-pick` 可跳过（取第一张，供无人值守批跑）。

### 4.2 keyframes — 每镜首/末帧

- **输入**：`shots.yaml`（每镜 first/last 提示词、last_mode、style）、`characters.yaml` 锚点、assets（可选参考图）。
- **行为**（每镜两 unit：`:first`、`:last`）：
  - 首帧：t2i（§3 规则 5 拼装次序），1344×768。默认纯锚点提示词（A 级验证路径）。
  - 末帧：`last_mode: edit`（默认）= 首帧作 image_1 的保身份改画。**前置步骤（不可省）**：先把 `S#_first.png` 复制为 `S#_ref.png` 放进两个 input_dirs，且**先 makedirs 再复制**——keyframes.log:4-12 的翻车先例：目录不存在时 shutil.copyfile 直接 FileNotFoundError，render_keyframes.py:136-139 因此补了 makedirs；漏此步则 edit_wf 的 LoadImage 找不到文件。edit 提示词模板 = "Edit \<image1\>: keep exactly the same person/face/hairstyle/outfit/camera angle/framing/lighting and background; change only — \<last 描述\>" + 画风锚点，负面追加 `different person, different face, different hair, different clothes, different location, different camera angle`（结构沿用 render_keyframes.py:22,27-28 的已跑通模板；措辞为按角色泛化的改写**而非原句**——代码原句是 "keep exactly the same **girl**, face, hairstyle, **uniform** … change only **the following** —"，person/outfit 替换处属泛化，首跑回归时对照验证）；`last_mode: fresh` = 大机位变化时全新 t2i（身份仍靠锚点，S4 先例 render_keyframes.py:42-43）。
  - `use_char_ref: true`（**B 级·待标定，默认关**）：把已 pick 三视图作参考图。**实现必须走 edit_wf 图形**——LoadImage+ImageScale 预处理、三视图作 `images.image_1`、latent 取 TextEncodeQwenImage21 输出 slot 2（render_keyframes.py:69-77 的已跑通结构）。**禁止**照抄 img_wf 的 ref_images 分支（comfy_client.py:65-67,77-78）：该分支 latent 仍取 EmptyQwenImage21Latent，三处调用从未传过 ref_images（test_s1.py:21、render_keyframes.py:121,133），零运行证据且图形结构不同，参考图在空 latent 下能否生效未知。标定通过（3-5 镜对比有无参考图的身份一致性）前不开默认。
- **输出**：`keyframes/S#_first.png`、`S#_last.png` + manifest（记录 prompt_ids、seeds、attempts）。失败后**总尝试 2 次**（1 次 + 1 次重试，`run.attempts`）再标 `failed`，不阻塞其它镜（沿用 render_keyframes.py:122-123,144 策略），段末汇总失败清单非零退出。

### 4.3 camera — 运镜（双模式）

- **输入**：`shots.yaml` 每镜 `camera:` 节 + `keyframes/` 产物。
- **模式B（默认，零依赖直通 H3）**：按 `move × intensity` 从 `pipeline/camera/vocab.yaml` 词库取英文短语，加 `extra` 自由补充，渲染成一行 `Camera: <短语>` 并入该镜 H3 提示词。词库条目以《雨落无声》文案为种子（如 `push_in/slow` → "very slow push-in, breathing hand-held stillness"，shots_video.json:3 原句；注意该提示词从未被 H3 实际消费过，词库效果随 §4.4 一并标定）。产物仅 `camera/S#_camdesc.txt`（可审计的最终 Camera 行）。
- **模式A（Blender 运镜 / 2.5D 视差，C 级·全部待首跑验证）**：
  1. 取预设模板（`camera/presets/*.py`，参数化 bpy：图像贴平面 + 相机路径 + 缓动 + overscan 裁切；`parallax_2p` 为前后双平面近似 2.5D）或 LLM 生成（llm.py 调 OpenAI 兼容接口，system prompt 注入预设契约与安全约束；**未配置/失败一律回退预设**；`require_review: true` 时 LLM 代码只落 `camera/S#.bpy` 不执行，人工过目后 `--trust-llm` 才跑）。引擎设置用双枚举 try/except（`BLENDER_EEVEE_NEXT` → 失败换 `BLENDER_EEVEE`，§2.1 注）。
  2. `blender --background --python S#.bpy` headless 渲染（blender.py 负责首次便携安装：下载 zip → **Python `zipfile` 解压**到 `install_dir` → 定位 blender.exe 缓存路径。不用 `tar -xf`：已实测 Git Bash tar 为 GNU tar 1.35 不解 zip、Python tarfile 对 zip 抛 `ReadError('truncated header')`，只有 System32 bsdtar 能解且不保证被 PATH 命中）。
  3. **模式A 首跑验证点**（一次性检查，通过后才开放批量）：a) 便携包安装+启动退出码；b) headless EEVEE 在本机 Windows/NVIDIA 驱动栈上可渲染（无显示器场景，C 级未实测）；c) 双枚举写法在目标版本生效；d) 1080p 单帧渲染时长（评估整段可行性）。
  4. 产物两种 `output:`：
     - `endpoints`（**默认，推荐**）：渲染运镜起点/终点两帧 → `S#_camfirst.png`/`S#_camlast.png`，替换原始首末帧送入 H3 —— 运镜感进入端点构图，生成运动与音频仍由 H3 负责。
     - `full_video`：整段运镜 PNG 序列 → ffmpeg 合成 `camera/S#.mp4`（**无生成音频**，final 段先补轨再 concat，见 §4.5）。video 段对该镜跳过 H3。适用于 H3 不可用/不擅长的镜头（纯 Ken Burns/视差），是保守兜底。
- **输出**：`camera/S#.bpy`、`S#_camdesc.txt`、（A/endpoints）`S#_camfirst/last.png`、（A/full_video）`S#.mp4` + manifest（unit=镜，记录模式、模板名/LLM 模型与响应哈希、blender 版本）。
- **资源时序**：模式A 渲染时段内不向 ComfyUI 发任何任务（Blender 与 ComfyUI 不同时占 GPU；H3 int8 unet 20.8G 几乎吃满 24G，全程**串行一次一个 job**，不并发——孤儿 job 收敛规则（§4.0）即为此兜底）。

### 4.4 video — 本地 H3 首尾帧图生视频（B 级·整段待首跑标定）

- **输入**：keyframes（或 camera endpoints 产物）、shots.yaml 的 `motion`（Timeline 动作描述）+ camera 行、全局 video 配置。
- **行为**（unit=镜）：
  - 前置门：服务器可达预检（§4.0）；权重就绪（unet/te ≥5GB、所选 LoRA ≥1.5GB，沿 render_videos.py:29-35,85 的大小门，但改为**立即失败并提示**而非死等，`--wait-weights` 才轮询）；模式A `full_video` 的镜直接跳过。
  - 提示词组装：`<镜头风格段> \n\n Timeline:\n<motion> \n\n Camera: <S#_camdesc.txt 内容> \n Audio: <audio_hint> \n No text, no subtitles, no watermark. Keep the character design consistent.`（结构 = shots_video.json 的提示词版式；注意该版式同样零运行证据，属首跑标定对象）。**镜头风格段来源有定义**：shots.yaml 单镜 `scene` 字段（场景+画风综合描述，对应 shots_video.json:3 版式的第一段）；未填时回退为 `first 描述 + style_anchors[style]` 拼接——两条路径的实际取值都写入 manifest 供审计。
  - 首末帧复制进两个 input_dirs（render_videos.py:73-76 做法），提交 `h3_fl2v` 图（=video_wf 节点结构原样，仅参数注入：LoRA 名、steps（随档位）、length、seed）。
  - 时长（**命中规则三步，不留给实现者现场发明**）：① 请求校验：`duration_s` 必须落在 [min_request_s, max_request_s]=[5.0, 15.0]，越界即配置错误；② 映射：`k = round((d×fps − b)/a)`，夹取 [k_min, k_max]=[7, 21]，`length = a×k+b`，实际时长 `eff = length/fps`，可达域 **5.167–15.083s**；③ 容差断言：`|eff − d| ≤ a/fps/2 ≈ 0.354s`（半步），超限**显式报错并列出最近两个网格点**（不静默大误差夹取）。本会话已用脚本逐 0.5s 扫 5.0–15.0 全部 21 点：全部命中，端点 15.0→k=21→15.083s（误差 0.083s）。**"124 帧≈5.17s"是设计值不是验证值**——本机从未成功执行过任何 H3 渲染（output/shots/ 为空，videos.log 的唯一 attempt 无结果行）。**首跑标定必须包含 5.17s/124 帧本身**：每个新项目（或网格参数变更后）先试产 1 镜（`--only S1`），人工确认时长/音轨/清晰度后再批量；网格常数若与 H3 节点实际要求不符，改 config `length_grid` 与 k 域一处即可，代码不写死。
  - 断点续跑：目标 `video/S#.mp4` 存在且 ≥200KB 且 manifest done → 跳过；失败后总尝试 2 次（1 次 + 1 次重试，`run.attempts`）；崩溃重启后由 §4.0 孤儿收敛处置遗留 prompt。
- **输出**：`video/S#.mp4`（1344×768、24fps、含 H3 生成音频轨——双 VAE 音频链路本身也是 B 级待验证）+ manifest（prompt_id、seed、length、时长分钟数）。

### 4.5 final — 合成（B 级·待首跑标定）

- **输入**：shots.yaml 镜序、`video/*.mp4` + `camera/*.mp4`（full_video 镜）、`subtitles.yaml`、可选 `assets/ambience.mp3` 音频床轨。
- **行为**（**次序固定，补轨必须前置于 concat**——concat demuxer 面对有声/无声混合输入会失败或时长错乱）：
  1. **逐镜音频规整**：模式A `full_video` 无生成音频的镜，先各自补静音轨（`-f lavfi -i anullsrc`）或与 ambience 混音，产出规整后的临时输入文件；有声镜原样通过。
  2. concat（转码 x264 crf18 + aac 192k，统一参数避免流不一致；缺镜告警并剔除后继续，build_final.py:32-35 同策略）。
  3. ffprobe 逐镜时长 → 计算字幕全局时间轴偏移（build_final.py:45-48 算法）。
  4. `subtitles.yaml` cues（`{shot, t_in, t_out, text}` + 片头/片尾卡）→ SRT → `subtitles` filter 烧录（force_style 用 config；**Windows 路径转义 `C\:/`、冒号双写**，build_final.py:71 写法封装进 ffmpeg_tool）+ 可选 drawtext 标题卡（alpha 渐入渐出模板）。中文路径 + subtitles 滤镜组合从未在本机跑通过，首跑标定点。
  5. contact sheet：ffmpeg `scale+tile` 生成每镜首末帧对照表（替代 PIL 版 contact_sheet.py；缺帧画格以纯色+drawtext 标 `MISSING` 近似）。**帧来源规则（与成片一致）**：模式B 镜 = keyframes 的 `S#_first/last.png`；模式A endpoints 镜 = `S#_camfirst/camlast.png`（即实际送入 H3 的端点）；模式A full_video 镜 = 该运镜视频首/末帧各抽一帧——一律取"实际进入成片的视觉端点"，源文件入 final 段 upstream 指纹（§5），重渲后 contact_sheet 判 stale 重生成，不会产出与成片不一致的对照表。
- **输出**：`final/<项目名>.mp4`、`<项目名>.srt`、`contact_sheet.png` + manifest（总时长、各镜偏移、命令日志）。

### 4.6 段0 screenplay — 编剧（write / breakdown；不进 `--stage all`）

- **screenplay-write（创意/大纲 → 剧本.md）**：输入 `--premise`（题材一句话）、`--style-note`（风格自由文本）、`--shots N`（镜数）、`--total-s S`（总时长预算）。先做预算可行性检查（N × H3 网格可达域 5.17–15.08s/镜，§4.4，±1 步容差），不可行一句报错退出。LLM 按项目台本模板结构（§6）生成：元信息（含机器可读预算行 `- 预算: N 镜 × Ts`）/人物设定（每角色一行 English anchor）/三幕结构/分镜表（每镜首帧/尾帧/H3 时间线/运镜/Audio/字幕）；生成后跑**结构 lint**（章节/锚点/镜号连续/预算行），不过则错误清单带回 LLM 重生成，**最多 3 次**，仍失败 → `剧本.draft.md` + `screenplay_review.txt` 保留最后产物，exit 1。剧本是自由文体，框架不解析（§6），结构化数据只认 yaml。
- **breakdown（剧本.md → shots.yaml + characters.yaml + subtitles.yaml）**：LLM 输出 JSON → **本地确定性后处理**（时长逐镜夹取进请求域并按 17k+5 网格落点；总预算超限逐镜降档直至命中；运镜词库标注（模式B move × intensity）；每镜一条字幕 cue（t_out 夹在镜内）；seed 一律写 0=按 §3 规则 4 派生）→ **schema 强校验**（pipeline.schema，与人工填写同一把尺）+ 双语字段/预算/引用完整性检查；不过则错误清单带回 LLM 重新生成，最多 3 次；仍失败 → 非零退出，最后一次产物保留为 `*.draft.yaml` + `breakdown_last_output.txt` + `screenplay_review.txt` 供人工修补。落盘次序：内存校验 → 自写 YAML 经 miniyaml 往返校验 → 旧版备份 `*.bak` → 写入 → 磁盘终检（`schema.load_project_data`），终检不过自动回滚。
- **降级（未配置 LLM，`llm.enabled=false` 或字段缺失）**：write 生成填好预算/题材的骨架 `剧本.md`（S1..SN 空表 + 两个 LLM 示例的提示）；breakdown 降级为**三份人工输入的校验器**（同一套 schema 校验 + 角色/镜数/cues/总时长汇总），提示信息友好。已配置但调用失败（服务未拉起/HTTP 错误）：重试后报错 exit 1（不静默降级——生成内容本身即产物）。
- **内容边界**：模板与提示词默认中性叙事、**全体角色一律成年人（18+）**；项目尺度由 project.yaml 的 `screenplay.content_rating` 自行声明（common.content_boundary 合成；无论声明为何，成年底线不变）。尺度决策留在剧本层，不进框架代码（§10.9）。
- 段属性：无 ComfyUI 依赖；不写段 manifest（产物本身即状态：剧本.md/三份 yaml，重跑前旧版自动备份 .bak）；`--stage screenplay` = write → breakdown 串联。


---

## 5. 状态与断点续跑（manifest 机制）

统一 schema（每段一个 JSON，位于该段产物目录）：

```json
{
  "stage": "keyframes",
  "project": "雨落无声",
  "schema": 1,
  "config_hash": "sha256:…（合并后配置）",
  "updated_at": "2026-10-26T21:30:00",
  "units": {
    "S1:first": {
      "status": "done",            // pending | running | done | failed | skipped | stale
      "files": {"first": "S1_first.png"},
      "prompt_id": "…",            // ComfyUI prompt_id：孤儿收敛（§4.0）与 recover 的依据
      "seed": 20261000, "channel": "photo",
      "inputs_hash": "sha256:…（见下：yaml 输入行 + 上游产物指纹）",
      "upstream": {"keyframes_manifest": "sha256:…", "first_png": "S1_first.png|1048576|1698…"},  // 路径|大小|mtime_ns
      "attempts": 1, "error": null, "updated_at": "…"
    }
  }
}
```

- **完成判定**（三条件同时满足才算 done）：`status==done` ∧ 文件存在 ∧ 大小 ≥ 段配置下限（图 100KB / 视频 200KB）。防"manifest 说好了但文件被删/半截"（4-step LoRA truncated 残件教训的同类防御）。
- **跳过**：run 时逐 unit 先判完成；完成即跳过 = 断点续跑。`--force` 重跑指定 unit。
- **失效判定 = `inputs_hash`（本 unit 的 yaml 输入行）∨ `upstream` 指纹（上游产物文件）∨ `config_hash`（该段相关配置节）任一变化**：
  - 上游产物指纹 = `路径|字节大小|mtime_ns`（大文件不哈希内容，代价 O(1)；yaml/词库等小文本文件用内容 sha256）。**跨段文件依赖显式入指纹**：keyframes 段的 PNG 进 video 段 unit 的 `upstream`；`camera/S#_camdesc.txt`（及 camfirst/camlast.png、full_video mp4）进 video 段对应 unit；`ambience.mp3`、video/camera 的 mp4、**以及 contact_sheet 的源帧（keyframes PNG / camfirst/camlast / 运镜抽帧，按 §4.5 帧来源规则）**进 final 段 unit。
  - 例：`keyframes --force` 重渲 S1 后，S1 PNG 的 mtime/大小变化 → video 段 `S1` unit 判 stale → 重渲（否则会拿旧帧出不一致成片）；改 camera 词库或 `extra` → camdesc 内容哈希变 → video 段 S1 stale。默认 stale 即重跑（`--keep-stale` 可保留），只向下游传染一层并打告警，不静默。
- **原子写**：tmp 文件 + `os.replace`，任何时刻被杀都不会留半个 manifest。
- **孤儿与半途回收**：进程死于 ComfyUI 仍在执行时（unit 停在 `running`）→ §4.0 孤儿收敛（查 history 收割 / 查 queue 等待，**不重提**）；已算完但死在下载前 → manifest 有 prompt_id，`python -m pipeline recover <项目> [--pid <id>]` 按 history 回捞产物补登记（resume.py 思路的产品化）。
- **运行日志**：`logs/<项目>_<段>.log` 追加式，含每次提交的完整提示词与节点参数（可复现性）。

---

## 6. new-project 脚手架

```
python -m pipeline new 我的新剧 [--style anime|photo]
```

生成 `projects/我的新剧/`：`project.yaml`（按所选通道预填 style_anchors 模板）、`剧本模板.md`、`characters.yaml`、`shots.yaml`、`subtitles.yaml`、五个产物目录 + 空 manifest。

- **剧本模板.md**：沿用《雨落无声》台本结构（元信息 → 人物设定含 English anchor → 三幕结构 → 分镜表（首帧/尾帧/H3 动效/声音/字幕）→ 技术流水线），每节带填写说明；这是"人/LLM 写 shots.yaml"的中间层，框架不解析它（保持剧本自由文体，结构化数据只认 yaml）。
- **shots.yaml 模板**（首镜为注释齐全的完整示例；所用语法全部落在 miniyaml 支持集内，§2.3）：

```yaml
project: 我的新剧
style: photo
defaults: {duration_s: 5.17, camera: {mode: B, move: push_in, intensity: slow}}
shots:
  - id: S1                # 纯镜号，排序即成片顺序
    title: 示例镜
    duration_s: 5.17      # 5–15，映射到 17k+5 帧网格（网格待首跑标定，§4.4）
    characters: [主角id]   # 引 characters.yaml 的 id
    style: photo          # 可选：单镜覆盖通道
    scene: "…"            # 可选：H3 提示词首段"场景+画风"综合描述；缺省=first 描述+画风锚点（§4.4 组装规则）
    first: "…"            # 首帧镜头描述（英文；角色/画风锚点由框架拼接）
    last:  "…"            # 末帧相对变化描述
    last_mode: edit       # edit（保身份，默认）| fresh（大机位变化）
    use_char_ref: false   # photo 通道可用三视图作参考图（B 级，待标定，默认关）
    camera: {mode: B, move: push_in, intensity: slow, extra: ""}
    motion: |             # H3 Timeline 段（动作/时间线）
      [0s-2s] …
    audio_hint: "steady rain, distant traffic"
    negative_extra: ""
    seed: 0               # 可选：显式 seed（0=按 §3 规则 4 派生）
```

- **characters.yaml 模板**：`characters[]`（id/name/anchor_en/outfit_en/negative_extra/assets{three_view, expressions[], props[]}）+ `props_catalog`（道具 → 英文锚点映射）。id 用 ASCII（文件名安全），name 中文。
- 生成后立即跑 schema 校验（必填/类型/引用完整性：shots 引用的 character/prop 必须存在），校验结果直接打印；`selftest.py` 第 10 项会对临时目录里脚手架生成的示例项目重复 schema 校验 + miniyaml 解析。

---

## 7. selftest.py（根目录，仅标准库干跑）

`python selftest.py [--with-server] [--strict]` —— **不渲染、不下载、不修改项目数据**；逐项打印 PASS/FAIL/SKIP，**任一 fatal 项失败即 exit 1**。检查清单：

| # | 检查 | 级别 |
|---|---|---|
| 1 | 目录契约：pipeline/（含 `__main__.py`，`python -m pipeline --help` 可执行）、config.yaml、projects_root、logs 可创建 | fatal |
| 2 | miniyaml 往返等价（内置样例，含中文/多行/flow 序列/嵌套 flow 映射） | fatal |
| 3 | 配置：全局可解析 + 必填校验 + **合并规则**（覆盖样例断言）+ 相对路径以仓库根锚定（在临时 cwd 下以仓库根路径加载配置，断言 projects_root/logs_dir 解析结果不变——测路径锚定，不测包发现） | fatal |
| 4 | 路径存在：portable_root/models_dir/input_dirs、font 文件 | fatal |
| 5 | ffmpeg/ffprobe 可发现（`shutil.which` 或配置绝对路径）+ `ffmpeg -version` 可执行且 ≥4（subprocess 调用，无渲染） | fatal |
| 6 | 权重在盘：photo 三件套、anime checkpoint、**H3 六件套**（unet/te/双 LoRA/双 VAE；按 models_dir + 配置中的**相对子路径**拼全路径——六件套分属 4 个子目录，拿裸文件名拼 models_dir 必 miss）+ 大小下限（LoRA 1.5G / unet,te 5G） | fatal |
| 7 | 五个工作流构造器在假参数下产出合法 API 图：每个 `[id,slot]` 引用的节点存在、class_type 非空、无 UI 格式残留字段 | fatal |
| 8 | 帧长网格：default_duration 映射合法 length 且 \|eff−req\|≤半步（a/fps/2）；5.0–15.0 逐 0.5s 共 21 点全部命中（k 域 [7,21]，可达 5.167–15.083s，端点 15.0→k=21→15.083s） | fatal |
| 9 | manifest 原子写/读/完成判定/**上游指纹失效**单测（临时目录：改上游文件 mtime → 断言 unit 转 stale） | fatal |
| 10 | 脚手架示例项目（临时目录）：**模板文件先过 miniyaml 解析**（规范自洽，§2.3），再 schema 校验通过 | fatal |
| 11 | ComfyUI 节点清单：**仅 `--with-server`** 时 GET /object_info，断言 `MiniMaxH3ImageToVideo`、`TextEncodeQwenImage21`、`UnetLoaderGGUF` 在列（H3 节点当前未装——custom_nodes 仅 ComfyUI-GGUF 与 SakuraQwenImage21Tools，已核实——装好前此项会 FAIL 并给出提示文案；服务器未启动时提示先跑 run_nvidia_gpu.bat）；不加参数则 SKIP | with-server 时 fatal |
| 12 | Blender：任一项目存在模式A 配置时检查 install_dir/blender.exe；未装提示"由 camera 模块首次使用时自动安装" | `--strict` 时 fatal，否则 SKIP+提示 |
| 13 | screenplay 段0 dry 校验（无 LLM 也能跑）：llm_client 配置判定 / 模板模式生成 剧本.md / 结构 lint 有界退回 / 校验器模式 / schema 强校验+落盘终检 / 预算约束 / CLI 冒烟（假 chat_fn 注入，不触网） | fatal |

selftest 只依赖标准库（json/urllib/subprocess/hashlib/tempfile/argparse/zipfile…），与框架本体同一约束。

---

## 8. CLI 总览

```
python -m pipeline new <名> [--style photo|anime]
python -m pipeline run <名> [--stage assets|keyframes|camera|video|final|all] [--only S1,S3] [--force] [--dry-run] [--set a.b=c]
python -m pipeline run <名> --stage screenplay-write --premise "…" [--style-note "…"] [--shots N] [--total-s S]
python -m pipeline run <名> --stage breakdown        # 剧本.md → 三份 yaml；未配置 LLM = 三份人工输入的校验器
python -m pipeline run <名> --stage screenplay       # = screenplay-write → breakdown 串联（段0，不进 all）
python -m pipeline status <名>          # 各段 manifest 汇总表
python -m pipeline doctor               # = selftest 的 --with-server 版 + 权重/节点详查
python -m pipeline recover <名> [--pid <id>]
python selftest.py [--with-server] [--strict]
```

入口实现：`pipeline/__main__.py` 转发 `cli.main()`（缺它则 `python -m pipeline` 直接报 "pipeline is a package and cannot be directly executed"，已实测复现）。段间依赖链：assets →(角色 pick) keyframes → camera → video → final；`--stage all` 按序执行，每段开始前走 §4.0 公共前置（服务器预检 / 孤儿收敛 / 依赖检查）。

---

## 9. 已核实的环境事实清单（设计依据）

| 事实 | 证据（本次会话核实） |
|---|---|
| ComfyUI 8188、便携包、内嵌 Python 3.13.14；**当前 8188 未监听、无 python 进程** | `python.exe --version`；comfy_client.py:5；`netstat -ano | grep 8188` 无结果；`tasklist` 无匹配 |
| custom_nodes 仅 ComfyUI-GGUF、SakuraQwenImage21Tools；**MiniMax H3 节点未装** | `ls …/custom_nodes` 输出（值班工程师补装中） |
| photo 三件套 / anime checkpoint / H3 六件套（unet/te/双 LoRA/双 VAE）在盘 | `ls` checkpoints/diffusion_models/text_encoders/vae/loras |
| 4-step LoRA 有 `…truncated.bak` 残件 | loras 目录列表 ⇒ lora_min_bytes 门必要 |
| **photo 关键帧批次已跑通**（20 张 PNG + contact_sheet.png 在盘） | keyframes.log 尾部 `S9 last: OK / S10 … OK / KEYFRAME BATCH FINISHED`；`ls output/` |
| **H3 视频零成功运行**：`output/shots/` 空；videos.log 止于 `20:35:09 S1 render attempt 1 ...` 无结果行 | `ls output/shots/`（空）；`cat videos.log` |
| **final 零运行**：无 `_merged.mp4/subtitles.srt/ep01*.mp4` | `ls output/` |
| **唯一实际择档：4-step LoRA + steps=4**（且未渲染成功）；剧本:102 的"4step…8 steps"自相矛盾不作依据 | videos.log 20:35:09 `lora=…4step… steps=4`；剧本:102 |
| ffmpeg 8.1.1 在 PATH；系统 Python 3.14.3；Blender 未装 | `which ffmpeg` + `-version`；`python --version`；`which blender` 无结果 |
| `python -m` 缺 `__main__.py` 必报错（exit=1） | 临时包复现：`No module named pipeline.__main__; …cannot be directly executed` |
| Git Bash tar=GNU tar 1.35 不解 zip；Python tarfile 对 zip 抛 `ReadError('truncated header')`；zipfile 可解 | 临时 zip 实测三种工具输出 |
| Blender 4.2–4.5 引擎枚举为 `BLENDER_EEVEE_NEXT`（≤4.1 与 5.0+ 用 `BLENDER_EEVEE`）；本机未装未实测 | Blender 官方 5.0 Python API 发布说明；sketchfab/blender-plugin issue #153/#176（外部依据） |
| `sakura_t2i_ref.json` 为 UI 格式不可直提 API | 文件头 `nodes/links` 结构 |
| 1344×768、length=124@24fps、res_multistep/simple 为**代码字面量**（H3 侧零运行证据，按设计值处理） | render_keyframes.py:121（A 级）；render_videos.py:47-51、剧本:102（B 级） |
| 双 input 目录、双 LoRA 择优、>5GB 权重门、>200KB 跳过门 | render_keyframes.py:10-11；render_videos.py:32-35,85,109 |
| img_wf 的 ref_images 分支零调用（三处调用均未传参） | test_s1.py:21；render_keyframes.py:121,133 |
| 权重分属 models 下 4 个子目录（photo unet 与 H3 unet 同在 diffusion_models；双 TE 在 text_encoders；LoRA 在 loras；VAE 在 vae） | `ls` 各子目录输出 ⇒ config 存相对子路径、检查按全路径 |
| 网格命中：k∈[7,21] 时 5.0–15.0 逐 0.5s 全部 21 点满足半步容差（端点 15.0→k=21→362 帧=15.083s，误差 0.083s；可达域 5.167–15.083s） | 本会话 Python 脚本逐点扫描输出 |
| edit 前复制首帧到 input_dirs 曾因目录缺失翻车（FileNotFoundError），后补 makedirs | keyframes.log:4-12 异常栈；render_keyframes.py:136-139 |
| 仓库外执行 `python -m pipeline` 报 No module named pipeline（包发现依赖 cwd/PYTHONPATH；路径锚定不受影响） | 本会话 `cd E:/projects/推理 && python -m pipeline` 输出 |

---

## 10. 风险与保守决策

1. **video/final 全链零运行证据（当前最大风险）**：H3 工作流、双 VAE 音频轨、帧长网格（含 124 帧本身）、concat、中文路径 subtitles 烧录，全部没在本机成功执行过一次。对策：§0 证据分级 + §4.4/§4.5 显式"待首跑标定" + 每新项目先 `--only S1` 试产；施工者不得按"参数照抄即稳"移植 video/final 两段。
2. **H3 节点未装**（已核实）：video 段被阻塞；selftest --with-server 与 doctor 把它顶到显眼位置。框架不代装节点（归值班工程师），只检测与提示。
3. **anime 通道未经本仓库验证**（C 级）：waiIllustrious 权重在盘但 `_tools` 从未引用。落地顺序里排最后，先 3–5 张标定 cfg/steps/img2img_denoise 再接项目；标定结果回写 config。
4. **use_char_ref 走 edit_wf 图形且默认关**（B 级）：参考图分支零运行证据、图形结构存疑（§4.2），标定通过前不开默认。
5. **H3 帧长网格是设计值**：仅代码字面量与剧本文案，无一次成功运行背书；首跑把 5.17s/124 帧纳入试产，网格不符改 config `length_grid` 一处。
6. **LLM 生成 bpy 的安全**：默认 `require_review: true` 只落盘不执行；执行亦限定 headless、cwd 限定 camera 目录、超时杀进程；LLM 不可用/未配置时全自动降级到预设模板，模式A 永不因 LLM 缺席而阻塞。
7. **24G 显存与孤儿 job**：全链串行、一次一个 ComfyUI job；崩溃后 `running` 状态的 unit 由 §4.0 收敛（查 history/queue，不盲目重提），防双 H3 job 同跑 OOM 双杀。
8. **Windows 细节**：Blender zip 用 `zipfile` 解压（GNU tar/tarfile 均不行，已实测）；Blender 引擎枚举按版本 try/except 双写；ffmpeg filter 中盘符 `C\:/` 转义与冒号双写封装进 ffmpeg_tool（build_final.py:71 前车之鉴）；subprocess 显式 `encoding="utf-8"`；配置路径一律正斜杠；相对路径以仓库根锚定不随 cwd。
9. **内容尺度**：框架内容无关，R18 尺度决策（剪影/局部特写呈现，见 剧本:6）留在剧本层，不进框架代码。

## 11. 落地顺序

1. 核心：config/miniyaml/comfy/manifest/cli/**__main__.py** + **selftest.py 先行**（骨架未写段前 selftest 已可跑 1–10 项）；
2. photo 通道：assets → keyframes（移植 A 级路径，回归验证与《雨落无声》批次一致）；
3. video：**首跑标定**（等 H3 节点装好、8188 拉起：先 `--only S1` 验证 124 帧/音频轨/时长，再批量）；
4. final：**首跑标定**（补轨次序、concat、中文路径字幕烧录各验一次）；
5. camera 模式B（词库，随 video 标定一并验）；模式A（Blender 便携安装 zipfile 解压 + 3 个预设 + 引擎枚举 + headless EEVEE 首跑验证点；LLM 可选开关）；
6. anime 通道标定后开放；use_char_ref 标定后决定默认值。
