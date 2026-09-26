# 南行测评工作台

工作台支持资料核查、模型判断对照、争议处理和教学报告。192 条合成记录可用保存的六类题文缓存在本地回放。

从本目录运行（Python 环境安装根目录 requirements-lock.txt；本次迁移沿用已有环境）：

```powershell
python -X utf8 -B launch_public.py --port 8780
```

打开 http://127.0.0.1:8780。启动器隔离本地资料目录、移除共享口令，并禁用服务端外发连接。状态保存在 workbench/runtime/product。前端构建文件已包含，可直接启动。

此目录是独立子项目，拥有自己的 aiv 包；应在此目录运行测试与服务，避免与仓库根目录的同名包混用。

```powershell
python -X utf8 -B -m pytest tests/test_product_api.py tests/test_product_reports.py tests/test_product_runner.py tests/test_product_frozen.py
```

前端源码位于 demo/src，锁定的工具链位于 tools/web-toolchain。需要修改前端时，使用 Node.js 24，在 tools/web-toolchain 执行 npm ci，再从 demo 执行 ../tools/web-toolchain/node_modules/.bin/vite build。迁移验证使用现有构建结果，未安装依赖。

独立部署适配入口为 aiv/product_settings.py 和 scripts/run_product.py；使用者应为自己的资料、模型服务及评审口令显式配置环境。launch_public.py 始终用于合成回放。

来源提交：`d607eae4450b8139164b980b923048e22c0256fe`。导出时仅调整默认资料路径与默认样本；源文件摘要和导出文件摘要分别记录在仓库准备收据与 REPOSITORY-MANIFEST.json。

前端随附依赖的许可证与 Apache 通知保存在 licenses/，依赖源码及前端构建结果遵循相应上游许可。
