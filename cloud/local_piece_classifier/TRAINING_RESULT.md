# 训练结果（2026-10-01）

- 模型：PaddleClas `PPLCNet_x1_0`，ImageNet SSLD 预训练，14 类，输入 `160x160`
- 硬件：NVIDIA GeForce RTX 3070 Ti 8 GB
- 框架：PaddlePaddle GPU 3.3.0，CUDA Runtime 12.6
- 数据：403 张人工分类图片，按原始棋盘帧分组切分
- 训练集：322 张 / 25 帧
- 验证集：81 张 / 4 帧
- 训练轮数：100
- 最终验证：Top-1 `1.00000`，Top-3 `1.00000`，Loss `0.11007`
- 最佳权重：`output/xiangqi_pplcnet_x1_0/best_model.pdparams`
- 静态推理模型：`output/xiangqi_pplcnet_x1_0/inference/`

静态模型已分别通过 GPU 与 CPU 单图推理。验证样本
`red_shuai/chess-441BF68B2D54-F525ACCA-000015__c015.jpg` 的 Top-1 为
`red_shuai`，置信度 `0.884073`。

验证集虽然按棋盘帧隔离，但只覆盖 4 个原始棋盘帧。100% 指标应解释为模型已很好地拟合当前
相机、棋盘、光照与裁剪流程；部署前仍建议补充不同光照、棋盘角度、曝光和相机距离下的独立测试集。

后续独立自动标注测试集评估见 `TEST_REPORT.md`。
