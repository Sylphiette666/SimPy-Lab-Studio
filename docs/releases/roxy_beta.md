# roxy_beta

这是基于 v1.2.0 的 Windows 测试版，发布分支和标签均为 `roxy_beta`。本 Release 标记为 **Pre-release**，不替换现有正式版，不代表已合并 v1.3.0 的全部改动。

## 本次改动

- 草稿保存到本机实验文件，支持重启恢复、断线重试及多窗口冲突保护。
- CSV／单工作表 XLSX 参数导入，支持字段映射、错误提示和修改预览。
- 参数单位与范围说明、关联检查及错误字段定位。
- 设备瓶颈分析、状态时间分解和缓冲占用热力图。
- 批量参数扫描、任务取消、结果排序、CSV 导出及显式采用方案。
- t 置信区间、配对比较、重复次数建议和实测 KPI 参数校准。
- API Key 遮挡回填、显示／隐藏、DeepSeek 配置模板及连接测试。
- 实验搜索、标签、归档、手动／每日备份及 ZIP 恢复。
- AI 调整前确认、取消与超时处理，以及配套文档和回归检查。

完整功能对比、使用入口和限制见[改动汇总](https://github.com/Sylphiette666/SimPy-Lab-Studio/blob/roxy_beta/docs/roxy-beta-changes.md)。

## 下载

- `SimPy-Lab-Studio-roxy_beta.exe`：Windows x64 测试程序，需要系统具备 Microsoft Edge WebView2 Runtime，无需安装 Python。
- `SimPy-Lab-Studio-roxy_beta-source.zip`：源码快照，包含文档和逐文件校验清单。
- `roxy-beta-changes.md`：完整改动汇总。
- `SHA256SUMS.txt`：上述附件的 SHA-256 校验值。

## 验证

功能版本已于 2026-09-16 在本机完成 310 项 Python 回归（覆盖率 86.85%）、Ruff 检查、8 组浏览器回归、6 项图模型测试及 9 项冻结 EXE 自检。发布沿用该已验证 EXE，本次仅整理发布目标、说明和源码快照。AI 自动化检查使用模拟服务，未调用真实付费接口。

EXE SHA-256：`13b381973a23980ea45ba8c720e2a667a783e241670f3cd767612df4db675bb1`。

自动备份仅在软件运行期间执行；实测校准为有限网格匹配；中断的仿真需重跑。桌面版本号仍显示 1.2.0，界面标注开发更新。
