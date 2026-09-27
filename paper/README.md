# 论文与结果入口

主稿沿资料、A 认知测量、B 比较资格、C 指标与条件范围、D 检验反馈、E 教学行动组织。正文采用最终 t3 的真实聚合结果；早期首问与三模型流程探索、合成分配对照及 AI 采用记录在附录中单列。

- [论文 PDF](../build/paper/main.pdf)：正式排版；正文和参考文献与附录分别计页。
- [Markdown 阅读稿](研究论文.md)：由当前 LaTeX 展开生成，五张统计图使用 PNG 预览。
- [聚合结果与方法条件](../results/final-analysis/summary.json)：真实、条件情景、合成计算及早期探索分别标识。
- [复现说明](../docs/复现说明.md)：仓库固定版本、公开聚合与授权输入的不同计算范围。

`main.tex` 是论文结构与数学定义源，`final_analysis_results.tex` 接入最终聚合段落、表格及五组统计图，`appendix_reproducibility.tex` 记录数据单位和计算方法，`appendix_ai_usage.tex` 记录 AI 使用与人工采用。结果段落由 `generate_final_analysis.py` 从固定汇总生成。当前主稿引用 17 篇已核验文献。

彩色数据图先由 `scripts/build_final_analysis.py` 分别生成十个独立子图，再由 `paper/figure_groups/` 中的五份 LaTeX 模板组合；`paper/figure_style.tex` 统一子图题、居中的主图题以及另起一行、9 pt 左对齐的图注。图题与表题继承 CUMCM 模板的小四号加粗设置。论文直接读取组图模板，`scripts/build_figure_groups.py` 用同一模板导出 README 和 Notebook 的完整组图预览。

## 编译与同步

论文使用现有多文件 XeLaTeX/Biber 工具链。已配置工作区直接执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_latex.ps1 -Source paper/main.tex -OutputDirectory build/paper
```

从固定聚合结果生成论文结果段落与表格：

```powershell
.venv/Scripts/python.exe -X utf8 paper/generate_final_analysis.py
```

生成器核对汇总文件 SHA-256，生成流程、分布、敏感性表及图路径；解释段落与该结果版本绑定。`--check` 检查输出字节一致性而不写文件。源结果变更时需同步审阅解释并更新明确的版本标识。

仅同步 Markdown 阅读稿：

```powershell
.venv/Scripts/python.exe -X utf8 paper/update_results.py --mirror-only
```

`--mirror-only` 展开当前主稿、结果段落及附录。结果生成、排版与阅读稿同步分别使用上述命令，Notebook 的执行入口见[复现说明](../docs/复现说明.md)。

## 结果版本

主分析快照为逐回合 t3，真实学生回合 3515，固定随机复核 352，高风险复核 827。任务与贡献候选分别为 1010、776，共同非空配对为 48。分数敏感性针对 356 个具有任务候选的学生—学期单元，完整 AIV 可计算单元数为 0，点分保持缺失；图中范围为明确情景下的保守外包。

冻结清单 SHA-256：

```text
7e885ceecf12a180349369ed9ddfc962ad71f1ea270f68b037a86a396430631d
```

最终分析 `summary.json` SHA-256：

```text
a181f0840a47c129aeb148f1ea5b36c5941a0943cd5fb1ebfa2acd8bfc28a64c
```

图表来源为 `results/final-analysis/fig01_dimensions` 至 `fig05_score_sensitivity` 的 PDF/PNG/SVG，独立子图文件保存在同一目录。五组彩色统计图的中文使用宋体（SimSun），西文与数字使用 Times New Roman，PNG 按 600 dpi 导出；论文通过 LaTeX 组合独立的矢量 PDF 子图。各格式下载入口见[根目录图表清单](../README.md#主要发现)。论文的源文件及 PDF 哈希随实际交付清单记录；复现代码提交通过仓库交付说明固定。

统计图与 Notebook 共用字体规范。重绘设备须安装可由 Matplotlib 识别的 SimSun 和 Times New Roman；缺少任一字体时应先补齐已有授权的字体，再执行重绘。字体文件属于运行环境依赖，不随仓库分发；已导出的 PNG/PDF/SVG 可直接阅读。完整环境要求见[复现说明](../docs/复现说明.md)。

## 模板与数据范围

排版基于 `templates/CUMCMThesis/` 固定版本，字体为 SimSun/SimHei、Times New Roman。模板固定版本与公开分发范围见 [第三方资源来源](../docs/第三方资源来源.md)。编译需要取得对应类文件及字体；成品 PDF 可直接阅读。

公开材料提供聚合结果、合成演示和论文，不包含学生原文、私有标识、数据库与凭据。授权输入的来源清单、哈希及原始逐条结果保留在私有研究环境中。
