# 棋子分类器微调结果（2026-10-01）

- 模型：PaddleClas `PPLCNet_x1_0`，14 类，输入 `160x160`
- 初始化权重：`output/xiangqi_pplcnet_x1_0/best_model.pdparams`
- 数据目录：`training_dataset/piece_character_dataset/images/needs_review`
- 数据总量：575 张 / 50 个原始棋盘帧
- 训练集：463 张 / 42 帧
- 验证集：112 张 / 8 帧
- 微调轮数：40
- 初始学习率：`0.001`，2 轮 warmup，Cosine 衰减
- 最佳验证：Top-1 `1.00000`，Top-3 `1.00000`
- 最佳权重独立复核：Top-1 `1.00000`，Top-3 `1.00000`，Loss `0.11691`
- 最佳权重：`output/xiangqi_pplcnet_x1_0_finetune/best_model.pdparams`
- 静态推理模型：`output/xiangqi_pplcnet_x1_0_finetune/inference/`

静态模型已通过 CPU 单图推理。验证样本
`red_shuai/chess-441BF68B2D54-B0CCB19C-000010__c002.jpg` 的 Top-1 为
`red_shuai`，置信度 `0.929431`。

验证集按原始棋盘帧隔离，但仍来自相同设备、棋盘和采集流程。上线前建议继续使用不同光照、
角度和距离的独立数据测试，避免把当前 100% 验证准确率解释为所有场景下的泛化性能。
