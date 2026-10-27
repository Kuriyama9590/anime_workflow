# -*- coding: utf-8 -*-
"""AI 漫剧生产框架 pipeline 包。

本包仅标准库（系统 Python >= 3.10，开发环境 3.14.3 已核实）。
已落地的子模块（六模块并行构建，docs/FRAMEWORK_DESIGN.md §11）：
  - config / config.yaml   全局+项目覆盖配置：加载/深合并/全量校验/哈希/仓库根锚定（§2）
  - miniyaml               受限子集 YAML 解析器（§2.3，与模板严格自洽）
  - schema                 项目四件 yaml 的装载与 schema 校验（§6）
  - scaffold               new-project 脚手架（§6）
  - manifest               各段 manifest：原子写/完成判定/上游指纹失效（§5）
  - comfy                  ComfyUI HTTP 客户端（升级自 _tools/comfy_client.py，§0）
  - ffmpeg_tool / llm      ffmpeg 发现与封装（§4.5）；OpenAI 兼容 LLM 客户端（§4.3）
  - seeds / timing         seed 派生规则（§3 规则 4）；H3 帧长网格三步命中（§4.4）
  - stages                 五段执行器包 + 调度（§4/§8）：assets/keyframes（photo 模块）、
                           video（video 模块）、camera 适配层、final 骨架
  - cli / __main__         统一 CLI：new/run/status/doctor/recover（§8）
  - blender                便携 Blender 安装（zipfile 解压）/发现/headless 执行（camera 模块）
  - camera                 运镜模块（模式A：Blender 预设运镜 + codegen + 渲染合成；
                           模式B：运镜提示词词库直通 H3）
  - screenplay             段0 编剧：write 创意→剧本.md；breakdown 剧本→三份 yaml
                           （LLM / 模板·校验器降级；公共 llm_client 读 config llm: 段）
workflows/（双通道 t2i/edit + H3 构造器）由通道模块维护。
入口：python -m pipeline --help；自检：python selftest.py。
"""
__version__ = "0.1.0"
