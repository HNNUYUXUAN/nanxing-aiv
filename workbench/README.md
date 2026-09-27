# 南行测评工作台

工作台提供资料核查、模型判断对照、争议处理和教学报告。本地示例包含 **192 条合成记录**及六类题文的保存响应，可离线回放。

[返回项目首页](../README.md) · [计算与复现](../docs/复现说明.md) · [界面演示](../slides/README.md)

![教师报告界面：构造输入的演示](../slides/roadshow/images/11.jpg)

## 启动合成示例

先按[运行指南](../docs/复现说明.md)安装根目录的 Python 依赖，然后在仓库根目录执行：

```powershell
cd workbench
..\.venv\Scripts\python.exe -X utf8 -B launch_public.py --port 8780
```

macOS / Linux 使用 `../.venv/bin/python`。浏览器打开 [本地工作台](http://127.0.0.1:8780)，依次查看资料、候选判断、待核事项及教师报告。终端按 Ctrl+C 停止服务。

启动器使用独立的合成资料目录和保存响应，禁用服务端外发连接，无需 API 密钥。操作状态保存在 `workbench/runtime/product`。前端构建文件已随仓库提供。

## 复用与开发

本目录是独立子项目，拥有自己的 `aiv` 包；服务与测试均从本目录运行。

```powershell
..\.venv\Scripts\python.exe -X utf8 -B -m pytest tests/test_product_api.py tests/test_product_reports.py tests/test_product_runner.py tests/test_product_frozen.py
```

前端源码位于 `demo/src`，工具链位于 `tools/web-toolchain`。修改前端需使用 Node.js 24，在 `tools/web-toolchain` 执行 `npm ci`，再从 `demo` 执行 `../tools/web-toolchain/node_modules/.bin/vite build`。

部署自己的资料与模型服务时，参考 `aiv/product_settings.py` 和 `scripts/run_product.py`，显式配置资料、服务及评审口令；`launch_public.py` 用于合成回放。

<details>
<summary>来源与许可</summary>

来源提交为 `d607eae4450b8139164b980b923048e22c0256fe`，文件摘要见根目录 `REPOSITORY-MANIFEST.json`。依赖许可证与 Apache 通知保存在 `licenses/`；第三方源码与构建结果遵循各自上游许可。

</details>
